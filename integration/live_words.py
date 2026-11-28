# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Mesure combien de mots la dictée en direct de CrispASR rend par rapport à une transcription hors direct.

Le script tourne sur la machine hôte (la bibliothèque standard suffit) et lance CrispASR par la commande donnée en
`--command`, par exemple une commande `docker run --rm -i -v ... image crispasr`. Le dossier de l'audio doit être monté
au même chemin dans le conteneur : le script passe à CrispASR les mêmes chemins que les siens.

Usage :
    python3 integration/live_words.py --audio extrait.wav --model parakeet.gguf [--stream-length MS] [--stream-step MS]
        [--command "docker run --rm -i -v modeles:/models -v /chemin/audio:/chemin/audio image crispasr"]
        [--threads 4] [--language fr]

Passe en direct : `ffmpeg -re` envoie l'audio au rythme réel à `--stream --stream-json --vad`. Passe hors direct :
`--backend parakeet -otxt`. La sortie est faite de lignes `clé=valeur` et ne contient aucun texte transcrit. Le code de
sortie vaut 0 quand `ratio` atteint 0,95, 1 sinon, et 2 quand une commande échoue.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

SEUIL = 0.95


def count_words(text: str) -> int:
    """Compte les mots séparés par des espaces."""
    return len(text.split())


def parse_event(line: str) -> dict | None:
    """Lit une ligne d'événement de CrispASR ; renvoie None si ce n'est pas un objet JSON avec un type."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None
    return event


def event_delay(arrival_s: float, event: dict) -> float | None:
    """Délai d'un événement : instant d'arrivée moins l'instant d'audio `t1` qu'il couvre."""
    t1 = event.get("t1")
    if not isinstance(t1, (int, float)):
        return None
    return arrival_s - float(t1)


def percentile(values: list[float], fraction: float) -> float:
    """Percentile par interpolation linéaire ; 0 pour une liste vide."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def median(values: list[float]) -> float:
    return percentile(values, 0.5)


def ratio(final_words: int, offline_words: int) -> float:
    return final_words / offline_words if offline_words else 0.0


def stream_pass(args: argparse.Namespace) -> dict:
    """Joue l'audio en direct et rassemble les événements : mots des finals, délais, durée."""
    command = shlex.split(args.command) + [
        "--stream", "--stream-json", "--vad",
        "-m", args.model, "-l", args.language, "-t", str(args.threads),
    ]
    if args.stream_length is not None:
        command += ["--stream-length", str(args.stream_length)]
    if args.stream_step is not None:
        command += ["--stream-step", str(args.stream_step)]
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-re", "-i", args.audio, "-f", "s16le", "-ar", "16000", "-ac", "1", "-"],
        stdout=subprocess.PIPE,
    )
    start = time.monotonic()
    crispasr = subprocess.Popen(command, stdin=ffmpeg.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    ffmpeg.stdout.close()
    final_words = partial_events = final_events = 0
    partial_delays: list[float] = []
    final_delays: list[float] = []
    for line in crispasr.stdout:
        arrival = time.monotonic() - start
        event = parse_event(line)
        if event is None:
            continue
        delay = event_delay(arrival, event)
        if event["type"] == "partial":
            partial_events += 1
            if delay is not None:
                partial_delays.append(delay)
        elif event["type"] == "final":
            final_events += 1
            final_words += count_words(str(event.get("text", "")))
            if delay is not None:
                final_delays.append(delay)
    code = crispasr.wait()
    wall = time.monotonic() - start
    ffmpeg_code = ffmpeg.wait()
    if code != 0 or ffmpeg_code != 0:
        raise RuntimeError(f"la passe en direct a échoué (CrispASR : {code}, ffmpeg : {ffmpeg_code})")
    return {
        "final_words": final_words,
        "partial_events": partial_events,
        "final_events": final_events,
        "partial_delays": partial_delays,
        "final_delays": final_delays,
        "wall": wall,
    }


def offline_pass(args: argparse.Namespace) -> int:
    """Transcrit l'audio hors direct et renvoie le nombre de mots ; le fichier texte est effacé."""
    audio = Path(args.audio).resolve()
    prefix = audio.parent / f".live_words_{uuid.uuid4().hex}"
    out = Path(f"{prefix}.txt")
    command = shlex.split(args.command) + [
        "--backend", "parakeet", "-m", args.model, "-f", str(audio), "-l", args.language,
        "-t", str(args.threads), "-otxt", "-of", str(prefix),
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise RuntimeError(f"la passe hors direct a échoué (CrispASR : {result.returncode})")
        if not out.exists():
            raise RuntimeError("la passe hors direct n'a produit aucun fichier texte")
        return count_words(out.read_text(encoding="utf-8"))
    finally:
        out.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", required=True, help="WAV mono 16 kHz")
    parser.add_argument("--model", required=True, help="chemin du GGUF, tel que CrispASR le voit")
    parser.add_argument("--command", default="crispasr", help="commande qui lance CrispASR (découpée par shlex)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--language", default="fr")
    parser.add_argument("--stream-length", type=int, default=None, metavar="MS")
    parser.add_argument("--stream-step", type=int, default=None, metavar="MS")
    args = parser.parse_args(argv)
    args.audio = os.path.abspath(args.audio)
    try:
        live = stream_pass(args)
        offline_words = offline_pass(args)
    except (RuntimeError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    value = ratio(live["final_words"], offline_words)
    for key, val in (
        ("final_words", live["final_words"]),
        ("offline_words", offline_words),
        ("ratio", f"{value:.3f}"),
        ("partial_events", live["partial_events"]),
        ("final_events", live["final_events"]),
        ("partial_delay_median_s", f"{median(live['partial_delays']):.2f}"),
        ("partial_delay_p90_s", f"{percentile(live['partial_delays'], 0.9):.2f}"),
        ("final_delay_median_s", f"{median(live['final_delays']):.2f}"),
        ("final_delay_p90_s", f"{percentile(live['final_delays'], 0.9):.2f}"),
        ("stream_wall_s", f"{live['wall']:.1f}"),
    ):
        print(f"{key}={val}")
    return 0 if value >= SEUIL else 1


if __name__ == "__main__":
    sys.exit(main())
