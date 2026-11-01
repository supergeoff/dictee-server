# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "websockets==17.4"]
# ///
"""Vérifications de la section 12 de la spec sur le vrai NeMo-Speech.cpp.

Prérequis : `make nemo-up` (NeMo répond sur http://127.0.0.1:18001).
Usage : uv run --script integration/check_nemo.py [--only a,b,c]

Chaque contrôle affiche une ligne `OK` ou `ÉCHEC`. Les relevés qui figent le protocole
vont dans dictee/tests/fixtures/nemo/ ; les fichiers audio produits vont dans .integration/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nemo_rt  # noqa: E402

ROOT = nemo_rt.ROOT
WORK = ROOT / ".integration"
FIXTURES = ROOT / "dictee" / "tests" / "fixtures" / "nemo"
RATE = 16000
PHRASE_1 = ("Bonjour, je teste la transcription de Nemotron et de Sortformer ce matin.", "Sofia")
PHRASE_2 = ("Très bien, parlons maintenant de la synthèse vocale en français.", "Leo")
BOOST_PHRASES = ["Nemotron", "Sortformer"]
BOOST = 3.0
LONG_SECONDS = 10 * 60
HOUR_SECONDS = 60 * 60
MEMORY_LIMIT = 3 * 1024**3
VOICES = {"John", "Sofia", "Aria", "Jason", "Leo"}

results: list[tuple[str, bool, str]] = []


def report(letter: str, title: str, ok: bool, detail: str) -> None:
    results.append((letter, ok, detail))
    print(f"{'OK' if ok else 'ÉCHEC'} {letter}. {title} : {detail}", flush=True)


def note(text: str) -> None:
    print(f"   {text}", flush=True)


def compose_base() -> list[str]:
    """Le binaire compose : `COMPOSE` s'il est fixé, sinon le plugin `docker compose`, sinon `docker-compose`."""
    if os.environ.get("COMPOSE"):
        return os.environ["COMPOSE"].split()
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"]


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    """Lance docker compose (plugin) ou docker-compose (binaire seul) sur les deux fichiers."""
    cmd = [*compose_base(), "-f", "compose.yaml", "-f", "compose.integration.yaml", *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def nemo_logs() -> str:
    out = compose("logs", "--no-color", "nemo")
    return out.stdout + out.stderr


def nemo_container() -> str:
    return compose("ps", "-q", "nemo").stdout.strip()


def wait_ready(timeout: float = 300) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            response = httpx.get(f"{nemo_rt.BASE_URL}/ready", timeout=5)
            if response.status_code == 200 and response.json().get("ready"):
                return response.json()
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit("NeMo ne répond pas sur /ready (lancer `make nemo-up`)")
        time.sleep(2)


def silence(seconds: float) -> bytes:
    return bytes(int(RATE * seconds) * 2)


def repeat_to(pcm: bytes, seconds: float, gap: float = 1.0) -> bytes:
    """Répète `pcm`, séparé par `gap` seconde de silence, sans dépasser `seconds`.

    NeMo refuse un audio de plus d'une heure (« audio exceeds configured limit » à 3606 s),
    d'où l'arrondi vers le bas.
    """
    unit = pcm + silence(gap)
    return unit * max(1, int(seconds * RATE * 2 // len(unit)))


def duration(pcm: bytes) -> float:
    return len(pcm) / (RATE * 2)


def prepare_audio() -> bytes:
    """Synthétise les deux phrases (WAV 16 kHz), écrit les fichiers et rend le PCM du dialogue."""
    WORK.mkdir(exist_ok=True)
    pcm = []
    for index, (text, voice) in enumerate((PHRASE_1, PHRASE_2), start=1):
        started = time.monotonic()
        wav = nemo_rt.synth_wav(text, voice, RATE)
        (WORK / f"phrase{index}.wav").write_bytes(wav)
        rate, data = nemo_rt.parse_wav(wav)
        assert rate == RATE, f"phrase {index} à {rate} Hz au lieu de {RATE} Hz"
        pcm.append(data)
        note(f"phrase {index} ({voice}) : {duration(data):.1f} s d'audio en {time.monotonic() - started:.1f} s")
    dialogue = pcm[0] + silence(1.0) + pcm[1]
    (WORK / "dialogue.wav").write_bytes(nemo_rt.wav_file(dialogue, RATE))
    note(f"dialogue : {duration(dialogue):.1f} s")
    return dialogue


def write_jsonl(path: Path, items: list[dict]) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items))


def write_json(path: Path, data: object) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def check_realtime(dialogue: bytes) -> None:
    """Contrôles a, b et c : une même session temps réel."""
    info: dict = {}
    events = nemo_rt.realtime(dialogue, RATE, BOOST_PHRASES, BOOST, language="fr-FR", info=info)
    write_jsonl(FIXTURES / "realtime_events.jsonl", events)
    kinds = [event.get("type") for event in events]
    note(f"{len(events)} événements en {info['seconds']:.1f} s : {', '.join(dict.fromkeys(kinds))}")

    finals = [e for e in events if e.get("type") == "conversation.item.input_audio_transcription.completed"]
    timed = [e for e in finals if e.get("words") and all("start" in w and "end" in w for w in e["words"])]
    last = events[-1].get("type") if events else None
    socket_state = f"fermé par NeMo (code {info['close_code']})" if info["ended_by"] == "closed" else "laissé ouvert par NeMo"
    closing = f"événement final {last}, socket {socket_state}"
    if timed:
        words = timed[0]["words"]
        report("a", "Temps réel", True, f"{len(finals)} final(s), {len(words)} mots horodatés, {closing}")
        note("exemple de mot : " + json.dumps(words[0], ensure_ascii=False, sort_keys=True))
        note("transcription : " + timed[0].get("transcript", ""))
    else:
        report("a", "Temps réel", False, f"aucun événement completed avec mots horodatés ({closing})")

    streaming = [line for line in nemo_logs().splitlines() if "mode=streaming" in line or "attention-right=13" in line]
    matched = [line for line in streaming if re.search(r"(?:attention-)?right=13\b", line)]
    if matched:
        report("b", "Contexte de 1,12 s", True, matched[0].split("|", 1)[-1].strip())
    else:
        report("b", "Contexte de 1,12 s", False, "aucune ligne de journal avec right=13 : " + " / ".join(streaming[-3:]))

    updated = [e for e in events if e.get("type") == "session.updated"]
    errors = [e for e in events if e.get("type") == "error"]
    disabled = [line for line in nemo_logs().splitlines() if "boosting disabled" in line]
    # Sans tokenizer SentencePiece dans le GGUF, NeMo accepte `speech_contexts` mais ne renforce rien.
    ok_c = bool(updated) and not errors and not disabled
    echo = json.dumps(updated[0].get("session", {}), ensure_ascii=False, sort_keys=True) if updated else "aucun session.updated"
    if disabled:
        report("c", "Renforcement", False, f"NeMo journalise que le renforcement est désactivé : {disabled[0].strip()}")
    else:
        report("c", "Renforcement", ok_c, f"session.updated sans error, écho {echo}" if ok_c else f"{echo} ; erreurs : {errors}")
    transcript = timed[0].get("transcript", "") if timed else ""
    note(f"mots renforcés reconnus : {[w for w in BOOST_PHRASES if w.lower() in transcript.lower()]}")


def check_speakers(dialogue: bytes) -> None:
    started = time.monotonic()
    response = httpx.post(
        f"{nemo_rt.BASE_URL}/v1/audio/diarizations",
        files={"file": ("dialogue.wav", nemo_rt.wav_file(dialogue, RATE), "audio/wav")},
        headers=nemo_rt.auth_headers(),
        timeout=600,
    )
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        report("d", "Locuteurs", False, f"HTTP {response.status_code} : {response.text[:200]}")
        return
    body = response.json()
    write_json(FIXTURES / "diarization.json", body)
    speakers = sorted({segment["speaker"] for segment in body.get("segments", [])})
    detail = f"{len(body.get('segments', []))} segments, locuteurs {speakers}, {elapsed:.1f} s"
    report("d", "Locuteurs", len(speakers) == 2, detail)
    for segment in body.get("segments", []):
        note(json.dumps(segment, sort_keys=True))


async def concurrent_sessions(dialogue: bytes) -> tuple[dict, dict, list[dict]]:
    long_pcm = repeat_to(dialogue, LONG_SECONDS)
    note(f"audio long : {duration(long_pcm) / 60:.1f} min, audio court : {duration(dialogue):.1f} s")
    long_info: dict = {}
    short_info: dict = {}
    origin = time.monotonic()
    long_task = asyncio.create_task(nemo_rt.realtime_async(long_pcm, RATE, [], 0.0, info=long_info))
    await asyncio.sleep(5)
    short_events = await nemo_rt.realtime_async(dialogue, RATE, [], 0.0, info=short_info)
    short_info["long_done_when_short_ended"] = long_task.done()
    await long_task
    short_info["offset"] = short_info["ended_at"] - origin
    long_info["offset"] = long_info["ended_at"] - origin
    return short_info, long_info, short_events


def check_concurrency(dialogue: bytes) -> None:
    short_info, long_info, short_events = asyncio.run(concurrent_sessions(dialogue))
    finals = [e for e in short_events if e.get("type") == "conversation.item.input_audio_transcription.completed"]
    ok = bool(finals) and short_info["ended_at"] < long_info["ended_at"] and not short_info["long_done_when_short_ended"]
    detail = (
        f"la courte finit à {short_info['offset']:.1f} s, la longue à {long_info['offset']:.1f} s "
        f"(la courte a duré {short_info['seconds']:.1f} s, la longue {long_info['seconds']:.1f} s)"
    )
    report("e", "Concurrence", ok, detail)


def parse_size(text: str) -> float:
    match = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"taille illisible : {text!r}")
    factor = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
    return float(match.group(1)) * factor[match.group(2).upper()]


def sample_memory(container: str, stop: threading.Event, samples: list[tuple[float, float]]) -> None:
    origin = time.monotonic()
    while not stop.is_set():
        started = time.monotonic()
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            samples.append((started - origin, parse_size(out.stdout.split("/")[0])))
        stop.wait(max(0.0, 2.0 - (time.monotonic() - started)))


def check_memory(dialogue: bytes) -> None:
    container = nemo_container()
    hour = repeat_to(dialogue, HOUR_SECONDS)
    path = WORK / "dialogue_60min.wav"
    path.write_bytes(nemo_rt.wav_file(hour, RATE))
    note(f"audio : {duration(hour) / 60:.1f} min, {path.stat().st_size / 1e6:.0f} Mo")
    samples: list[tuple[float, float]] = []
    stop = threading.Event()
    sampler = threading.Thread(target=sample_memory, args=(container, stop, samples), daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        with path.open("rb") as handle:
            response = httpx.post(
                f"{nemo_rt.BASE_URL}/v1/audio/diarizations",
                files={"file": (path.name, handle, "audio/wav")},
                headers=nemo_rt.auth_headers(),
                timeout=None,
            )
        status, body = response.status_code, response.text
    except httpx.HTTPError as error:
        status, body = 0, repr(error)
    finally:
        stop.set()
        sampler.join()
    elapsed = time.monotonic() - started
    if status != 200:
        note(f"réponse de NeMo : {body[:300]}")
    peak = max((size for _, size in samples), default=0.0)
    oom = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.OOMKilled}}", container], capture_output=True, text=True
    ).stdout.strip()
    segments = json.loads(body).get("segments", []) if status == 200 else []
    speakers = sorted({segment["speaker"] for segment in segments})
    detail = (
        f"pic {peak / 1024**3:.2f} Gio sur {len(samples)} relevés (limite 3 Gio), "
        f"{elapsed / 60:.1f} min, {len(segments)} segments, locuteurs {speakers}, HTTP {status}, OOMKilled={oom}"
    )
    report("f", "Mémoire", status == 200 and peak < MEMORY_LIMIT and oom != "true", detail)
    (WORK / "memory_samples.csv").write_text(
        "seconds,bytes\n" + "".join(f"{t:.1f},{size:.0f}\n" for t, size in samples)
    )


def check_voices() -> None:
    response = httpx.get(f"{nemo_rt.BASE_URL}/v1/models", headers=nemo_rt.auth_headers(), timeout=30)
    response.raise_for_status()
    body = response.json()
    write_json(FIXTURES / "models.json", body)
    speech = [m for m in body.get("data", []) if m.get("capability") == "speech"]
    voices = set(speech[0].get("voices", [])) if speech else set()
    started = time.monotonic()
    wav = nemo_rt.synth_wav(PHRASE_1[0], "Sofia")
    rate, pcm = nemo_rt.parse_wav(wav)
    detail = (
        f"voix {sorted(voices)}, synthèse sans sample_rate à {rate} Hz "
        f"({duration_at(pcm, rate):.1f} s d'audio en {time.monotonic() - started:.1f} s)"
    )
    report("g", "Voix et fréquence", voices == VOICES, detail)


def duration_at(pcm: bytes, rate: int) -> float:
    return len(pcm) / (rate * 2)


CHECKS = {
    "abc": lambda audio: check_realtime(audio),
    "d": lambda audio: check_speakers(audio),
    "e": lambda audio: check_concurrency(audio),
    "f": lambda audio: check_memory(audio),
    "g": lambda audio: check_voices(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="a,d,e,f,g", help="contrôles à lancer, par exemple a,d (a lance aussi b et c)")
    args = parser.parse_args()
    wanted = {part.strip() for part in args.only.split(",") if part.strip()}

    ready = wait_ready()
    version = httpx.get(f"{nemo_rt.BASE_URL}/version", timeout=10).text.strip()
    print(f"NeMo prêt : {ready} ; version : {version}", flush=True)
    dialogue = prepare_audio()
    for letters, run in CHECKS.items():
        if wanted & set(letters):
            run(dialogue)
    failed = [letter for letter, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)} contrôle(s) OK sur {len(results)}" + (f", ÉCHEC : {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
