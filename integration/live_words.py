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
        [--threads 4] [--language fr] [--verbose]

Passe en direct : le script convertit l'audio en PCM16 mono 16 kHz, lance CrispASR avec `--stream --stream-json --vad`
puis lui envoie lui-même le PCM par morceaux de 100 ms cadencés sur l'horloge. Du silence au rythme réel précède l'audio
jusqu'au premier événement de CrispASR (le modèle est alors chargé), et 2 s de silence le suivent. Passe hors direct :
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
import threading
import time
import uuid
from pathlib import Path

SEUIL = 0.95
CHUNK_S = 0.1
CHUNK_BYTES = int(16000 * CHUNK_S) * 2
LOAD_TIMEOUT_S = 120.0
TAIL_S = 2.0


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


def event_delay(arrival_s: float, event: dict, lead_in_s: float = 0.0) -> float | None:
    """Délai d'un événement : arrivée (comptée depuis le début de l'audio) moins l'instant d'audio `t1` qu'il couvre.

    Les `t1` de CrispASR comptent le silence d'amorce de durée `lead_in_s` : on le retranche. Renvoie None quand
    l'événement n'a pas de `t1` ou que son `t1` tombe dans le silence d'amorce.
    """
    t1 = event.get("t1")
    if not isinstance(t1, (int, float)) or float(t1) <= lead_in_s:
        return None
    return arrival_s - (float(t1) - lead_in_s)


def last_final_words(finals: list[dict]) -> dict:
    """Mots des textes finals par `utterance_id` : seul le dernier final d'un même identifiant compte."""
    words: dict = {}
    for position, event in enumerate(finals):
        key = event.get("utterance_id", f"sans-id-{position}")
        words[key] = count_words(str(event.get("text", "")))
    return words


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
    """Joue l'audio en direct à CrispASR, au rythme réel, et rassemble les événements.

    Le silence d'amorce dure jusqu'au premier événement de CrispASR, preuve que le modèle est chargé (au plus 120 s).
    Le délai d'un événement compte depuis le début de l'envoi de l'audio, pas depuis le lancement.
    """
    conversion = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", args.audio, "-f", "s16le", "-ar", "16000", "-ac", "1", "-"],
        capture_output=True,
    )
    if conversion.returncode != 0:
        raise RuntimeError(f"la conversion de l'audio a échoué (ffmpeg : {conversion.returncode})")
    pcm = conversion.stdout
    command = shlex.split(args.command) + [
        "--stream", "--stream-json", "--vad",
        "-m", args.model, "-l", args.language, "-t", str(args.threads),
    ]
    if args.stream_length is not None:
        command += ["--stream-length", str(args.stream_length)]
    if args.stream_step is not None:
        command += ["--stream-step", str(args.stream_step)]
    crispasr = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None if args.verbose else subprocess.DEVNULL
    )
    events: list[tuple[float, dict]] = []
    first_event = threading.Event()

    def read_events() -> None:
        for raw in crispasr.stdout:
            arrival = time.monotonic()
            event = parse_event(raw.decode("utf-8", errors="replace"))
            if event is not None:
                events.append((arrival, event))
                first_event.set()

    def send(data: bytes) -> None:
        try:
            crispasr.stdin.write(data)
            crispasr.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise RuntimeError("CrispASR a fermé son entrée avant la fin de l'envoi") from None

    def wait_until(instant: float) -> None:
        delay = instant - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    silence = bytes(CHUNK_BYTES)
    try:
        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        lead_origin = time.monotonic()
        sent = 0
        while not first_event.is_set():
            if crispasr.poll() is not None:
                raise RuntimeError(f"CrispASR s'est arrêté avant son premier événement ({crispasr.returncode})")
            if time.monotonic() - lead_origin > LOAD_TIMEOUT_S:
                raise RuntimeError(f"aucun événement de CrispASR en {LOAD_TIMEOUT_S:.0f} s : le modèle ne se charge pas")
            wait_until(lead_origin + sent * CHUNK_S)
            send(silence)
            sent += 1
        lead_in = sent * CHUNK_S
        audio_start = time.monotonic()
        chunks = [pcm[i:i + CHUNK_BYTES] for i in range(0, len(pcm), CHUNK_BYTES)]
        chunks += [silence] * int(TAIL_S / CHUNK_S)
        for index, chunk in enumerate(chunks):
            wait_until(audio_start + index * CHUNK_S)
            send(chunk)
        crispasr.stdin.close()
        reader.join()
        code = crispasr.wait()
        wall = time.monotonic() - audio_start
    finally:
        if crispasr.poll() is None:
            crispasr.kill()
            crispasr.wait()
    if code != 0:
        raise RuntimeError(f"la passe en direct a échoué (CrispASR : {code})")
    partial_delays: list[float] = []
    final_delays: list[float] = []
    finals: list[dict] = []
    partial_events = final_events = 0
    for arrival, event in events:
        delay = event_delay(arrival - audio_start, event, lead_in)
        if event["type"] == "partial":
            if delay is not None:
                partial_events += 1
                partial_delays.append(delay)
        elif event["type"] == "final":
            if delay is not None:
                final_events += 1
                final_delays.append(delay)
                finals.append(event)
    per_utterance = last_final_words(finals)
    return {
        "final_words": sum(per_utterance.values()),
        "final_utterances": len(per_utterance),
        "partial_events": partial_events,
        "final_events": final_events,
        "partial_delays": partial_delays,
        "final_delays": final_delays,
        "wall": wall,
        "lead_in": lead_in,
    }


def offline_pass(args: argparse.Namespace) -> int:
    """Transcrit l'audio hors direct et renvoie le nombre de mots ; le fichier texte est effacé s'il le peut."""
    audio = Path(args.audio).resolve()
    prefix = audio.parent / f".live_words_{uuid.uuid4().hex}"
    out = Path(f"{prefix}.txt")
    command = shlex.split(args.command) + [
        "--backend", "parakeet", "-m", args.model, "-f", str(audio), "-l", args.language,
        "-t", str(args.threads), "-otxt", "-of", str(prefix),
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=None if args.verbose else subprocess.DEVNULL
        )
        if result.returncode != 0:
            raise RuntimeError(f"la passe hors direct a échoué (CrispASR : {result.returncode})")
        if not out.exists():
            raise RuntimeError("la passe hors direct n'a produit aucun fichier texte")
        return count_words(out.read_text(encoding="utf-8"))
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError as exc:
            print(f"avertissement : le fichier texte temporaire n'a pas pu être effacé ({exc.strerror})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", required=True, help="WAV mono 16 kHz")
    parser.add_argument("--model", required=True, help="chemin du GGUF, tel que CrispASR le voit")
    parser.add_argument("--command", default="crispasr", help="commande qui lance CrispASR (découpée par shlex)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--language", default="fr")
    parser.add_argument("--stream-length", type=int, default=None, metavar="MS")
    parser.add_argument("--stream-step", type=int, default=None, metavar="MS")
    parser.add_argument("--verbose", action="store_true", help="laisse passer la sortie d'erreur de CrispASR")
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
        ("final_utterances", live["final_utterances"]),
        ("partial_delay_median_s", f"{median(live['partial_delays']):.2f}"),
        ("partial_delay_p90_s", f"{percentile(live['partial_delays'], 0.9):.2f}"),
        ("final_delay_median_s", f"{median(live['final_delays']):.2f}"),
        ("final_delay_p90_s", f"{percentile(live['final_delays'], 0.9):.2f}"),
        ("stream_wall_s", f"{live['wall']:.1f}"),
        ("lead_in_s", f"{live['lead_in']:.1f}"),
    ):
        print(f"{key}={val}")
    return 0 if value >= SEUIL else 1


if __name__ == "__main__":
    sys.exit(main())
