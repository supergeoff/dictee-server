import asyncio
import io
import json
import socket
import struct
import wave
from contextlib import aclosing
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from dictee import nemo as nemo_module
from dictee.audio import wav_pcm
from dictee.nemo import (
    TTS_SAMPLE_RATE,
    NemoClient,
    NemoError,
    NemoOverloaded,
    NemoUnavailable,
)
from dictee.types import Turn, Word

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "nemo"


def make_wav(seconds: float) -> bytes:
    """WAV PCM16 mono 16 kHz aux octets variés : une trame perdue ou déplacée change le contenu relu."""
    count = int(16000 * seconds)
    pcm = struct.pack(f"<{count}h", *((n * 37) % 20000 - 10000 for n in range(count)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(pcm)
    return buffer.getvalue()


WAV_1S = make_wav(1.0)

# Relevé de la session temps réel de la tâche 1 : session.created, session.updated, puis les événements qui
# suivent le commit (deltas, completed, committed).
RECORDED = [json.loads(line) for line in (FIXTURES / "realtime_events.jsonl").read_text().splitlines()]
CREATED, UPDATED, *AFTER_COMMIT = RECORDED
COMPLETED = next(event for event in AFTER_COMMIT if event["type"].endswith(".completed"))
COMMITTED = next(event for event in AFTER_COMMIT if event["type"] == "input_audio_buffer.committed")
# NeMo a rendu le final mais n'a pas annoncé la fin : la session reste ouverte.
HELD_BACK = [event for event in AFTER_COMMIT if event is not COMMITTED]


def error_event(code: str, error_type: str) -> dict:
    """Événement `error` tel que NeMo l'envoie (forme relevée sur le vrai serveur)."""
    return {
        "error": {"code": code, "message": f"{code} sent by the fake NeMo", "type": error_type},
        "event_id": "event_5",
        "type": "error",
    }


class FakeNemoRealtime:
    """Faux serveur temps réel de NeMo : il rejoue le relevé de la tâche 1 et note ce qu'il reçoit.

    Comme NeMo, il laisse le socket ouvert après `committed` : c'est au client de le fermer.
    """

    def __init__(self) -> None:
        # Événements envoyés après le commit ; une liste vide donne un NeMo qui ne répond plus.
        self.after_commit: list[dict] = AFTER_COMMIT
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
                    for item in self.after_commit:
                        await ws.send(json.dumps(item))
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()


@pytest.fixture
async def fake_ws():
    fake = FakeNemoRealtime()
    server = await serve(fake.handle, "127.0.0.1", 0, process_request=fake.process_request)
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


async def transcribe(nemo, wav=WAV_1S, *, language="fr-FR", phrases=(), boost=3.0, prompt=None):
    return await collect(nemo.transcribe(wav, language=language, phrases=list(phrases), boost=boost, prompt=prompt))


# --- Temps réel ---


async def test_session_update_carries_language_timestamps_and_boost(fake_ws, nemo):
    await transcribe(nemo, phrases=["Nemotron"])
    assert fake_ws.session == {
        "sample_rate": 16000,
        "language": "fr-FR",
        "word_timestamps": True,
        "speech_contexts": [{"phrases": ["Nemotron"], "boost": 3.0}],
    }


async def test_no_speech_contexts_without_phrases(fake_ws, nemo):
    await transcribe(nemo, phrases=[])
    assert "speech_contexts" not in fake_ws.session
    assert "prompt" not in fake_ws.session


async def test_prompt_is_passed_on(fake_ws, nemo):
    await transcribe(nemo, prompt="Nemotron")
    assert fake_ws.session["prompt"] == "Nemotron"


async def test_connects_to_the_realtime_route(fake_ws, nemo):
    await transcribe(nemo)
    assert fake_ws.path == "/v1/audio/transcriptions/realtime"


async def test_sends_the_api_key(fake_ws, nemo):
    await transcribe(nemo)
    assert fake_ws.authorization == "Bearer test-nemo-key"


async def test_session_update_comes_before_the_audio_and_commit_after_it(fake_ws, nemo):
    await transcribe(nemo)
    assert [message["type"] for message in fake_ws.text_messages] == ["session.update", "input_audio_buffer.commit"]


async def test_audio_goes_as_binary_frames_then_commit(fake_ws, nemo):
    wav = make_wav(2.5)
    await transcribe(nemo, wav)
    assert b"".join(fake_ws.frames) == wav_pcm(wav)
    assert [len(frame) for frame in fake_ws.frames] == [32000, 32000, 16000]
    assert fake_ws.text_messages[-1] == {"type": "input_audio_buffer.commit"}


async def test_audio_given_as_a_bytearray_is_sent_the_same(fake_ws, nemo):
    wav = make_wav(1.5)
    await transcribe(nemo, bytearray(wav))
    assert b"".join(fake_ws.frames) == wav_pcm(wav)


async def test_finals_come_from_completed_events(fake_ws, nemo):
    finals = await transcribe(nemo)
    assert len(finals) == 1
    final = finals[0]
    assert final.text == COMPLETED["transcript"]
    assert final.words == tuple(Word(text=w["word"], start=w["start"], end=w["end"]) for w in COMPLETED["words"])
    # Le relevé écrit certains instants en entiers JSON : les mots les rendent toujours en secondes flottantes.
    assert all(isinstance(word.start, float) and isinstance(word.end, float) for word in final.words)
    assert all(word.speaker is None for word in final.words)


async def test_each_completed_event_gives_a_final_and_committed_ends_the_session(fake_ws, nemo):
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
    finals = await transcribe(nemo)
    assert [final.text for final in finals] == [COMPLETED["transcript"], "Deuxième phrase."]
    assert finals[1].words == (Word("Deuxième", 0.1, 0.5), Word("phrase.", 0.6, 1.0))


async def test_the_client_closes_the_socket_when_the_session_ends(fake_ws, nemo):
    await transcribe(nemo)
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
async def test_error_event_raises_the_exception_of_its_code(fake_ws, nemo, code, expected):
    fake_ws.error_code = code
    with pytest.raises(expected, match=f"{code} sent by the fake NeMo" if expected is NemoError else None):
        await transcribe(nemo)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_error_event_after_the_audio_raises_too(fake_ws, nemo):
    fake_ws.after_commit = [error_event("resource_exhausted", "rate_limit_error")]
    with pytest.raises(NemoOverloaded):
        await transcribe(nemo)


@pytest.mark.parametrize(("status", "expected"), [(429, NemoOverloaded), (401, NemoError), (503, NemoError)])
async def test_refused_handshake_maps_the_http_status(fake_ws, nemo, status, expected):
    fake_ws.reject_status = status
    with pytest.raises(expected):
        await transcribe(nemo)


async def test_unreachable_server_raises_unavailable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Le port est libéré : personne n'y écoute.
    async with httpx.AsyncClient() as http:
        nemo = NemoClient(f"http://127.0.0.1:{port}", "test-nemo-key", http)
        with pytest.raises(NemoUnavailable):
            await transcribe(nemo)


async def test_connection_dropped_by_nemo_raises_unavailable(fake_ws, nemo):
    fake_ws.drop_on_commit = True
    with pytest.raises(NemoUnavailable):
        await transcribe(nemo)


async def test_silence_from_nemo_raises_nemo_error(fake_ws, nemo, monkeypatch):
    monkeypatch.setattr(nemo_module, "EVENT_TIMEOUT", 0.2)
    fake_ws.after_commit = []
    with pytest.raises(NemoError, match="no event"):
        await transcribe(nemo)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_stopping_the_consumer_closes_the_session(fake_ws, nemo):
    fake_ws.after_commit = HELD_BACK
    first_final = asyncio.Event()

    async def consume():
        async with aclosing(nemo.transcribe(WAV_1S, language="fr-FR", phrases=[], boost=3.0, prompt=None)) as finals:
            async for _ in finals:
                first_final.set()
                await asyncio.Event().wait()  # le consommateur ne revient jamais chercher la suite

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_final.wait(), 2)
    task.cancel()
    await asyncio.wait_for(fake_ws.closed.wait(), 1)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelling_a_consumer_that_waits_for_nemo_closes_the_session(fake_ws, nemo):
    fake_ws.after_commit = []
    task = asyncio.create_task(transcribe(nemo))
    await until(lambda: fake_ws.text_messages and fake_ws.text_messages[-1]["type"] == "input_audio_buffer.commit")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_cancelling_during_the_audio_upload_closes_the_session(fake_ws, nemo, monkeypatch):
    # NeMo lit l'audio à sa vitesse : l'envoi d'une longue dictée dure des minutes.
    # On simule un envoi qui ne finit pas.
    async def endless_upload(ws, pcm):
        await asyncio.Event().wait()

    monkeypatch.setattr(nemo_module, "_send_audio", endless_upload)
    task = asyncio.create_task(transcribe(nemo))
    await until(lambda: fake_ws.session is not None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(fake_ws.closed.wait(), 1)


async def test_connection_options_fit_nemo(fake_ws, nemo, monkeypatch):
    # NeMo ne répond à un ping qu'après l'audio envoyé avant lui, le `completed` d'une longue dictée est un seul
    # événement plus gros que la limite par défaut de `websockets`, et une session abandonnée pendant l'envoi de
    # l'audio ne répond pas à la fermeture : on n'attend pas les 10 s par défaut.
    seen: dict = {}
    real_connect = nemo_module.connect

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(nemo_module, "connect", spy)
    await transcribe(nemo)
    assert seen["ping_interval"] is None
    assert seen["max_size"] is None
    assert seen["close_timeout"] == nemo_module.CLOSE_TIMEOUT < 10


# --- Routes HTTP ---


def envelope(message: str, error_type: str) -> dict:
    """Enveloppe d'erreur des routes HTTP de NeMo (forme relevée sur le vrai serveur)."""
    return {"error": {"code": None, "message": message, "param": None, "type": error_type}}


def ok_response(path: str) -> httpx.Response:
    if path == "/v1/audio/diarizations":
        return httpx.Response(200, json=json.loads((FIXTURES / "diarization.json").read_text()))
    if path == "/v1/audio/speech":
        return httpx.Response(200, content=b"\x01\x00" * 100, headers={"Content-Type": "audio/pcm"})
    if path == "/v1/models":
        return httpx.Response(200, json=json.loads((FIXTURES / "models.json").read_text()))
    raise AssertionError(f"unexpected route {path}")


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
    "diarize": lambda nemo: nemo.diarize(WAV_1S),
    "speak": lambda nemo: nemo.speak("Bonjour.", voice="Leo", language="fr-FR"),
    "voices": lambda nemo: nemo.voices(),
}


async def test_diarize_posts_the_file_and_parses_turns(http_nemo):
    recorder = Recorder()
    turns = await http_nemo(recorder).diarize(WAV_1S)
    (request,) = recorder.requests
    assert (request.method, request.url.path) == ("POST", "/v1/audio/diarizations")
    assert request.headers["Authorization"] == "Bearer test-nemo-key"
    assert b'name="file"' in request.content
    assert WAV_1S in request.content
    assert b'name="mode"\r\n\r\nstreaming\r\n' in request.content
    segments = json.loads((FIXTURES / "diarization.json").read_text())["segments"]
    assert turns == [Turn(start=s["start"], end=s["end"], speaker=s["speaker"]) for s in segments]
    assert [turn.speaker for turn in turns] == [1, 2]
    assert all(isinstance(turn.start, float) and isinstance(turn.end, float) for turn in turns)


async def test_diarize_accepts_a_bytearray(http_nemo):
    recorder = Recorder()
    await http_nemo(recorder).diarize(bytearray(WAV_1S))
    assert WAV_1S in recorder.requests[0].content


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
async def test_timeout_or_lost_connection_raises_unavailable(http_nemo, error):
    nemo = http_nemo(Recorder(error=error))
    with pytest.raises(NemoUnavailable):
        await nemo.diarize(WAV_1S)


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
async def test_other_error_status_raises_nemo_error(http_nemo, response, expected):
    nemo = http_nemo(Recorder(response))
    with pytest.raises(NemoError, match=expected):
        await nemo.diarize(WAV_1S)


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
    [(CALLS["diarize"], 1200.0), (CALLS["speak"], 120.0), (CALLS["voices"], 10.0)],
    ids=CALLS.keys(),
)
async def test_http_calls_set_their_own_timeout(http_nemo, call, read_timeout):
    # Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
    recorder = Recorder()
    await call(http_nemo(recorder))
    timeout = recorder.requests[0].extensions["timeout"]
    assert timeout["read"] == read_timeout
    assert timeout["connect"] == 10.0
