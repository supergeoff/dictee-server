# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Sonde des alias LiteLLM speech et transcribe.

Variables d'environnement : LITELLM_URL (finit par /v1) et LITELLM_KEY.
La clé et les en-têtes ne sont jamais affichés.

Voxtral n'accepte que mp3 et pcm via OpenRouter. Le PCM est du 16 bits signé
little-endian mono à 24 kHz (doc Mistral), ce que la transcription du WAV
reconstruit confirme.
"""

import io
import os
import sys
import time
import wave

import httpx

TEXT = "Bonjour, ceci est un essai de transcription."
VOICE = "fr_marie_neutral"
PCM_RATE = 24000


def wav_data_info(data: bytes) -> tuple[int, int, int, int]:
    """Renvoie (taux, canaux, bits par échantillon, octets de données) d'un WAV.

    La taille du chunk data est bornée par les octets réellement présents,
    ce qui couvre les WAV en flux dont la taille déclarée est fausse.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("en-tête RIFF/WAVE absent")
    fmt = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + 16 > len(data):
                raise ValueError("chunk fmt tronqué")
            channels = int.from_bytes(data[body + 2 : body + 4], "little")
            rate = int.from_bytes(data[body + 4 : body + 8], "little")
            bits = int.from_bytes(data[body + 14 : body + 16], "little")
            if rate <= 0 or channels <= 0 or bits <= 0:
                raise ValueError("chunk fmt invalide")
            fmt = (rate, channels, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("chunk data avant le chunk fmt")
            return (*fmt, min(size, len(data) - body))
        pos = body + size + (size % 2)
    if fmt is None:
        raise ValueError("chunk fmt introuvable")
    raise ValueError("chunk data introuvable")


def wav_sample_rate(data: bytes) -> int:
    """Lit le taux d'échantillonnage dans l'en-tête d'un WAV."""
    return wav_data_info(data)[0]


def pcm_duration_s(n_bytes: int, sample_rate: int) -> float:
    """Durée d'un PCM16 mono."""
    if sample_rate <= 0 or n_bytes < 0:
        raise ValueError("taille ou taux invalide")
    return n_bytes / (2 * sample_rate)


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Enveloppe un PCM 16 bits mono dans un WAV."""
    if sample_rate <= 0:
        raise ValueError("taux invalide")
    if len(pcm) % 2:
        raise ValueError("nombre d'octets impair pour du PCM 16 bits")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def fail(message: str) -> None:
    print(f"ECHEC : {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    url = os.environ.get("LITELLM_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_KEY", "")
    if not url or not key:
        fail("les variables LITELLM_URL et LITELLM_KEY doivent être définies.")
    headers = {"Authorization": f"Bearer {key}"}

    with httpx.Client(timeout=120) as client:
        # 1. PCM en flux, sans compression pour compter les vrais octets
        payload = {
            "model": "speech",
            "voice": VOICE,
            "input": TEXT,
            "response_format": "pcm",
            "stream_format": "audio",
        }
        try:
            t0 = time.monotonic()
            first = None
            parts = []
            with client.stream(
                "POST",
                f"{url}/audio/speech",
                headers={**headers, "Accept-Encoding": "identity"},
                json=payload,
            ) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    if first is None:
                        first = time.monotonic() - t0
                    parts.append(chunk)
            elapsed = time.monotonic() - t0
        except httpx.HTTPError as e:
            fail(f"appel speech en pcm : {type(e).__name__} {e}")
        if first is None:
            fail("appel speech en pcm : aucun octet reçu.")
        chunks = len(parts)
        pcm = b"".join(parts)
        ratio_time = first / elapsed if elapsed > 0 else 1.0
        print(
            f"1. speech pcm : premier octet {first:.2f} s, total {elapsed:.2f} s "
            f"(premier/total {ratio_time:.2f}), {chunks} morceaux, {len(pcm)} octets, "
            f"{pcm_duration_s(len(pcm), PCM_RATE):.2f} s d'audio à {PCM_RATE} Hz"
        )
        if chunks == 1 or ratio_time > 0.9:
            print("ATTENTION : l'audio semble arriver d'un seul bloc, pas de flux.")

        # 2. Transcription du WAV construit à partir du PCM
        try:
            wav = pcm_to_wav(pcm, PCM_RATE)
        except ValueError as e:
            fail(f"construction du wav : {e}")
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
        print(f"2. transcription : {text!r} {'OK' if ok else ''}".rstrip())
        if not ok:
            fail("la transcription ne contient pas « essai ».")


if __name__ == "__main__":
    main()
