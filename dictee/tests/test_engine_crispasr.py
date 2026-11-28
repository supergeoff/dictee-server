"""L'adaptateur CrispASR : morceaux de fichier, disponibilité, erreurs, et une tâche jouée de bout en bout."""

import asyncio
import contextlib
import json

import httpx
import pytest
from conftest import audio_task, write_wav
from fakes import CRISPASR_FIXTURES, FakeCrispasr, FakeNemo

from dictee import cuts
from dictee.cuts import Silence, chunk_bounds
from dictee.engines import Engines
from dictee.engines import crispasr as crispasr_module
from dictee.engines.crispasr import STREAM_CHUNK_S, CrispasrTranscriber
from dictee.engines.ports import EngineError, EngineOverloaded, EngineUnavailable
from dictee.processing import RunStats, Services, process
from dictee.speakers import label_speakers
from dictee.types import Final, Word

pytestmark = pytest.mark.anyio

CHUNK = json.loads((CRISPASR_FIXTURES / "chunk.json").read_text())
DIARIZED = json.loads((CRISPASR_FIXTURES / "diarized.json").read_text())


@pytest.fixture
def fake_crispasr():
    return FakeCrispasr()


@pytest.fixture
def crispasr(fake_crispasr):
    return CrispasrTranscriber("http://crispasr.test/", "secret-key", fake_crispasr.http)


def failing(error: Exception) -> CrispasrTranscriber:
    """Un adaptateur dont tout appel HTTP échoue par `error`."""

    def fail(request):
        raise error

    return CrispasrTranscriber("http://crispasr.test", "k", httpx.AsyncClient(transport=httpx.MockTransport(fail)))


async def chunk(adapter, language="fr-FR", phrases=()):
    return await adapter.transcribe_chunk(b"RIFF", language=language, phrases=phrases, boost=1.0)


async def test_chunk_returns_words_with_times(crispasr):
    final = await chunk(crispasr)
    expected = [word for segment in CHUNK["segments"] for word in segment["words"]]
    assert final.text == CHUNK["text"].strip()
    assert final.words == tuple(Word(w["word"], w["start"], w["end"]) for w in expected)
    assert final.words[0] == Word("Bonjour,", 0.0, 0.64)


async def test_chunk_posts_the_expected_request(crispasr, fake_crispasr):
    await chunk(crispasr)
    (request,) = fake_crispasr.requests
    assert (request["method"], request["path"]) == ("POST", "/v1/audio/transcriptions")
    assert request["headers"]["authorization"] == "Bearer secret-key"
    assert request["form"] == {
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
        "language": "fr",
    }


async def test_chunk_never_sends_hotwords(crispasr, fake_crispasr):
    await chunk(crispasr, phrases=["Septeo"])
    form = fake_crispasr.requests[0]["form"]
    assert "hotwords" not in form and "hotwords_boost" not in form


@pytest.mark.parametrize(("received", "sent"), [("fr-FR", "fr"), ("en-US", "en"), ("auto", "auto")])
async def test_language_is_iso(crispasr, fake_crispasr, received, sent):
    await chunk(crispasr, language=received)
    assert fake_crispasr.requests[0]["form"]["language"] == sent


@pytest.mark.parametrize(("status", "expected"), [(503, False), (200, True)])
async def test_ready_is_false_while_loading(crispasr, fake_crispasr, status, expected):
    fake_crispasr.health_statuses = [status]
    assert await crispasr.ready() is expected
    assert fake_crispasr.requests[0]["path"] == "/health"


async def test_ready_does_not_send_the_key(crispasr, fake_crispasr):
    await crispasr.ready()
    assert "authorization" not in fake_crispasr.requests[0]["headers"]


async def test_ready_is_false_for_a_200_with_another_status():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "loading"}))
    adapter = CrispasrTranscriber("http://crispasr.test", "k", httpx.AsyncClient(transport=transport))
    assert await adapter.ready() is False


async def test_unreachable_server_is_unavailable():
    adapter = failing(httpx.ConnectError("refused"))
    with pytest.raises(EngineUnavailable):
        await chunk(adapter)
    assert await adapter.ready() is False


async def test_timeout_is_unavailable():
    with pytest.raises(EngineUnavailable):
        await chunk(failing(httpx.ReadTimeout("too slow")))


async def test_loading_server_is_unavailable(crispasr, fake_crispasr):
    fake_crispasr.transcription_statuses = [503]
    with pytest.raises(EngineUnavailable):
        await chunk(crispasr)


async def test_429_is_overloaded(crispasr, fake_crispasr):
    fake_crispasr.transcription_statuses = [429]
    with pytest.raises(EngineOverloaded):
        await chunk(crispasr)


async def test_error_message_does_not_quote_the_body(crispasr, fake_crispasr):
    fake_crispasr.transcription_statuses = [500]
    with pytest.raises(EngineError) as raised:
        await chunk(crispasr)
    assert FakeCrispasr.SECRET not in str(raised.value)


async def test_unreadable_answer_is_an_error(crispasr, fake_crispasr):
    fake_crispasr.fixture = "progress.json"
    with pytest.raises(EngineError):
        await chunk(crispasr)


# Une tâche sans locuteurs passe par les morceaux de l'adaptateur.


@pytest.fixture
def crispasr_services(store, settings, fake_llm, crispasr):
    nemo = FakeNemo()
    return Services(store, Engines(stt=crispasr, live=nemo, tts=nemo), settings, fake_llm.http)


async def test_task_runs_on_crispasr_chunks(crispasr_services, fake_crispasr):
    result = await process(crispasr_services, audio_task(crispasr_services, 3.0), RunStats())
    assert result.text == CHUNK["text"].strip()
    assert result.words[0] == Word("Bonjour,", 0.0, 0.64)
    assert [r["path"] for r in fake_crispasr.requests] == ["/v1/audio/transcriptions"]


@pytest.mark.usefixtures("no_retry_delays")
async def test_task_waits_while_engine_loads(crispasr_services, fake_crispasr):
    fake_crispasr.transcription_statuses = [503, 200]
    fake_crispasr.health_statuses = [503, 200]
    stats = RunStats()
    result = await process(crispasr_services, audio_task(crispasr_services, 3.0), stats)
    assert result.text == CHUNK["text"].strip()
    assert stats.retries == 1
    assert [r["path"] for r in fake_crispasr.requests].count("/health") == 2


# Un fichier entier : avec les personnes en une requête, sans elles par morceaux coupés aux silences.


async def run(adapter, wav, **kwargs) -> list:
    """Les événements de `transcribe` sur `wav`, en français et sans glossaire."""
    kwargs = {"language": "fr-FR", "phrases": (), "boost": 1.0, **kwargs}
    async with contextlib.aclosing(adapter.transcribe(wav, **kwargs)) as events:
        return [event async for event in events]


@pytest.fixture
def wav_40s(tmp_path):
    path = tmp_path / "audio.wav"
    write_wav(path, 40.0)
    return path


@pytest.fixture
def fast_progress(monkeypatch):
    monkeypatch.setattr(crispasr_module, "PROGRESS_EVERY_S", 0.01)


async def test_speakers_request_asks_for_sortformer(tmp_path, fake_crispasr, crispasr):
    fake_crispasr.fixture = "diarized.json"
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    (final,) = await run(crispasr, wav, speakers=True)
    (request,) = [r for r in fake_crispasr.requests if r["path"] == "/v1/audio/transcriptions"]
    assert request["form"] == {
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
        "language": "fr",
        "diarize": "true",
        "diarize_method": "sortformer",
    }
    expected = [
        Word(w["word"], w["start"], w["end"], segment["speaker"])
        for segment in DIARIZED["segments"]
        for w in segment["words"]
    ]
    assert final.words == tuple(expected)
    assert {word.speaker for word in final.words} == {"A", "B"}
    # Les lettres du serveur passent telles quelles par `label_speakers`, qui les garde dans l'ordre d'apparition.
    assert [w.speaker for w in label_speakers(final.words)] == [w.speaker for w in final.words]


async def test_speakers_send_the_whole_file_in_one_request(tmp_path, fake_crispasr, crispasr):
    wav = tmp_path / "audio.wav"
    write_wav(wav, 50.0)
    await run(crispasr, wav, speakers=True)
    assert [r["path"] for r in fake_crispasr.requests].count("/v1/audio/transcriptions") == 1


@pytest.mark.usefixtures("fast_progress")
async def test_progress_is_reported_as_fraction(tmp_path, fake_crispasr, crispasr):
    fake_crispasr.progress_body = {"busy": True, "progress": 42}
    fake_crispasr.transcription_delay = 0.1
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    seen: list[float] = []
    await run(crispasr, wav, speakers=True, on_progress=seen.append)
    assert seen and set(seen) == {0.42}


@pytest.mark.usefixtures("fast_progress")
async def test_a_negative_progress_is_not_reported(tmp_path, fake_crispasr, crispasr):
    fake_crispasr.progress_body = {"busy": False, "progress": -1}
    fake_crispasr.transcription_delay = 0.1
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    seen: list[float] = []
    await run(crispasr, wav, speakers=True, on_progress=seen.append)
    assert seen == []
    assert any(r["path"] == "/progress" for r in fake_crispasr.requests)


@pytest.mark.usefixtures("fast_progress")
@pytest.mark.parametrize("body", [{"oops": 1}, {"progress": "x"}])
async def test_an_unreadable_progress_does_not_fail_the_transcription(tmp_path, fake_crispasr, crispasr, body):
    fake_crispasr.progress_body = body
    fake_crispasr.transcription_delay = 0.1
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    (final,) = await run(crispasr, wav, speakers=True, on_progress=lambda fraction: None)
    assert final.text == CHUNK["text"].strip()


@pytest.mark.usefixtures("fast_progress")
async def test_a_failing_progress_route_does_not_fail_the_transcription(tmp_path, monkeypatch):
    fake = FakeCrispasr()
    fake.transcription_delay = 0.1
    inner = fake.http._transport

    async def handler(request):
        if request.url.path == "/progress":
            raise httpx.ConnectError("down")
        return await inner.handle_async_request(request)

    adapter = CrispasrTranscriber(
        "http://crispasr.test", "k", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    (final,) = await run(adapter, wav, speakers=True, on_progress=lambda fraction: None)
    assert final.text == CHUNK["text"].strip()


@pytest.mark.usefixtures("fast_progress")
async def test_an_error_raised_by_on_progress_reaches_the_caller(tmp_path, fake_crispasr, crispasr):
    fake_crispasr.progress_body = {"busy": True, "progress": 42}
    fake_crispasr.transcription_delay = 30.0
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)

    def broken(fraction: float) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await asyncio.wait_for(run(crispasr, wav, speakers=True, on_progress=broken), timeout=5)


@pytest.mark.usefixtures("fast_progress")
async def test_closing_the_stream_stops_the_request_and_the_progress_reading(tmp_path, fake_crispasr, crispasr):
    fake_crispasr.transcription_delay = 30.0
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(run(crispasr, wav, speakers=True, on_progress=lambda fraction: None))
    await asyncio.sleep(0.1)
    assert any(r["path"] == "/progress" for r in fake_crispasr.requests)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    seen = len(fake_crispasr.requests)
    await asyncio.sleep(0.1)
    assert len(fake_crispasr.requests) == seen
    assert set(asyncio.all_tasks()) <= before | {asyncio.current_task()}


async def test_speakers_error_is_translated(tmp_path, fake_crispasr, crispasr):
    fake_crispasr.transcription_statuses = [429]
    wav = tmp_path / "audio.wav"
    write_wav(wav, 3.0)
    with pytest.raises(EngineOverloaded):
        await run(crispasr, wav, speakers=True)


async def test_stream_yields_one_final_per_chunk(wav_40s, fake_crispasr, crispasr, monkeypatch):
    async def silences(wav, duration):
        return [Silence(11.8, 12.2), Silence(25.8, 26.2)]

    monkeypatch.setattr(crispasr_module, "detect_silences", silences)
    progress: list[float] = []
    events = await run(crispasr, wav_40s, partials=True, on_progress=progress.append)
    assert all(isinstance(event, Final) for event in events)
    assert len(events) == 3
    assert [event.words[0].start for event in events] == pytest.approx([0.0, 12.0, 26.0])
    assert events[1].words[-1].end == pytest.approx(12.0 + CHUNK["segments"][-1]["words"][-1]["end"])
    assert all(word.speaker is None for event in events for word in event.words)
    assert progress == pytest.approx([12 / 40, 26 / 40, 1.0])
    assert [r["path"] for r in fake_crispasr.requests] == ["/v1/audio/transcriptions"] * 3
    assert all("diarize" not in r["form"] for r in fake_crispasr.requests)


async def test_a_short_file_is_one_chunk(tmp_path, fake_crispasr, crispasr):
    wav = tmp_path / "audio.wav"
    write_wav(wav, STREAM_CHUNK_S)
    events = await run(crispasr, wav)
    assert len(events) == 1 and events[0].words[0] == Word("Bonjour,", 0.0, 0.64)


async def test_stream_chunks_stay_under_the_limit(wav_40s, crispasr, monkeypatch):
    async def silences(wav, duration):
        return []

    monkeypatch.setattr(crispasr_module, "detect_silences", silences)
    events = await run(crispasr, wav_40s)
    assert len(events) == len(chunk_bounds(40.0, cuts.plan_cuts(40.0, [], STREAM_CHUNK_S)))
    assert len(events) >= 3
