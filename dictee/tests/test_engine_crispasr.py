"""L'adaptateur CrispASR : morceaux de fichier, disponibilité, erreurs, et une tâche jouée de bout en bout."""

import json

import httpx
import pytest
from conftest import audio_task
from fakes import CRISPASR_FIXTURES, FakeCrispasr, FakeNemo

from dictee.engines import Engines
from dictee.engines.crispasr import CrispasrTranscriber
from dictee.engines.ports import EngineError, EngineOverloaded, EngineUnavailable
from dictee.processing import RunStats, Services, process
from dictee.types import Word

pytestmark = pytest.mark.anyio

CHUNK = json.loads((CRISPASR_FIXTURES / "chunk.json").read_text())


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
