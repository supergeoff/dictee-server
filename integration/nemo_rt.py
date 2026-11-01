"""Client minimal de NeMo-Speech.cpp pour les vérifications d'intégration.

`synth` produit de l'audio par `/v1/audio/speech`, `realtime` joue une session
sur `/v1/audio/transcriptions/realtime`. `check_nemo.py` et, plus tard,
`boost.py` partagent ces deux fonctions. Le module vise le NeMo du compose
d'intégration (`http://127.0.0.1:18001`) et lit sa clé dans `.env`.

Dépendances : httpx, websockets (déclarées par les scripts qui l'importent).
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("NEMO_URL", "http://127.0.0.1:18001")


def api_key() -> str:
    """Lit NEMO_API_KEY dans l'environnement, sinon dans le .env du dépôt (un conteneur jetable n'a pas de .env)."""
    if os.environ.get("NEMO_API_KEY"):
        return os.environ["NEMO_API_KEY"]
    for line in (ROOT / ".env").read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "NEMO_API_KEY":
            return value.strip()
    raise RuntimeError("NEMO_API_KEY absente de .env (lancer `make nemo-up`)")


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}"}


def parse_wav(data: bytes) -> tuple[int, bytes]:
    """Rend (fréquence, PCM16 mono) d'un WAV PCM16, en lisant l'en-tête RIFF."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("réponse qui n'est pas un WAV")
    rate = channels = bits = 0
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            _fmt, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", data[body : body + 16])
        elif chunk_id == b"data":
            if (channels, bits) != (1, 16):
                raise ValueError(f"WAV attendu en mono PCM16, reçu {channels} canaux, {bits} bits")
            return rate, data[body : body + min(size, len(data) - body)]
        pos = body + size + (size & 1)
    raise ValueError("WAV sans bloc data")


def wav_rate(data: bytes) -> int:
    return parse_wav(data)[0]


def synth_wav(text: str, voice: str, rate: int | None = None, language: str = "fr-FR") -> bytes:
    """Rend le WAV de `/v1/audio/speech` ; sans `rate`, NeMo garde sa fréquence native."""
    body: dict[str, object] = {"input": text, "voice": voice, "language": language, "response_format": "wav"}
    if rate is not None:
        body["sample_rate"] = rate
    response = httpx.post(f"{BASE_URL}/v1/audio/speech", json=body, headers=auth_headers(), timeout=300)
    response.raise_for_status()
    return response.content


def synth(text: str, voice: str, rate: int) -> bytes:
    """Rend du PCM16 mono little-endian à `rate` Hz."""
    got_rate, pcm = parse_wav(synth_wav(text, voice, rate))
    if got_rate != rate:
        raise RuntimeError(f"fréquence demandée {rate} Hz, reçue {got_rate} Hz")
    return pcm


def wav_file(pcm: bytes, rate: int) -> bytes:
    """Enveloppe du PCM16 mono dans un en-tête WAV."""
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


async def realtime_async(
    pcm: bytes,
    rate: int,
    phrases: list[str],
    boost: float,
    *,
    language: str = "fr-FR",
    frame_seconds: float = 1.0,
    idle_timeout: float = 180.0,
    info: dict | None = None,
) -> list[dict]:
    """Joue une session temps réel et rend tous les événements reçus, dans l'ordre.

    La session s'arrête sur `input_audio_buffer.committed` (dernier événement de NeMo, qui
    laisse le socket ouvert) ou sur un événement `error`. `info` reçoit `seconds` (durée de la
    session), `ended_at` (horloge monotone de la fin), `ended_by` (type de l'événement final,
    `closed` si le serveur a fermé le socket, `idle` si plus rien n'est arrivé) ainsi que
    `close_code` et `close_reason` du socket.
    """
    url = BASE_URL.replace("http", "ws", 1) + "/v1/audio/transcriptions/realtime"
    update: dict[str, object] = {"sample_rate": rate, "language": language, "word_timestamps": True}
    if phrases:
        update["speech_contexts"] = [{"phrases": phrases, "boost": boost}]
    events: list[dict] = []
    started = time.monotonic()
    ended_by = "closed"
    # Pas de ping de maintien. Mesuré : NeMo lit les trames au rythme du traitement et ne répond
    # à un ping qu'après l'audio envoyé avant lui (pong au bout de 113 s sur 10 min d'audio),
    # donc le ping par défaut de websockets (20 s) coupe une session longue en 1011.
    async with connect(url, additional_headers=auth_headers(), max_size=None, ping_interval=None) as ws:
        updated = asyncio.Event()

        async def reader() -> None:
            nonlocal ended_by
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
                    except ConnectionClosed:
                        return
                    except asyncio.TimeoutError:
                        ended_by = "idle"
                        return
                    event = json.loads(raw)
                    events.append(event)
                    kind = event.get("type")
                    if kind in ("session.updated", "error"):
                        updated.set()
                    if kind in ("input_audio_buffer.committed", "error"):
                        # NeMo ne ferme pas le socket : après `committed`, la session est terminée.
                        ended_by = kind
                        return
            finally:
                updated.set()

        read_task = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "session.update", "session": update}))
        await asyncio.wait_for(updated.wait(), timeout=idle_timeout)
        if not read_task.done():  # sinon NeMo a déjà répondu `error` ou fermé le socket
            frame_bytes = int(rate * frame_seconds) * 2
            for start in range(0, len(pcm), frame_bytes):
                await ws.send(pcm[start : start + frame_bytes])
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await read_task
        if info is not None:
            info["close_code"] = ws.close_code
            info["close_reason"] = ws.close_reason
            info["ended_at"] = time.monotonic()
            info["seconds"] = info["ended_at"] - started
            info["ended_by"] = ended_by
    return events


def realtime(pcm: bytes, rate: int, phrases: list[str], boost: float, **options) -> list[dict]:
    """Version synchrone de `realtime_async`."""
    return asyncio.run(realtime_async(pcm, rate, phrases, boost, **options))


async def scripted_session(
    session: dict,
    steps: list[tuple],
    *,
    idle_timeout: float = 180.0,
    frame_seconds: float = 1.0,
) -> tuple[list[tuple[float, dict]], dict]:
    """Joue une session temps réel selon `steps` ; rend les événements reçus avec leur instant, et un bilan.

    `session` est le contenu de `session.update` (`sample_rate` compris). Étapes :
    `("audio", pcm, paced)` envoie le PCM par trames de `frame_seconds`, au rythme réel si `paced` ;
    `("commit",)` envoie `input_audio_buffer.commit` ; `("wait", type)` attend un nouvel événement de ce type ;
    `("sleep", secondes)` attend. La session finit quand les étapes sont jouées et qu'il est arrivé autant de
    `input_audio_buffer.committed` que de commits envoyés, ou sur un `error`, une fermeture ou `idle_timeout`
    secondes sans événement. Les instants sont comptés depuis l'ouverture. Le bilan porte `seconds`,
    `ended_by`, `commit_times` (instants des commits envoyés) et `max_gap` (plus long silence entre deux
    événements reçus, ou entre un commit envoyé et l'événement suivant).
    """
    url = BASE_URL.replace("http", "ws", 1) + "/v1/audio/transcriptions/realtime"
    rate = int(session["sample_rate"])
    events: list[tuple[float, dict]] = []
    commit_times: list[float] = []
    started = time.monotonic()
    state = {"ended_by": "closed", "committed": 0}
    arrived = asyncio.Condition()

    def now() -> float:
        return time.monotonic() - started

    async with connect(url, additional_headers=auth_headers(), max_size=None, ping_interval=None) as ws:

        async def reader() -> None:
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
                    except ConnectionClosed:
                        return
                    except asyncio.TimeoutError:
                        state["ended_by"] = "idle"
                        return
                    event = json.loads(raw)
                    async with arrived:
                        events.append((now(), event))
                        kind = event.get("type")
                        if kind == "input_audio_buffer.committed":
                            state["committed"] += 1
                        arrived.notify_all()
                    if kind == "error":
                        state["ended_by"] = "error"
                        return
                    if state.get("done") and state["committed"] >= len(commit_times):
                        state["ended_by"] = "committed"
                        return
            finally:
                async with arrived:
                    state["reader_done"] = True
                    arrived.notify_all()

        async def wait_for(kind: str) -> None:
            seen = sum(1 for _, event in events if event.get("type") == kind)
            async with arrived:
                await arrived.wait_for(
                    lambda: state.get("reader_done") or sum(1 for _, e in events if e.get("type") == kind) > seen
                )

        read_task = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "session.update", "session": session}))
        await wait_for("session.updated")
        frame_bytes = int(rate * frame_seconds) * 2
        try:
            for step in steps:
                if read_task.done():
                    break
                if step[0] == "audio":
                    _, pcm, paced = step
                    for start in range(0, len(pcm), frame_bytes):
                        sent = time.monotonic()
                        await ws.send(pcm[start : start + frame_bytes])
                        if paced:
                            await asyncio.sleep(max(0.0, frame_seconds - (time.monotonic() - sent)))
                elif step[0] == "commit":
                    commit_times.append(now())
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                elif step[0] == "wait":
                    await wait_for(step[1])
                elif step[0] == "sleep":
                    await asyncio.sleep(step[1])
        except ConnectionClosed:
            state["ended_by"] = "closed"
        state["done"] = True
        if not read_task.done() and state["committed"] >= len(commit_times):
            read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)

    instants = sorted([t for t, _ in events] + commit_times)
    gaps = [later - earlier for earlier, later in zip(instants, instants[1:])]
    info = {
        "seconds": now(),
        "ended_by": state["ended_by"],
        "commit_times": commit_times,
        "max_gap": max(gaps, default=0.0),
    }
    return events, info
