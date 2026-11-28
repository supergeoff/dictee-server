# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1"]
# ///
"""Vérifications du serveur CrispASR seul, sur ses vrais modèles.

Prérequis : la stack de `make integration` est démarrée (CrispASR répond sur http://127.0.0.1:18002 et phonon sur
http://127.0.0.1:18003). Usage : uv run --script integration/check_crispasr.py [--only a,b,c]

L'audio d'essai n'est pas un fichier du dépôt : le serveur de voix de phonon dit deux phrases neutres, que le script
rééchantillonne à 16 kHz. La clé du serveur est la première de `CRISPASR_API_KEYS`, lue dans le `.env` (jamais
affichée). Aucun contrôle n'affiche de texte transcrit : seuls des comptes et des durées sont imprimés.
Chaque contrôle affiche une ligne `OK` ou `ÉCHEC`. Ce script remplace `check_nemo.py` à la tâche 14.
"""

from __future__ import annotations

import argparse
import array
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nemo_rt  # noqa: E402

BASE_URL = "http://127.0.0.1:18002"
VOICE_URL = "http://127.0.0.1:18003"
VOICE_RATE = 24000
RATE = 16000
PHRASES = [
    "Bonjour, je teste la transcription de CrispASR ce matin.",
    "Très bien, parlons maintenant de la synthèse vocale en français.",
]
TIMEOUT = 600.0
PROGRESS_POLL = 0.5


class Failure(Exception):
    """Un contrôle qui échoue, avec le message à afficher."""


def expect(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def resample(pcm: bytes, source: int, target: int) -> bytes:
    """Rééchantillonne du PCM16 mono par interpolation linéaire."""
    samples = array.array("h")
    samples.frombytes(pcm)
    count = len(samples) * target // source
    out = array.array("h")
    for index in range(count):
        position = index * source / target
        low = int(position)
        high = min(low + 1, len(samples) - 1)
        fraction = position - low
        out.append(round(samples[low] * (1 - fraction) + samples[high] * fraction))
    return out.tobytes()


def speak(text: str, voice: str) -> bytes:
    """Le PCM16 24 kHz que phonon rend pour `text`."""
    response = httpx.post(
        f"{VOICE_URL}/v1/audio/speech",
        json={"model": "tts-1", "input": text, "voice": voice, "response_format": "pcm"},
        timeout=300,
    )
    expect(response.status_code == 200, f"phonon : HTTP {response.status_code}")
    return response.content


def dialogue() -> bytes:
    """Les deux phrases séparées d'une seconde de silence, en WAV 16 kHz."""
    voice = nemo_rt.env_value("DEFAULT_VOICE")
    gap = bytes(RATE * 2)
    pcm = gap.join(resample(speak(text, voice), VOICE_RATE, RATE) for text in PHRASES)
    return nemo_rt.wav_file(pcm, RATE)


def headers() -> dict[str, str]:
    key = nemo_rt.env_value("CRISPASR_API_KEYS").split(",")[0].strip()
    return {"Authorization": f"Bearer {key}"}


def transcribe(wav: bytes, extra: dict[str, str] | None = None) -> httpx.Response:
    data = {"response_format": "verbose_json", "timestamp_granularities[]": "word", "language": "fr", **(extra or {})}
    return httpx.post(
        f"{BASE_URL}/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("audio.wav", wav, "audio/wav")},
        data=data,
        timeout=TIMEOUT,
    )


def segments_of(response: httpx.Response) -> list[dict]:
    expect(response.status_code == 200, f"HTTP {response.status_code}")
    segments = response.json().get("segments")
    expect(isinstance(segments, list) and segments, "aucun segment dans la réponse")
    return segments


def check_health(_audio: Callable[[], bytes]) -> str:
    response = httpx.get(f"{BASE_URL}/health", timeout=10)
    expect(response.status_code == 200, f"HTTP {response.status_code}")
    expect(response.json().get("status") == "ok", f"status {response.json().get('status')!r} au lieu de 'ok'")
    return "200, status ok"


def check_words(audio: Callable[[], bytes]) -> str:
    wav = audio()
    started = time.monotonic()
    segments = segments_of(transcribe(wav))
    words = [word for segment in segments for word in segment.get("words") or ()]
    expect(words, "aucun mot horodaté")
    expect(all("start" in w and "end" in w for w in words), "un mot sans start ou end")
    starts = [w["start"] for w in words]
    expect(starts == sorted(starts), "les mots ne sont pas dans l'ordre du temps")
    seconds = len(wav) / (RATE * 2)
    expect(words[-1]["end"] <= seconds + 1.0, f"fin du dernier mot ({words[-1]['end']:.1f} s) après l'audio")
    return (
        f"{len(segments)} segments, {len(words)} mots horodatés pour {seconds:.1f} s d'audio, "
        f"{time.monotonic() - started:.1f} s"
    )


def check_speakers(audio: Callable[[], bytes]) -> str:
    segments = segments_of(transcribe(audio(),{"diarize": "true", "diarize_method": "sortformer"}))
    speakers = {segment.get("speaker") for segment in segments}
    expect(None not in speakers, "un segment sans locuteur")
    return f"{len(segments)} segments, locuteurs {sorted(speakers)}"


def check_progress(audio: Callable[[], bytes]) -> str:
    """`GET /progress` répond pendant un fichier avec locuteurs : 0 à 100 au travail, -1 au repos."""
    wav = audio()
    seen: list[dict] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                response = httpx.get(f"{BASE_URL}/progress", headers=headers(), timeout=5)
                if response.status_code == 200:
                    seen.append(response.json())
            except httpx.HTTPError:
                pass
            stop.wait(PROGRESS_POLL)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        segments_of(transcribe(wav, {"diarize": "true", "diarize_method": "sortformer"}))
    finally:
        stop.set()
        thread.join(timeout=10)
    expect(seen, "/progress n'a jamais répondu 200")
    expect(all(isinstance(body.get("busy"), bool) for body in seen), "un relevé sans champ booléen `busy`")
    # Au repos le serveur répond {"busy": false, "progress": -1} ; 0 à 100 n'a de sens que pendant un travail.
    for body in seen:
        value = body.get("progress")
        ok = isinstance(value, (int, float)) and (0 <= value <= 100 if body["busy"] else value == -1)
        expect(ok, f"relevé incohérent : busy={body['busy']} progress={value!r}")
    busy = sum(1 for body in seen if body["busy"])
    return f"{len(seen)} relevés, busy vu {busy} fois (progress de 0 à 100), au repos progress vaut -1"


CHECKS: list[tuple[str, str, Callable[[Callable[[], bytes]], str]]] = [
    ("health", "GET /health", check_health),
    ("words", "Transcription avec mots horodatés", check_words),
    ("speakers", "Transcription avec locuteurs", check_speakers),
    ("progress", "GET /progress pendant un fichier avec locuteurs", check_progress),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    names = ", ".join(check[0] for check in CHECKS)
    parser.add_argument("--only", default="", help=f"contrôles à jouer, séparés par des virgules ({names})")
    args = parser.parse_args()
    only = {part.strip() for part in args.only.split(",") if part.strip()}
    unknown = only - {check[0] for check in CHECKS}
    if unknown:
        parser.error(f"contrôle inconnu : {', '.join(sorted(unknown))}")
    cache: list[bytes] = []

    def audio() -> bytes:
        """L'audio d'essai, demandé à phonon une seule fois et seulement quand un contrôle en a besoin."""
        if not cache:
            cache.append(dialogue())
        return cache[0]

    failed = False
    for key, title, func in CHECKS:
        if only and key not in only:
            continue
        started = time.monotonic()
        try:
            detail, ok = func(audio), True
        except Failure as error:
            detail, ok = str(error), False
        except Exception as error:  # un échec de transport ou du script compte comme un échec du contrôle
            detail, ok = f"{type(error).__name__} : {error}", False
        print(f"{'OK' if ok else 'ÉCHEC'} {title} ({time.monotonic() - started:.1f} s) : {detail[:500]}", flush=True)
        if not ok:
            failed = True
            break
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
