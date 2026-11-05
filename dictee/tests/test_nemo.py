import asyncio
import email
import email.policy
import io
import json
import logging
import socket
import struct
import time
import wave
from contextlib import aclosing
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from dictee import nemo as nemo_module
from dictee.nemo import (
    SPEAKER_KEY,
    TTS_SAMPLE_RATE,
    NemoClient,
    NemoError,
    NemoOverloaded,
    NemoUnavailable,
)
from dictee.types import Cleared, Committed, Final, Partial, Word

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "nemo"


def wav_bytes(seconds: float) -> bytes:
    """WAV PCM16 mono 16 kHz aux octets variés : une trame perdue ou déplacée change le contenu relu."""
    count = int(16000 * seconds)
    pcm = struct.pack(f"<{count}h", *((n * 37) % 20000 - 10000 for n in range(count)))
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return out.getvalue()


def write_wav(directory: Path, seconds: float) -> Path:
    """Écrit sur le disque le WAV de `wav_bytes`."""
    path = directory / f"audio-{seconds:g}s.wav"
    path.write_bytes(wav_bytes(seconds))
    return path


WAV_1S = wav_bytes(1.0)


@pytest.fixture
def wav_1s(tmp_path) -> Path:
    return write_wav(tmp_path, 1.0)


# Relevé de la session temps réel de la tâche 1 : session.created, session.updated, puis les événements qui
# suivent le commit (deltas, completed, committed).
RECORDED = [json.loads(line) for line in (FIXTURES / "realtime_events.jsonl").read_text().splitlines()]
CREATED, UPDATED, *AFTER_COMMIT = RECORDED
COMPLETED = next(event for event in AFTER_COMMIT if event["type"].endswith(".completed"))
COMMITTED = next(event for event in AFTER_COMMIT if event["type"] == "input_audio_buffer.committed")
# NeMo a rendu le final mais n'a pas annoncé la fin : la session reste ouverte.
HELD_BACK = [event for event in AFTER_COMMIT if event is not COMMITTED]

# Relevé de la même session avec `speaker_diarization` : les deux premiers événements sont ceux de l'ouverture, les
# suivants ceux d'après le commit. Chaque mot du `completed` porte sa personne, un entier numéroté à partir de 1.
SPEAKERS_RECORDED = [
    json.loads(line) for line in (FIXTURES / "realtime_speakers_events.jsonl").read_text().splitlines()
]
SPEAKERS_AFTER_COMMIT = SPEAKERS_RECORDED[2:]
SPEAKERS_COMPLETED = next(event for event in SPEAKERS_AFTER_COMMIT if event["type"].endswith(".completed"))


def split_after_commits(events: list[dict]) -> list[list[dict]]:
    """Coupe les événements d'après les commits en une liste par commit, chacune finie par son `committed`."""
    turns: list[list[dict]] = [[]]
    for event in events:
        turns[-1].append(event)
        if event["type"] == "input_audio_buffer.committed":
            turns.append([])
    return turns[:-1]


# Relevé d'une session à deux commits (tâche 1) : après l'ouverture, les événements de chaque commit, du premier delta
# à son `committed`.
MULTITURN_RECORDED = [
    json.loads(line) for line in (FIXTURES / "realtime_multiturn_events.jsonl").read_text().splitlines()
]
MULTITURN_AFTER_COMMITS = split_after_commits(MULTITURN_RECORDED[2:])


def error_event(code: str, error_type: str) -> dict:
    """Événement `error` tel que NeMo l'envoie (forme relevée sur le vrai serveur)."""
    return {
        "error": {"code": code, "message": f"{code} sent by the fake NeMo", "type": error_type},
        "event_id": "event_5",
        "type": "error",
    }


class FakeNemoRealtime:
    """Faux serveur temps réel de NeMo : il rejoue le relevé de la tâche 1 et note ce qu'il reçoit.

    Comme NeMo, il laisse le socket ouvert après `committed` : c'est au client de le fermer. Il répond
    `input_audio_buffer.cleared` à chaque `input_audio_buffer.clear`.
    """

    def __init__(self) -> None:
        # Événements envoyés après le commit ; une liste vide donne un NeMo qui ne répond plus.
        self.after_commit: list[dict] = AFTER_COMMIT
        # Événements de chaque commit, dans l'ordre des commits ; une fois la liste épuisée, `after_commit` vaut.
        self.after_commits: list[list[dict]] = []
        self.commits = 0
        self.commit_delay = 0.0  # secondes d'attente entre le commit et le premier événement qui le suit
        self.drop_on_commit = False  # coupe la connexion à la réception du commit
        self.error_code: str | None = None  # envoie cet `error` à la place de session.updated
        self.reject_status: int | None = None  # refuse la poignée de main avec ce statut HTTP
        self.port = 0
        self.path: str | None = None
        self.authorization: str | None = None
        self.session: dict | None = None
        self.frames: list[bytes] = []
        self.text_messages: list[dict] = []
        self.closed = asyncio.Event()  # posé quand la connexion se termine

    def process_request(self, connection, request):
        if self.reject_status is not None:
            return connection.respond(HTTPStatus(self.reject_status), "refused\n")
        return None

    async def handle(self, ws) -> None:
        self.path = ws.request.path
        self.authorization = ws.request.headers.get("Authorization")
        try:
            await ws.send(json.dumps(CREATED))
            async for message in ws:
                if isinstance(message, bytes):
                    self.frames.append(message)
                    continue
                event = json.loads(message)
                self.text_messages.append(event)
                if event["type"] == "session.update":
                    self.session = event["session"]
                    reply = error_event(self.error_code, "rate_limit_error") if self.error_code else UPDATED
                    await ws.send(json.dumps(reply))
                elif event["type"] == "input_audio_buffer.commit":
                    if self.drop_on_commit:
                        await ws.close()
                        return
                    await asyncio.sleep(self.commit_delay)
                    turn = self.commits
                    self.commits += 1
                    after = self.after_commits[turn] if turn < len(self.after_commits) else self.after_commit
                    for item in after:
                        await ws.send(json.dumps(item))
                elif event["type"] == "input_audio_buffer.clear":
                    await ws.send(json.dumps({"event_id": "event_9", "type": "input_audio_buffer.cleared"}))
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()


@pytest.fixture
async def fake_ws():
    fake = FakeNemoRealtime()
    # Le faux serveur ne journalise rien en DEBUG : le test des journaux ne lit que ce que le client écrit.
    quiet = logging.getLogger("tests.fake_nemo")
    quiet.setLevel(logging.INFO)
    server = await serve(fake.handle, "127.0.0.1", 0, process_request=fake.process_request, logger=quiet)
    fake.port = server.sockets[0].getsockname()[1]
    yield fake
    server.close()
    await server.wait_closed()


@pytest.fixture
async def nemo(fake_ws):
    async with httpx.AsyncClient() as http:
        yield NemoClient(f"http://127.0.0.1:{fake_ws.port}", "test-nemo-key", http)


async def collect(stream):
    return [item async for item in stream]


async def until(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def transcribe(nemo, path, *, language="fr-FR", phrases=(), boost=3.0, **options):
    return await collect(nemo.transcribe(path, language=language, phrases=list(phrases), boost=boost, **options))


# --- Temps réel ---


async def test_session_update_carries_language_timestamps_and_boost(fake_ws, nemo, wav_1s):
    await transcribe(nemo, wav_1s, phrases=["Nemotron"])
    assert fake_ws.session == {
        "sample_rate": 16000,
        "language": "fr-FR",
        "word_timestamps": True,
        "speech_contexts": [{"phrases": ["Nemotron"], "boost": 3.0}],
    }


async def test_no_speech_contexts_without_phrases(fake_ws, nemo, wav_1s):
    await transcribe(nemo, wav_1s, phrases=[])
    assert "speech_contexts" not in fake_ws.session


async def test_speakers_are_asked_in_the_session_update(fake_ws, nemo, tmp_path):
    await transcribe(nemo, write_wav(tmp_path, 1.0), speakers=True)
    assert fake_ws.session["speaker_diarization"] is True


async def test_no_speakers_and_no_prompt_unless_asked(fake_ws, nemo, tmp_path):
    await transcribe(nemo, write_wav(tmp_path, 1.0))
    assert "speaker_diarization" not in fake_ws.session and "prompt" not in fake_ws.session


async def test_words_carry_the_speaker_that_nemo_gives(fake_ws, nemo, tmp_path):
    fake_ws.after_commit = SPEAKERS_AFTER_COMMIT
    events = await transcribe(nemo, write_wav(tmp_path, 1.0), speakers=True)
    speakers = [word.speaker for event in events if isinstance(event, Final) for word in event.words]
    assert speakers == [str(word[SPEAKER_KEY]) for word in SPEAKERS_COMPLETED["words"]]


async def test_the_recorded_speakers_are_strings_numbered_from_one(fake_ws, nemo, wav_1s):
    fake_ws.after_commit = SPEAKERS_AFTER_COMMIT
    (final,) = [event for event in await transcribe(nemo, wav_1s, speakers=True) if isinstance(event, Final)]
    assert {word.speaker for word in final.words} == {"1", "2"}
    assert [word.speaker for word in final.words][:2] == ["1", "1"]


async def test_connects_to_the_realtime_route(fake_ws, nemo, wav_1s):
    await transcribe(nemo, wav_1s)
    assert fake_ws.path == "/v1/audio/transcriptions/realtime"


async def test_sends_the_api_key(fake_ws, nemo, wav_1s):
    await transcribe(nemo, wav_1s)
    assert fake_ws.authorization == "Bearer test-nemo-key"


async def test_session_update_comes_before_the_audio_and_commit_after_it(fake_ws, nemo, wav_1s):
    await transcribe(nemo, wav_1s)
    assert [message["type"] for message in fake_ws.text_messages] == ["session.update", "input_audio_buffer.commit"]


async def test_the_audio_is_read_from_the_file_by_one_second_frames(fake_ws, nemo, tmp_path):
    path = write_wav(tmp_path, 2.5)
    await transcribe(nemo, path)
    assert [len(frame) for frame in fake_ws.frames] == [32000, 32000, 16000]
    assert b"".join(fake_ws.frames) == path.read_bytes()[44:]
    assert fake_ws.text_messages[-1] == {"type": "input_audio_buffer.commit"}


async def test_progress_gives_the_seconds_already_sent(fake_ws, nemo, tmp_path):
    seen = []
    await transcribe(nemo, write_wav(tmp_path, 2.5), on_progress=seen.append)
    assert seen == [1.0, 2.0, 2.5]


async def test_finals_come_from_completed_events(fake_ws, nemo, wav_1s):
    finals = await transcribe(nemo, wav_1s)
    assert len(finals) == 1
    final = finals[0]
    assert final.text == COMPLETED["transcript"]
    assert final.words == tuple(Word(text=w["word"], start=w["start"], end=w["end"]) for w in COMPLETED["words"])
    # Le relevé écrit certains instants en entiers JSON : les mots les rendent toujours en secondes flottantes.
    assert all(isinstance(word.start, float) and isinstance(word.end, float) for word in final.words)
    assert all(word.speaker is None for word in final.words)


async def test_each_completed_event_gives_a_final_and_committed_ends_the_session(fake_ws, nemo, wav_1s):
    second = {
        **COMPLETED,
        "event_id": "event_19",
        "transcript": "Deuxième phrase.",
        "words": [
            {"confidence": 1, "end": 0.5, "start": 0.1, "word": "Deuxième"},
            {"confidence": 1, "end": 1, "start": 0.6, "word": "phrase."},
        ],
    }
    fake_ws.after_commit = [COMPLETED, second, COMMITTED]
    finals = await transcribe(nemo, wav_1s)
    assert [final.text for final in finals] == [COMPLETED["transcript"], "Deuxième phrase."]
    assert finals[1].words == (Word("Deuxième", 0.1, 0.5), Word("phrase.", 0.6, 1.0))


async def test_no_partials_unless_they_are_asked_for(fake_ws, nemo, wav_1s):
    items = await transcribe(nemo, wav_1s)
    assert [type(item) for item in items] == [Final]
    assert items == await transcribe(nemo, wav_1s, partials=False)


async def test_partials_come_from_the_non_empty_deltas_in_order_before_the_final(fake_ws, nemo, wav_1s):
    deltas = [event["delta"] for event in AFTER_COMMIT if event["type"].endswith(".delta")]
    # Le relevé contient bien un delta vide, que le client ne relaie pas ; les autres gardent leurs espaces.
    assert "" in deltas
    assert any(delta != delta.strip() for delta in deltas)
    items = await transcribe(nemo, wav_1s, partials=True)
    assert items[:-1] == [Partial(delta) for delta in deltas if delta]
    assert [type(item) for item in items[-1:]] == [Final]


async def test_the_finals_do_not_change_with_the_partials(fake_ws, nemo, wav_1s):
    with_partials = await transcribe(nemo, wav_1s, partials=True)
    assert [item for item in with_partials if isinstance(item, Final)] == await transcribe(nemo, wav_1s)


async def test_the_deltas_the_recording_shows_start_the_final_text(fake_ws, nemo, wav_1s):
    # La route en flux émet le reste du `Final` après les `Partial` : elle suppose ce préfixe, que le relevé montre.
    items = await transcribe(nemo, wav_1s, partials=True)
    streamed = "".join(item.text for item in items if isinstance(item, Partial))
    assert items[-1].text.startswith(streamed)
    assert items[-1].text != streamed


async def test_partials_of_each_item_come_before_its_final(fake_ws, nemo, wav_1s):
    second = {**COMPLETED, "event_id": "event_19", "transcript": "Deuxième phrase.", "words": []}
    delta = {"type": "conversation.item.input_audio_transcription.delta", "delta": "Deuxième"}
    fake_ws.after_commit = [delta, COMPLETED, delta, second, COMMITTED]
    items = await transcribe(nemo, wav_1s, partials=True)
    recorded_final = (await transcribe(nemo, wav_1s))[0]
    assert items == [Partial("Deuxième"), recorded_final, Partial("Deuxième"), Final("Deuxième phrase.", ())]


async def test_the_client_closes_the_socket_when_the_session_ends(fake_ws, nemo, wav_1s):
    await transcribe(nemo, wav_1s)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("resource_exhausted", NemoOverloaded),
        ("invalid_argument", NemoError),
        ("unavailable", NemoError),
        ("internal_error", NemoError),
    ],
)
async def test_error_event_raises_the_exception_of_its_code(fake_ws, nemo, wav_1s, code, expected):
    fake_ws.error_code = code
    with pytest.raises(expected, match=f"{code} sent by the fake NeMo" if expected is NemoError else None):
        await transcribe(nemo, wav_1s)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_error_event_after_the_audio_raises_too(fake_ws, nemo, wav_1s):
    fake_ws.after_commit = [error_event("resource_exhausted", "rate_limit_error")]
    with pytest.raises(NemoOverloaded):
        await transcribe(nemo, wav_1s)


@pytest.mark.parametrize(("status", "expected"), [(429, NemoOverloaded), (401, NemoError), (503, NemoError)])
async def test_refused_handshake_maps_the_http_status(fake_ws, nemo, wav_1s, status, expected):
    fake_ws.reject_status = status
    with pytest.raises(expected):
        await transcribe(nemo, wav_1s)


async def test_unreachable_server_raises_unavailable(wav_1s):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Le port est libéré : personne n'y écoute.
    async with httpx.AsyncClient() as http:
        nemo = NemoClient(f"http://127.0.0.1:{port}", "test-nemo-key", http)
        with pytest.raises(NemoUnavailable):
            await transcribe(nemo, wav_1s)


async def test_connection_dropped_by_nemo_raises_unavailable(fake_ws, nemo, wav_1s):
    fake_ws.drop_on_commit = True
    with pytest.raises(NemoUnavailable):
        await transcribe(nemo, wav_1s)


async def test_silence_from_nemo_raises_nemo_error(fake_ws, nemo, wav_1s, monkeypatch):
    monkeypatch.setattr(nemo_module, "EVENT_TIMEOUT", 0.2)
    fake_ws.after_commit = []
    with pytest.raises(NemoError, match="no event"):
        await transcribe(nemo, wav_1s)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_with_speakers_the_wait_between_two_events_goes_up_to_the_speakers_timeout(
    fake_ws, nemo, wav_1s, monkeypatch
):
    # NeMo marque les personnes sur tout l'audio avant de rendre le seul événement final : l'attente y est plus longue.
    monkeypatch.setattr(nemo_module, "EVENT_TIMEOUT", 0.2)
    monkeypatch.setattr(nemo_module, "SPEAKERS_EVENT_TIMEOUT", 3.0)
    fake_ws.commit_delay = 0.6
    with pytest.raises(NemoError, match="no event from NeMo for 0.2 seconds"):
        await transcribe(nemo, wav_1s)
    fake_ws.after_commit = SPEAKERS_AFTER_COMMIT
    items = await transcribe(nemo, wav_1s, speakers=True)
    assert [type(item) for item in items] == [Final]


async def test_with_speakers_silence_from_nemo_still_ends_after_the_speakers_timeout(
    fake_ws, nemo, wav_1s, monkeypatch
):
    monkeypatch.setattr(nemo_module, "SPEAKERS_EVENT_TIMEOUT", 0.3)
    fake_ws.after_commit = []
    started = time.monotonic()
    with pytest.raises(NemoError, match="no event from NeMo for 0.3 seconds"):
        await transcribe(nemo, wav_1s, speakers=True)
    assert time.monotonic() - started >= 0.3
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_stopping_the_consumer_closes_the_session(fake_ws, nemo, wav_1s):
    fake_ws.after_commit = HELD_BACK
    first_final = asyncio.Event()

    async def consume():
        async with aclosing(nemo.transcribe(wav_1s, language="fr-FR", phrases=[], boost=3.0)) as finals:
            async for _ in finals:
                first_final.set()
                await asyncio.Event().wait()  # le consommateur ne revient jamais chercher la suite

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_final.wait(), 2)
    task.cancel()
    await asyncio.wait_for(fake_ws.closed.wait(), 1)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelling_a_consumer_that_waits_for_nemo_closes_the_session(fake_ws, nemo, wav_1s):
    fake_ws.after_commit = []
    task = asyncio.create_task(transcribe(nemo, wav_1s))
    await until(lambda: fake_ws.text_messages and fake_ws.text_messages[-1]["type"] == "input_audio_buffer.commit")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_cancelling_during_the_audio_upload_closes_the_session(fake_ws, nemo, wav_1s, monkeypatch):
    # NeMo lit l'audio à sa vitesse : l'envoi d'une longue dictée dure des minutes.
    # On simule un envoi qui ne finit pas.
    async def endless_upload(*_):
        await asyncio.Event().wait()

    monkeypatch.setattr(nemo_module, "_send_audio", endless_upload)
    task = asyncio.create_task(transcribe(nemo, wav_1s))
    await until(lambda: fake_ws.session is not None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_an_error_of_the_progress_callback_ends_the_session_at_once(fake_ws, nemo, wav_1s):
    def broken(seconds):
        raise RuntimeError("progress failed")

    # NeMo ne dit rien : sans la garde de l'envoi, la session attendrait le délai d'attente avant de s'arrêter.
    fake_ws.after_commit = []
    async with asyncio.timeout(5):
        with pytest.raises(RuntimeError, match="progress failed"):
            await transcribe(nemo, wav_1s, on_progress=broken)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_a_file_that_cannot_be_read_ends_the_session_with_its_own_error(fake_ws, nemo, tmp_path):
    fake_ws.after_commit = []
    async with asyncio.timeout(5):
        with pytest.raises(FileNotFoundError):
            await transcribe(nemo, tmp_path / "missing.wav")
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_connection_options_fit_nemo(fake_ws, nemo, wav_1s, monkeypatch):
    # NeMo ne répond à un ping qu'après l'audio envoyé avant lui, le `completed` d'une longue dictée est un seul
    # événement plus gros que la limite par défaut de `websockets`, et une session abandonnée pendant l'envoi de
    # l'audio ne répond pas à la fermeture : on n'attend pas les 10 s par défaut.
    seen: dict = {}
    real_connect = nemo_module.connect

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(nemo_module, "connect", spy)
    await transcribe(nemo, wav_1s)
    assert seen["ping_interval"] is None
    assert seen["max_size"] is None
    assert seen["close_timeout"] == nemo_module.CLOSE_TIMEOUT < 10


async def test_logs_never_carry_the_api_key_nor_the_session_content(fake_ws, nemo, wav_1s, caplog):
    # À son niveau DEBUG, `websockets` écrit les en-têtes de la poignée de main et un extrait de chaque trame.
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="websockets")
    # Témoin : une connexion qui garde le journal par défaut de `websockets` laisse bien la clé dans le journal.
    async with connect(f"ws://127.0.0.1:{fake_ws.port}", additional_headers={"Authorization": "Bearer control-key"}):
        pass
    assert "Bearer control-key" in caplog.text
    caplog.clear()

    await transcribe(nemo, wav_1s, phrases=["Sortformer"], speakers=True)
    assert fake_ws.authorization == "Bearer test-nemo-key"  # la session a bien eu lieu, clé comprise
    for secret in ("test-nemo-key", "Sortformer", "speech_contexts", "speaker_diarization"):
        assert secret not in caplog.text


# --- Dictée en direct ---


def live_items(after_commit: list[dict]) -> list:
    """Ce que `LiveSession.events` doit rendre des événements d'un commit : un `Partial` par delta non vide, le texte
    du `completed` en `Final`, puis `Committed`."""
    partials = [
        Partial(event["delta"]) for event in after_commit if event["type"].endswith(".delta") and event["delta"]
    ]
    (completed,) = [event for event in after_commit if event["type"].endswith(".completed")]
    return [*partials, Final(completed["transcript"], ()), Committed()]


async def test_a_live_session_sends_its_options_then_the_audio_as_binary_frames(fake_ws, nemo):
    async with nemo.live(
        sample_rate=24000, language="fr-FR", phrases=["Nemotron"], boost=1.0, endpointing_ms=500
    ) as session:
        await session.send_audio(b"\x00\x01" * 100)
        await session.commit()
    assert fake_ws.session == {
        "sample_rate": 24000,
        "language": "fr-FR",
        "speech_contexts": [{"phrases": ["Nemotron"], "boost": 1.0}],
        "endpointing_ms": 500,
    }
    assert fake_ws.frames == [b"\x00\x01" * 100]


async def test_a_live_session_sends_neither_terms_nor_endpointing_unless_given(fake_ws, nemo):
    async with nemo.live(sample_rate=16000, language="en-US", phrases=[], boost=1.0):
        pass
    assert fake_ws.session == {"sample_rate": 16000, "language": "en-US"}


async def test_live_events_follow_the_recorded_multiturn_session(fake_ws, nemo):
    fake_ws.after_commits = MULTITURN_AFTER_COMMITS
    received = []
    live = nemo.live(sample_rate=16000, language="fr-FR", phrases=[], boost=1.0)
    async with live as session, aclosing(session.events()) as events:
        for _ in MULTITURN_AFTER_COMMITS:
            await session.send_audio(b"\x00\x00" * 1600)
            await session.commit()
            async for event in events:
                received.append(event)
                if isinstance(event, Committed):
                    break
    assert len(MULTITURN_AFTER_COMMITS) == 2
    assert received == [item for turn in MULTITURN_AFTER_COMMITS for item in live_items(turn)]


async def test_a_live_clear_is_sent_and_acknowledged(fake_ws, nemo):
    async with nemo.live(sample_rate=24000, language="fr-FR", phrases=[], boost=1.0) as session:
        await session.send_audio(b"\x00\x01" * 100)
        await session.clear()
        async with aclosing(session.events()) as events:
            assert await anext(events) == Cleared()
    assert fake_ws.text_messages[-1] == {"type": "input_audio_buffer.clear"}


async def test_a_live_error_event_raises_the_exception_of_its_code(fake_ws, nemo):
    fake_ws.after_commit = [error_event("resource_exhausted", "rate_limit_error")]
    with pytest.raises(NemoOverloaded):
        async with nemo.live(sample_rate=24000, language="fr-FR", phrases=[], boost=1.0) as session:
            await session.commit()
            async with aclosing(session.events()) as events:
                await anext(events)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_a_live_session_dropped_by_nemo_raises_unavailable(fake_ws, nemo):
    fake_ws.drop_on_commit = True
    with pytest.raises(NemoUnavailable):
        async with nemo.live(sample_rate=24000, language="fr-FR", phrases=[], boost=1.0) as session:
            await session.commit()
            async with aclosing(session.events()) as events:
                await anext(events)


async def test_leaving_a_live_session_closes_its_socket(fake_ws, nemo):
    async with nemo.live(sample_rate=24000, language="fr-FR", phrases=[], boost=1.0) as session:
        await session.send_audio(b"\x00\x01" * 100)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


# --- Routes HTTP ---


def envelope(message: str, error_type: str) -> dict:
    """Enveloppe d'erreur des routes HTTP de NeMo (forme relevée sur le vrai serveur)."""
    return {"error": {"code": None, "message": message, "param": None, "type": error_type}}


# Relevé de la route REST de la tâche 1 : la réponse de NeMo à un morceau de 30 s en `verbose_json` avec les mots.
REST_RECORDED = json.loads((FIXTURES / "rest_transcription.json").read_text())


def ok_response(path: str) -> httpx.Response:
    if path == "/v1/audio/speech":
        return httpx.Response(200, content=b"\x01\x00" * 100, headers={"Content-Type": "audio/pcm"})
    if path == "/v1/models":
        return httpx.Response(200, json=json.loads((FIXTURES / "models.json").read_text()))
    if path == "/v1/audio/transcriptions":
        return httpx.Response(200, json=REST_RECORDED)
    if path == "/ready":
        return httpx.Response(200, json={"ready": True})
    raise AssertionError(f"unexpected route {path}")


def parse_form(request: httpx.Request) -> dict[str, list]:
    """Relit le formulaire multipart d'une requête notée : le texte de chaque champ, les octets du fichier."""
    head = f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode()
    message = email.message_from_bytes(head + request.content, policy=email.policy.HTTP)
    form: dict[str, list] = {}
    for part in message.iter_parts():
        content = part.get_payload(decode=True)
        value = content if part.get_filename() else content.decode()
        form.setdefault(part.get_param("name", header="content-disposition"), []).append(value)
    return form


class Recorder:
    """Faux transport HTTP : il note les requêtes et rend l'erreur ou la réponse fixée, sinon celle de la route."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response or ok_response(request.url.path)


@pytest.fixture
async def http_nemo():
    clients: list[httpx.AsyncClient] = []

    def build(recorder: Recorder) -> NemoClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        clients.append(http)
        return NemoClient("http://nemo.test", "test-nemo-key", http)

    yield build
    for http in clients:
        await http.aclose()


CALLS = {
    "speak": lambda nemo: nemo.speak("Bonjour.", voice="Leo", language="fr-FR"),
    "voices": lambda nemo: nemo.voices(),
    "transcribe_chunk": lambda nemo: nemo.transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0),
}


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_http_429_raises_overloaded(http_nemo, call):
    nemo = http_nemo(Recorder(httpx.Response(429, json=envelope("queue full", "rate_limit_error"))))
    with pytest.raises(NemoOverloaded):
        await call(nemo)


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_connect_error_raises_unavailable(http_nemo, call):
    nemo = http_nemo(Recorder(error=httpx.ConnectError("refused")))
    with pytest.raises(NemoUnavailable):
        await call(nemo)


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("slow"), httpx.ReadError("reset"), httpx.RemoteProtocolError("disconnected")],
    ids=lambda error: type(error).__name__,
)
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_timeout_or_lost_connection_raises_unavailable(http_nemo, call, error):
    nemo = http_nemo(Recorder(error=error))
    with pytest.raises(NemoUnavailable):
        await call(nemo)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(422, json=envelope("unsupported audio format", "invalid_request_error")),
            "HTTP 422.*unsupported audio format",
        ),
        (httpx.Response(500, text="<html>boom</html>"), "HTTP 500"),
    ],
    ids=["envelope", "no envelope"],
)
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_other_error_status_raises_nemo_error(http_nemo, call, response, expected):
    nemo = http_nemo(Recorder(response))
    with pytest.raises(NemoError, match=expected):
        await call(nemo)


async def test_speak_asks_for_pcm(http_nemo):
    recorder = Recorder()
    audio = await http_nemo(recorder).speak("Bonjour.", voice="Leo", language="fr-FR")
    (request,) = recorder.requests
    assert (request.method, request.url.path) == ("POST", "/v1/audio/speech")
    assert request.headers["Authorization"] == "Bearer test-nemo-key"
    assert json.loads(request.content) == {
        "input": "Bonjour.",
        "voice": "Leo",
        "language": "fr-FR",
        "response_format": "pcm",
    }
    assert audio == b"\x01\x00" * 100


def test_tts_sample_rate_is_the_measured_one():
    assert TTS_SAMPLE_RATE == 22050


async def test_voices_come_from_the_models_listing(http_nemo):
    recorder = Recorder()
    voices = await http_nemo(recorder).voices()
    (request,) = recorder.requests
    assert (request.method, request.url.path) == ("GET", "/v1/models")
    assert request.headers["Authorization"] == "Bearer test-nemo-key"
    assert set(voices) == {"John", "Sofia", "Aria", "Jason", "Leo"}


async def test_voices_without_a_speech_model_raises_nemo_error(http_nemo):
    listing = json.loads((FIXTURES / "models.json").read_text())
    listing["data"] = [model for model in listing["data"] if model["capability"] != "speech"]
    nemo = http_nemo(Recorder(httpx.Response(200, json=listing)))
    with pytest.raises(NemoError, match="speech"):
        await nemo.voices()


@pytest.mark.parametrize(
    ("call", "read_timeout"),
    [
        (CALLS["speak"], 120.0),
        (CALLS["voices"], 10.0),
        # Un morceau de 30 s prend 8 s seul chez NeMo, mais 16 morceaux envoyés ensemble mettent 128 s : le délai
        # couvre la file de NeMo (voir `CHUNK_TIMEOUT`).
        (CALLS["transcribe_chunk"], 600.0),
        (lambda nemo: nemo.ready(), 5.0),
    ],
    ids=[*CALLS, "ready"],
)
async def test_http_calls_set_their_own_timeout(http_nemo, call, read_timeout):
    # Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
    recorder = Recorder()
    await call(http_nemo(recorder))
    timeout = recorder.requests[0].extensions["timeout"]
    assert timeout["read"] == read_timeout
    assert timeout["connect"] == 10.0


# --- Morceaux sur la route REST ---


async def test_a_chunk_goes_as_a_multipart_form_for_word_timestamps(http_nemo):
    recorder = Recorder()
    await http_nemo(recorder).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    [request] = recorder.requests
    form = parse_form(request)
    assert request.url.path == "/v1/audio/transcriptions"
    assert (form["language"], form["response_format"]) == (["fr-FR"], ["verbose_json"])
    assert form["timestamp_granularities[]"] == ["word"] and form["file"] == [WAV_1S]
    assert "speech_contexts" not in form and "prompt" not in form


async def test_a_chunk_is_sent_as_the_file_audio_wav_with_the_api_key(http_nemo):
    recorder = Recorder()
    await http_nemo(recorder).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    [request] = recorder.requests
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer test-nemo-key"
    assert b'name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n' in request.content


async def test_a_chunk_carries_the_glossary_as_speech_contexts(http_nemo):
    recorder = Recorder()
    await http_nemo(recorder).transcribe_chunk(WAV_1S, language="fr-FR", phrases=["Nemotron"], boost=1.0)
    assert json.loads(parse_form(recorder.requests[0])["speech_contexts"][0]) == [
        {"phrases": ["Nemotron"], "boost": 1.0}
    ]


async def test_a_chunk_answer_gives_the_text_and_the_timed_words(http_nemo):
    final = await http_nemo(Recorder()).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    assert final.text == REST_RECORDED["text"].strip()
    assert [(w.text, w.start, w.end) for w in final.words] == [
        (w["word"], w["start"], w["end"]) for w in REST_RECORDED["words"]
    ]
    # Les mots de la route REST ne portent pas de personne.
    assert all(word.speaker is None for word in final.words)


async def test_a_chunk_text_loses_the_spaces_around_it(http_nemo):
    answer = httpx.Response(200, json={**REST_RECORDED, "text": "  Bonjour à tous.\n", "words": []})
    final = await http_nemo(Recorder(answer)).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    assert final == Final("Bonjour à tous.", ())


async def test_a_silent_chunk_gives_an_empty_final(http_nemo):
    answer = httpx.Response(200, json={"duration": 30, "language": "fr-FR", "task": "transcribe", "text": ""})
    final = await http_nemo(Recorder(answer)).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)
    assert final == Final("", ())


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"language": "fr-FR"}),
        httpx.Response(200, json={"text": None}),
        httpx.Response(200, json={"text": "Bonjour.", "words": [{"word": "Bonjour."}]}),
    ],
    ids=["not json", "not an object", "no text", "null text", "word without times"],
)
async def test_a_chunk_answer_that_cannot_be_read_raises_nemo_error(http_nemo, answer):
    with pytest.raises(NemoError, match="unreadable"):
        await http_nemo(Recorder(answer)).transcribe_chunk(WAV_1S, language="fr-FR", phrases=[], boost=1.0)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [(200, {"ready": True}, True), (200, {"ready": False}, False), (503, {"ready": False}, False)],
)
async def test_ready_is_true_only_when_nemo_says_so(http_nemo, status, body, expected):
    assert await http_nemo(Recorder(httpx.Response(status, json=body))).ready() is expected


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="ready"),
        httpx.Response(200, json=["ready"]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"ready": "true"}),
        httpx.Response(429, json=envelope("queue full", "rate_limit_error")),
        httpx.Response(500, text="<html>boom</html>"),
    ],
    ids=["not json", "not an object", "no key", "text instead of true", "429", "500"],
)
async def test_ready_is_false_for_any_other_answer(http_nemo, answer):
    assert await http_nemo(Recorder(answer)).ready() is False


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("disconnected")],
    ids=lambda error: type(error).__name__,
)
async def test_ready_is_false_when_the_transport_fails(http_nemo, error):
    assert await http_nemo(Recorder(error=error)).ready() is False


async def test_ready_is_false_when_nemo_is_unreachable():
    async with httpx.AsyncClient() as http:
        assert await NemoClient("http://127.0.0.1:9", "key", http).ready() is False


async def test_ready_reads_the_ready_route_with_the_api_key(http_nemo):
    recorder = Recorder()
    assert await http_nemo(recorder).ready() is True
    [request] = recorder.requests
    assert (request.method, request.url.path) == ("GET", "/ready")
    assert request.headers["Authorization"] == "Bearer test-nemo-key"
