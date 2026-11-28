"""Les trois rôles du moteur audio : fabrique, choix par réglage et capacités déclarées."""

import httpx
import pytest
from conftest import REQUIRED_ENV
from fakes import FakeNemo

from dictee.engines import Engines, _check, build_engines
from dictee.engines.nemo import NemoClient
from dictee.settings import Settings


def _settings(**env: str) -> Settings:
    return Settings.from_env({**REQUIRED_ENV, **env})


def test_build_engines_defaults_to_nemo():
    engines = build_engines(_settings(), httpx.AsyncClient())

    assert isinstance(engines.stt, NemoClient)
    assert engines.live is engines.stt
    assert engines.tts is engines.stt


@pytest.mark.parametrize("variable", ["STT_ENGINE", "LIVE_ENGINE", "TTS_ENGINE"])
def test_unknown_engine_names_the_variable(variable):
    with pytest.raises(ValueError, match=rf"{variable}.*foo"):
        build_engines(_settings(**{variable: "foo"}), httpx.AsyncClient())


def test_nemo_declares_its_capabilities():
    client = NemoClient("http://nemo.test", "key", httpx.AsyncClient())

    assert client.speakers is True
    assert client.progress is True
    assert client.sample_rate == 22050


def test_engine_without_speakers_is_refused_at_startup():
    fake = FakeNemo()
    fake.speakers = False

    with pytest.raises(ValueError, match="STT_ENGINE.*dictee-diarize"):
        _check(Engines(stt=fake, live=fake, tts=fake))
