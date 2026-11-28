# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "websockets==17.4"]
# ///
"""Vérifications de la section 12 de la spec sur le vrai NeMo-Speech.cpp.

Prérequis : `NEMO_VERSION=<version> make nemo-up` (NeMo répond sur http://127.0.0.1:18001).
Usage : uv run --script integration/check_nemo.py [--only a,b,c] [--note note.wav]

Chaque contrôle affiche une ligne `OK` ou `ÉCHEC`. Les relevés qui figent le protocole
vont dans dictee/tests/fixtures/nemo/ ; les fichiers audio produits vont dans .integration/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nemo_rt  # noqa: E402
from common import compose, compose_base  # noqa: E402,F401

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
MEETING_SECONDS = 2 * 60 * 60
REST_CHUNK_SECONDS = 30
CONCURRENT_REQUESTS = 16
COMPLETED = "conversation.item.input_audio_transcription.completed"
COMMITTED = "input_audio_buffer.committed"
MEMORY_LIMIT = 3 * 1024**3
# PCM 16 kHz de `--note` (un enregistrement réel) : il sert aux mesures, jamais aux relevés, et son texte
# n'est ni gardé ni affiché.
NOTE_PCM: bytes | None = None
VOICES = {"John", "Sofia", "Aria", "Jason", "Leo"}
# Contrôle k : deux phrases anglaises, et les mots (sans accent) que chaque langue doit rendre.
EN_PHRASE_1 = ("Good morning, this is a test of the automatic language detection.", "John")
EN_PHRASE_2 = ("Thank you, the meeting will continue in English this afternoon.", "Aria")
FR_KEYWORDS = ["bonjour", "transcription", "synthese", "francais"]
EN_KEYWORDS = ["morning", "language", "meeting", "afternoon"]
LANGUAGE_CASES = ("auto", "fr-FR", "en-US")
# Au changement de langue, NeMo perd parfois les premiers mots de la nouvelle langue : il suffit d'en retrouver les
# trois quarts. Un audio mêlé de 20 s varie trop d'un passage à l'autre pour le mesurer : le contrôle k2 compte les
# mots-clés d'un bloc d'anglais de 32 s, puis d'une session français, anglais, français de 96 s.
BLOCK_REPEATS = 3
MIN_SHARE = 0.75
SHORT_FRENCH = [
    ("D'accord, merci.", "Sofia"),
    ("Oui, je vois.", "Leo"),
    ("Rendez-vous demain matin.", "Sofia"),
    ("Peux-tu m'envoyer le document ?", "Leo"),
    ("Non, pas maintenant.", "Leo"),
]
NOTE_LANGUAGE_SECONDS = 30

results: list[tuple[str, bool, str]] = []


def report(letter: str, title: str, ok: bool, detail: str) -> None:
    results.append((letter, ok, detail))
    print(f"{'OK' if ok else 'ÉCHEC'} {letter}. {title} : {detail}", flush=True)


def note(text: str) -> None:
    print(f"   {text}", flush=True)


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


def fill_to(pcm: bytes, seconds: float, gap: float = 1.0) -> bytes:
    """Répète `pcm`, séparé par `gap` seconde de silence, puis coupe à `seconds` exactement."""
    unit = pcm + silence(gap)
    size = int(seconds * RATE) * 2
    return (unit * (size // len(unit) + 1))[:size]


def rest_form(phrases: list[str], boost: float) -> dict[str, str]:
    form = {"language": "fr-FR", "response_format": "verbose_json", "timestamp_granularities[]": "word"}
    if phrases:
        form["speech_contexts"] = json.dumps([{"phrases": phrases, "boost": boost}])
    return form


def rest_files(pcm: bytes) -> dict:
    return {"file": ("chunk.wav", nemo_rt.wav_file(pcm, RATE), "audio/wav")}


def rest_transcribe(pcm: bytes, phrases: list[str], boost: float) -> tuple[float, httpx.Response]:
    """Un morceau sur la route REST de NeMo ; rend la durée de la requête et la réponse."""
    started = time.monotonic()
    response = httpx.post(
        f"{nemo_rt.BASE_URL}/v1/audio/transcriptions",
        data=rest_form(phrases, boost),
        files=rest_files(pcm),
        headers=nemo_rt.auth_headers(),
        timeout=600,
    )
    return time.monotonic() - started, response


async def concurrent_rest(pcm: bytes, count: int) -> list:
    async with httpx.AsyncClient(timeout=600) as client:

        async def one():
            return await client.post(
                f"{nemo_rt.BASE_URL}/v1/audio/transcriptions",
                data=rest_form([], 0.0),
                files=rest_files(pcm),
                headers=nemo_rt.auth_headers(),
            )

        return await asyncio.gather(*(one() for _ in range(count)), return_exceptions=True)


def check_rest_chunk(dialogue: bytes) -> None:
    """Contrôle h : un morceau de 30 s sur la route REST, puis la Note et une file pleine."""
    chunk = fill_to(dialogue, REST_CHUNK_SECONDS)
    elapsed, response = rest_transcribe(chunk, BOOST_PHRASES, 1.0)
    if response.status_code != 200:
        report("h", "Morceau REST", False, f"HTTP {response.status_code} : {response.text[:300]}")
        return
    body = response.json()
    write_json(FIXTURES / "rest_transcription.json", body)
    words = body.get("words") or []
    ok = bool(words) and all({"word", "start", "end"} <= set(word) for word in words)
    keys = sorted(words[0]) if words else []
    report("h", "Morceau REST", ok, f"{duration(chunk):.0f} s en {elapsed:.1f} s, {len(words)} mots, clés {keys}")
    note("transcription du dialogue de synthèse : " + body.get("text", ""))
    sample = NOTE_PCM[: REST_CHUNK_SECONDS * RATE * 2] if NOTE_PCM else chunk
    if NOTE_PCM:
        for attempt in range(1, 4):
            elapsed, response = rest_transcribe(sample, [], 0.0)
            note(f"Note, 30 premières secondes, essai {attempt} : HTTP {response.status_code} en {elapsed:.1f} s")
    started = time.monotonic()
    answers = asyncio.run(concurrent_rest(sample, CONCURRENT_REQUESTS))
    statuses: dict[str, int] = {}
    refused = None
    for answer in answers:
        label = str(answer.status_code) if isinstance(answer, httpx.Response) else type(answer).__name__
        statuses[label] = statuses.get(label, 0) + 1
        if refused is None and isinstance(answer, httpx.Response) and answer.status_code != 200:
            refused = answer
    note(f"{CONCURRENT_REQUESTS} requêtes simultanées en {time.monotonic() - started:.1f} s : {statuses}")
    if refused is not None:
        note(f"première réponse refusée : HTTP {refused.status_code} {refused.text[:300]}")


def speaker_key(word: dict) -> str | None:
    """Le champ qui porte la personne d'un mot : `speaker` s'il existe, sinon le premier champ qui la nomme."""
    if "speaker" in word:
        return "speaker"
    return next((key for key in word if "speaker" in key), None)


def check_meeting(dialogue: bytes) -> None:
    """Contrôle i : une réunion de 2 heures en temps réel avec personnes, puis le relevé du dialogue."""
    session = {"sample_rate": RATE, "language": "fr-FR", "word_timestamps": True, "speaker_diarization": True}
    events, _ = asyncio.run(nemo_rt.scripted_session(session, [("audio", dialogue, False), ("commit",)]))
    write_jsonl(FIXTURES / "realtime_speakers_events.jsonl", [event for _, event in events])
    words = [w for _, e in events if e.get("type") == COMPLETED for w in e.get("words") or []]
    key = next((speaker_key(word) for word in words if speaker_key(word)), None)
    note(f"dialogue : clé de personne {key!r}, premier mot {json.dumps(words[0], ensure_ascii=False) if words else '-'}")
    if NOTE_PCM is None:
        report("i", "Réunion de 2 heures", False, "--note manquant")
        return
    meeting = fill_to(NOTE_PCM, MEETING_SECONDS)
    events, info = asyncio.run(
        nemo_rt.scripted_session(session, [("audio", meeting, False), ("commit",)], idle_timeout=MEETING_SECONDS)
    )
    errors = [e for _, e in events if e.get("type") == "error"]
    finals = [(t, e) for t, e in events if e.get("type") == COMPLETED]
    commit_at = info["commit_times"][0] if info["commit_times"] else float("nan")
    final_at = finals[-1][0] if finals else float("nan")
    final_size = max((len(json.dumps(e, ensure_ascii=False).encode()) for _, e in finals), default=0)
    words = [w for _, e in finals for w in e.get("words") or []]
    key = next((speaker_key(word) for word in words if speaker_key(word)), None)
    speakers = sorted({str(word[key]) for word in words if key and key in word})
    del events, finals  # aucun texte de la Note n'est gardé
    detail = (
        f"{duration(meeting) / 60:.0f} min d'audio en {info['seconds'] / 60:.1f} min ; "
        f"du commit au final {final_at - commit_at:.0f} s ; plus long silence {info['max_gap']:.0f} s ; "
        f"événement final {final_size / 1e6:.2f} Mo ; {len(words)} mots ; clé {key!r} ; personnes {speakers} ; "
        f"fin {info['ended_by']}"
    )
    report("i", "Réunion de 2 heures", bool(words) and key is not None and len(speakers) >= 2 and not errors, detail)
    if errors:
        note(f"erreur de NeMo : {errors[0].get('error')}")


def check_turns() -> None:
    """Contrôle j : deux commits dans une session, puis la finalisation seule avec `endpointing_ms`."""
    first = nemo_rt.parse_wav((WORK / "phrase1.wav").read_bytes())[1]
    second = nemo_rt.parse_wav((WORK / "phrase2.wav").read_bytes())[1]
    session = {"sample_rate": RATE, "language": "fr-FR", "word_timestamps": True}
    steps = [("audio", first, False), ("commit",), ("wait", COMMITTED), ("audio", second, False), ("commit",)]
    events, info = asyncio.run(nemo_rt.scripted_session(session, steps, idle_timeout=60))
    write_jsonl(FIXTURES / "realtime_multiturn_events.jsonl", [event for _, event in events])
    kinds = [event.get("type") for _, event in events]
    errors = [event.get("error", {}).get("code") for _, event in events if event.get("type") == "error"]
    many = kinds.count(COMPLETED) >= 2 and kinds.count(COMMITTED) >= 2
    note(
        f"deux commits : {kinds.count(COMPLETED)} completed, {kinds.count(COMMITTED)} committed, "
        f"fin {info['ended_by']}, erreurs {errors}"
    )
    endpointing = {**session, "endpointing_ms": 500}
    steps = [
        ("audio", first, True),
        ("audio", silence(2.0), True),
        ("audio", second, True),
        ("audio", silence(2.0), True),
        ("sleep", 10.0),
        ("commit",),
    ]
    events_vad, info_vad = asyncio.run(nemo_rt.scripted_session(endpointing, steps, idle_timeout=60))
    write_jsonl(FIXTURES / "realtime_endpointing_events.jsonl", [event for _, event in events_vad])
    commit_at = info_vad["commit_times"][0] if info_vad["commit_times"] else float("inf")
    before = [t for t, event in events_vad if event.get("type") == COMPLETED and t < commit_at]
    errors_vad = [event.get("error", {}).get("code") for _, event in events_vad if event.get("type") == "error"]
    vad = len(before) >= 2
    note(
        f"endpointing_ms=500 : {len(before)} completed avant le commit (à {', '.join(f'{t:.1f} s' for t in before)}), "
        f"commit à {commit_at:.1f} s, fin {info_vad['ended_by']}, erreurs {errors_vad}"
    )
    report("j", "Prises de parole", not errors and not errors_vad, f"many_commits={many}, server_vad={vad}")


def plain_words(text: str) -> list[str]:
    """Les mots d'un texte, en minuscules et sans accents, pour y chercher des mots-clés."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return re.findall(r"[a-z']+", "".join(char for char in decomposed if not unicodedata.combining(char)))


def keywords_found(text: str, keywords: list[str]) -> list[str]:
    words = set(plain_words(text))
    return [keyword for keyword in keywords if keyword in words]


def occurrences(text: str, keywords: list[str]) -> int:
    """Combien de fois les mots-clés apparaissent dans le texte, répétitions comprises."""
    words = plain_words(text)
    return sum(words.count(keyword) for keyword in keywords)


def synth_pcm(phrase: tuple[str, str], language: str) -> bytes:
    """Le PCM 16 kHz d'une phrase dite par NeMo dans la langue donnée."""
    text, voice = phrase
    rate, data = nemo_rt.parse_wav(nemo_rt.synth_wav(text, voice, RATE, language))
    assert rate == RATE, f"phrase à {rate} Hz au lieu de {RATE} Hz"
    return data


def rest_text(pcm: bytes, language: str) -> tuple[int, str]:
    """Le statut HTTP et le texte de la route REST de NeMo pour cet audio et cette langue."""
    form = rest_form([], 0.0) | {"language": language}
    response = httpx.post(
        f"{nemo_rt.BASE_URL}/v1/audio/transcriptions",
        data=form,
        files=rest_files(pcm),
        headers=nemo_rt.auth_headers(),
        timeout=600,
    )
    return response.status_code, (response.json().get("text", "") if response.status_code == 200 else "")


def session_text(pcm: bytes, language: str) -> tuple[list[str], str]:
    """Les types d'événements et le texte d'une session temps réel avec personnes."""
    session = {"sample_rate": RATE, "language": language, "word_timestamps": True, "speaker_diarization": True}
    events, _ = asyncio.run(nemo_rt.scripted_session(session, [("audio", pcm, False), ("commit",)]))
    kinds = [event.get("type", "?") for _, event in events]
    return kinds, " ".join(event.get("transcript", "") for _, event in events if event.get("type") == COMPLETED)


def check_language() -> None:
    """Contrôle k : NeMo détecte la langue (`auto`) sur la route REST et sur la session avec personnes.

    Trois audios de synthèse, français, anglais et mêlés, passent dans chaque langue. Seuls des comptes sont
    affichés : mots-clés retrouvés par langue et nombre de mots, jamais le texte.
    """
    fr1, fr2 = synth_pcm(PHRASE_1, "fr-FR"), synth_pcm(PHRASE_2, "fr-FR")
    en1, en2 = synth_pcm(EN_PHRASE_1, "en-US"), synth_pcm(EN_PHRASE_2, "en-US")
    gap = silence(1.0)
    audios = {
        "french": fr1 + gap + fr2,
        "english": en1 + gap + en2,
        "mixed": fr1 + gap + en1 + gap + fr2 + gap + en2,
    }
    # (audio, langue, route) -> (mots-clés français, mots-clés anglais)
    found: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {}
    accepted = True
    for name, pcm in audios.items():
        (WORK / f"langue-{name}.wav").write_bytes(nemo_rt.wav_file(pcm, RATE))
        for language in LANGUAGE_CASES:
            status, rest = rest_text(pcm, language)
            kinds, live = session_text(pcm, language)
            for route, text in (("REST", rest), ("session", live)):
                found[(name, language, route)] = (keywords_found(text, FR_KEYWORDS), keywords_found(text, EN_KEYWORDS))
                french, english = found[(name, language, route)]
                words = len(plain_words(text))
                note(f"{name} / {language} / {route} : {len(french)} mot(s)-clé(s) français, {len(english)} anglais, {words} mots")
            note(f"{name} / {language} : REST HTTP {status}, session {kinds[0] if kinds else '-'} ... {kinds[-1] if kinds else '-'}")
            if language == "auto":
                accepted = accepted and status == 200 and "session.updated" in kinds and kinds[-1:] == [COMMITTED]
                accepted = accepted and "error" not in kinds
    report("k1", "auto accepté", accepted, "route REST en 200 et session jusqu'à committed, sans error, avec auto")

    french_block = (fr1 + gap + fr2 + gap) * BLOCK_REPEATS
    english_block = (en1 + gap + en2 + gap) * BLOCK_REPEATS
    total = len(EN_KEYWORDS) * BLOCK_REPEATS
    needed = math.ceil(total * MIN_SHARE)
    rest_auto = occurrences(rest_text(english_block, "auto")[1], EN_KEYWORDS)
    rest_fixed = occurrences(rest_text(english_block, "fr-FR")[1], EN_KEYWORDS)
    meeting = french_block + english_block + french_block
    session_auto = occurrences(session_text(meeting, "auto")[1], EN_KEYWORDS)
    session_fixed = occurrences(session_text(meeting, "fr-FR")[1], EN_KEYWORDS)
    note(f"bloc anglais de {duration(english_block):.0f} s en REST : {rest_auto} mots-clés sur {total} en auto, {rest_fixed} en fr-FR")
    note(f"session de {duration(meeting):.0f} s, français puis anglais puis français : {session_auto} en auto, {session_fixed} en fr-FR")
    both = rest_auto >= needed and rest_auto > rest_fixed and session_auto >= needed and session_auto > session_fixed
    detail = f"auto retrouve au moins {needed} mots-clés anglais sur {total}, et plus qu'avec fr-FR, en REST et en session"
    report("k2", "deux langues", both, detail)

    start = all("morning" in found[("english", "auto", route)][1] for route in ("REST", "session"))
    report("k3", "départ en anglais", start, "audio anglais avec auto : le premier mot-clé est retrouvé, par les deux routes")

    same = True
    for index, pcm in enumerate((fr1, fr2), start=1):
        automatic, fixed = rest_text(pcm, "auto")[1], rest_text(pcm, "fr-FR")[1]
        same = same and len(plain_words(automatic)) == len(plain_words(fixed))
        note(f"phrase française {index} : {len(plain_words(automatic))} mots en auto, {len(plain_words(fixed))} en fr-FR")
    for phrase in SHORT_FRENCH:
        pcm = synth_pcm(phrase, "fr-FR")
        automatic, fixed = rest_text(pcm, "auto")[1], rest_text(pcm, "fr-FR")[1]
        equal = plain_words(automatic) == plain_words(fixed)
        same = same and equal
        note(f"énoncé français de {duration(pcm):.1f} s : {'identique' if equal else 'différent'} en auto et en fr-FR")
        if not equal:
            # Les énoncés sont de synthèse : leur texte peut s'afficher pour voir ce qui diffère.
            note(f"  auto : {automatic}")
            note(f"  fr-FR : {fixed}")
    detail = "deux phrases de même longueur, et chaque énoncé court identique, avec auto et avec fr-FR (REST)"
    report("k4", "dictées françaises", same, detail)

    if NOTE_PCM is not None:
        excerpt = NOTE_PCM[: NOTE_LANGUAGE_SECONDS * RATE * 2]
        counts = {language: len(plain_words(rest_text(excerpt, language)[1])) for language in ("auto", "fr-FR")}
        note(f"enregistrement réel, {NOTE_LANGUAGE_SECONDS} premières secondes : {counts} mots")


CHECKS = {
    "abc": lambda audio: check_realtime(audio),
    "d": lambda audio: check_speakers(audio),
    "e": lambda audio: check_concurrency(audio),
    "f": lambda audio: check_memory(audio),
    "g": lambda audio: check_voices(),
    "h": lambda audio: check_rest_chunk(audio),
    "i": lambda audio: check_meeting(audio),
    "j": lambda audio: check_turns(),
    "k": lambda audio: check_language(),
}


def main() -> int:
    global NOTE_PCM
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="a,d,e,f,g", help="contrôles à lancer, par exemple a,d (a lance aussi b et c)")
    parser.add_argument("--note", type=Path, help="WAV PCM16 mono 16 kHz d'un enregistrement réel, pour h et i")
    args = parser.parse_args()
    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    if args.note is not None:
        rate, NOTE_PCM = nemo_rt.parse_wav(args.note.read_bytes())
        if rate != RATE:
            raise SystemExit(f"--note doit être à {RATE} Hz, pas {rate} Hz")

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
