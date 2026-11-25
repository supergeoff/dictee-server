# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Sonde des alias LiteLLM speech et transcribe.

Variables d'environnement : LITELLM_URL (finit par /v1) et LITELLM_KEY.
La clé et les en-têtes ne sont jamais affichés.
"""

import os
import sys
import time

import httpx

TEXT = "Bonjour, ceci est un essai de transcription."
VOICE = "fr_marie_neutral"


def wav_sample_rate(data: bytes) -> int:
    """Lit le taux d'échantillonnage dans l'en-tête d'un WAV (chunk fmt)."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("en-tête RIFF/WAVE absent")
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if chunk_id == b"fmt ":
            if size < 16 or pos + 8 + 16 > len(data):
                raise ValueError("chunk fmt tronqué")
            return int.from_bytes(data[pos + 12 : pos + 16], "little")
        pos += 8 + size + (size % 2)
    raise ValueError("chunk fmt introuvable")


def pcm_duration_s(n_bytes: int, sample_rate: int) -> float:
    """Durée d'un PCM16 mono."""
    if sample_rate <= 0 or n_bytes < 0:
        raise ValueError("taille ou taux invalide")
    return n_bytes / (2 * sample_rate)


def fail(message: str) -> None:
    print(f"ECHEC : {message}", file=sys.stderr)
    sys.exit(1)


def speech_payload(response_format: str, **extra) -> dict:
    return {
        "model": "speech",
        "voice": VOICE,
        "input": TEXT,
        "response_format": response_format,
        **extra,
    }


def main() -> None:
    url = os.environ.get("LITELLM_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_KEY", "")
    if not url or not key:
        fail("les variables LITELLM_URL et LITELLM_KEY doivent être définies.")
    headers = {"Authorization": f"Bearer {key}"}

    with httpx.Client(timeout=120) as client:
        # 1. WAV complet
        try:
            r = client.post(
                f"{url}/audio/speech", headers=headers, json=speech_payload("wav")
            )
            r.raise_for_status()
            wav = r.content
            rate = wav_sample_rate(wav)
        except (httpx.HTTPError, ValueError) as e:
            fail(f"appel speech en wav : {type(e).__name__} {e}")
        print(f"1. speech wav : {len(wav)} octets, taux d'échantillonnage {rate} Hz")

        # 2. PCM en flux
        try:
            t0 = time.monotonic()
            first = None
            chunks = 0
            total = 0
            with client.stream(
                "POST",
                f"{url}/audio/speech",
                headers=headers,
                json=speech_payload("pcm", stream_format="audio"),
            ) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_raw():
                    if not chunk:
                        continue
                    if first is None:
                        first = time.monotonic() - t0
                    chunks += 1
                    total += len(chunk)
            elapsed = time.monotonic() - t0
        except httpx.HTTPError as e:
            fail(f"appel speech en pcm : {type(e).__name__} {e}")
        if first is None:
            fail("appel speech en pcm : aucun octet reçu.")
        print(
            f"2. speech pcm : premier octet {first:.2f} s, total {elapsed:.2f} s, "
            f"{chunks} morceaux, {total} octets, "
            f"{pcm_duration_s(total, rate):.2f} s d'audio (à {rate} Hz)"
        )
        if chunks == 1:
            print("ATTENTION : l'audio est arrivé d'un seul bloc, pas de flux.")

        # 3. Transcription du WAV de l'appel 1
        try:
            r = client.post(
                f"{url}/audio/transcriptions",
                headers=headers,
                data={"model": "transcribe"},
                files={"file": ("essai.wav", wav, "audio/wav")},
            )
            r.raise_for_status()
            text = r.json().get("text", "")
        except (httpx.HTTPError, ValueError) as e:
            fail(f"appel transcriptions : {type(e).__name__} {e}")
        ok = "essai" in text.lower()
        print(f"3. transcription : {text!r} {'OK' if ok else ''}".rstrip())
        if not ok:
            fail("la transcription ne contient pas « essai ».")


if __name__ == "__main__":
    main()
