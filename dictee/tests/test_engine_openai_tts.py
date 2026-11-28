"""L'adaptateur de voix compatible OpenAI : requête de synthèse, liste des voix, file d'attente et erreurs."""

import asyncio

import httpx
import pytest
from fakes import FakeVoiceServer

from dictee.engines import openai_tts
from dictee.engines.openai_tts import OpenAISpeech
from dictee.engines.ports import EngineError, EngineOverloaded, EngineUnavailable

pytestmark = pytest.mark.anyio

PCM = bytes(range(64))


@pytest.fixture
def server():
    return FakeVoiceServer(pcm=PCM)


def adapter(server: FakeVoiceServer, *, max_concurrent: int = 1) -> OpenAISpeech:
    return OpenAISpeech("http://phonon.test/", server.http, sample_rate=24000, max_concurrent=max_concurrent)


@pytest.fixture
def quick_retries(monkeypatch):
    """Réduit l'attente entre deux essais et sa durée maximale : les tests ne dorment pas vraiment."""
    monkeypatch.setattr(openai_tts, "RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(openai_tts, "RETRY_MAX_S", 0.1)


async def test_speak_asks_for_pcm_without_speed(server):
    assert await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR") == PCM
    (request,) = server.speech_requests
    assert request["method"] == "POST"
    assert request["json"] == {"model": "tts-1", "input": "Bonjour.", "voice": "Lucien", "response_format": "pcm"}
    assert "authorization" not in request["headers"]


async def test_the_adapter_declares_its_sample_rate(server):
    assert adapter(server).sample_rate == 24000


async def test_voices_reads_the_voices_list(server):
    server.voices = ["Lucien", "Arnaud"]
    assert await adapter(server).voices() == ["Lucien", "Arnaud"]
    assert server.requests[-1]["path"] == "/v1/audio/voices"


async def test_unreadable_voices_answer_is_an_engine_error():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"nope": 1})))
    with pytest.raises(EngineError):
        await OpenAISpeech("http://phonon.test", http, sample_rate=24000, max_concurrent=1).voices()


async def test_ready_reads_health(server):
    assert await adapter(server).ready() is True
    assert server.requests[-1]["path"] == "/health"
    server.unreachable = True
    assert await adapter(server).ready() is False


async def test_calls_wait_for_their_turn(server):
    server.speech_delay = 0.05
    voice = adapter(server, max_concurrent=1)
    await asyncio.gather(
        voice.speak("Un.", voice="Lucien", language="fr-FR"), voice.speak("Deux.", voice="Lucien", language="fr-FR")
    )
    assert len(server.speech_requests) == 2
    assert server.max_active == 1


async def test_calls_run_together_up_to_the_number_of_places(server):
    server.speech_delay = 0.05
    voice = adapter(server, max_concurrent=2)
    await asyncio.gather(*(voice.speak("Un.", voice="Lucien", language="fr-FR") for _ in range(3)))
    assert server.max_active == 2


async def test_busy_server_is_retried_then_overloaded(server, quick_retries):
    server.speech_statuses = [429]
    with pytest.raises(EngineOverloaded):
        await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR")
    assert len(server.speech_requests) > 1


async def test_busy_server_that_recovers_gives_the_sound(server, quick_retries):
    server.speech_statuses = [429, 429, 200]
    assert await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR") == PCM
    assert len(server.speech_requests) == 3


async def test_unreachable_server_is_unavailable(server):
    server.unreachable = True
    with pytest.raises(EngineUnavailable):
        await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR")


async def test_error_answers_are_fixed_messages_that_do_not_quote_the_body(server):
    server.speech_statuses = [500]
    with pytest.raises(EngineError) as raised:
        await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR")
    assert FakeVoiceServer.SECRET not in str(raised.value) and "500" in str(raised.value)
    server.speech_statuses = [200]
    with pytest.raises(EngineError) as raised:  # voix inconnue : 400
        await adapter(server).speak("Bonjour.", voice="Inconnue", language="fr-FR")
    assert FakeVoiceServer.SECRET not in str(raised.value) and "400" in str(raised.value)
    server.speech_statuses = [503]
    with pytest.raises(EngineUnavailable):
        await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR")


async def test_unreachable_message_is_fixed(server):
    server.unreachable = True
    with pytest.raises(EngineUnavailable) as raised:
        await adapter(server).speak("Bonjour.", voice="Lucien", language="fr-FR")
    assert str(raised.value) == "The voice server is unreachable"
    assert isinstance(raised.value.__cause__, httpx.ConnectError)
