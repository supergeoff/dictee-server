"""Les trois rôles du moteur audio : fabrique, choix par réglage et capacités déclarées."""

import anyio
import httpx
import pytest
from conftest import REQUIRED_ENV
from fakes import FakeNemo

from dictee.engines import Engines, _check, build_engines
from dictee.engines.crispasr import CrispasrTranscriber
from dictee.engines.nemo import NemoClient
from dictee.settings import Settings


def _settings(**env: str) -> Settings:
    return Settings.from_env({**REQUIRED_ENV, **env})


@pytest.fixture
def http():
    client = httpx.AsyncClient()
    yield client
    anyio.run(client.aclose)


def test_build_engines_defaults_to_nemo(http):
    engines = build_engines(_settings(), http)

    assert isinstance(engines.stt, NemoClient)
    assert engines.live is engines.stt
    assert engines.tts is engines.stt


@pytest.mark.parametrize("variable", ["STT_ENGINE", "LIVE_ENGINE", "TTS_ENGINE"])
def test_unknown_engine_names_the_variable(variable, http):
    with pytest.raises(ValueError, match=rf"{variable}.*foo"):
        build_engines(_settings(**{variable: "foo"}), http)


def test_crispasr_replaces_only_the_file_transcriber(http):
    engines = build_engines(_settings(STT_ENGINE="crispasr", CRISPASR_API_KEY="key"), http)

    assert isinstance(engines.stt, CrispasrTranscriber)
    assert isinstance(engines.live, NemoClient)
    assert engines.tts is engines.live


def test_nemo_declares_its_capabilities(http):
    client = NemoClient("http://nemo.test", "key", http)

    assert client.speakers is True
    assert client.progress is True
    assert client.sample_rate == 22050


def test_engine_without_speakers_is_refused_at_startup():
    fake = FakeNemo()
    fake.speakers = False

    with pytest.raises(ValueError, match="STT_ENGINE.*dictee-diarize"):
        _check(Engines(stt=fake, live=fake, tts=fake))
