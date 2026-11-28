"""Les trois rôles du moteur audio : fabrique, choix par réglage et capacités déclarées."""

from dataclasses import replace

import anyio
import httpx
import pytest
from conftest import REQUIRED_ENV
from fakes import FakeNemo

from dictee.engines import Engines
from dictee.engines.crispasr import CrispasrTranscriber
from dictee.engines.crispasr_stream import CrispasrLive
from dictee.engines.factory import _check, build_engines
from dictee.engines.nemo import NemoClient
from dictee.engines.openai_tts import OpenAISpeech
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
    # `Settings.from_env` refuse déjà ce nom : la fabrique garde sa propre garde pour des réglages construits autrement.
    settings = replace(_settings(), **{variable.lower(): "foo"})
    with pytest.raises(ValueError, match=rf"{variable}.*foo"):
        build_engines(settings, http)


def test_crispasr_replaces_only_the_file_transcriber(http):
    engines = build_engines(_settings(STT_ENGINE="crispasr", CRISPASR_API_KEY="key"), http)

    assert isinstance(engines.stt, CrispasrTranscriber)
    assert isinstance(engines.live, NemoClient)
    assert engines.tts is engines.live


def test_crispasr_receives_the_diarization_method(http, monkeypatch):
    created = []
    monkeypatch.setattr(
        "dictee.engines.factory.CrispasrTranscriber",
        lambda *args, **kwargs: created.append((args, kwargs)) or FakeNemo(),
    )

    build_engines(_settings(STT_ENGINE="crispasr", CRISPASR_API_KEY="key", DIAR_METHOD="pyannote"), http)

    assert created == [(("http://crispasr:8080", "key", http), {"diar_method": "pyannote"})]


def test_crispasr_leaves_nemo_shared_by_the_other_roles(http):
    engines = build_engines(_settings(STT_ENGINE="crispasr", CRISPASR_API_KEY="key"), http)

    assert isinstance(engines.stt, CrispasrTranscriber)
    assert engines.live is engines.tts


def test_crispasr_is_not_built_when_no_role_chooses_it(http, monkeypatch):
    created = []
    monkeypatch.setattr("dictee.engines.factory.CrispasrTranscriber", lambda *args: created.append(args))

    engines = build_engines(_settings(), http)

    assert created == []
    assert engines.stt is engines.live is engines.tts


def test_nemo_declares_its_capabilities(http):
    client = NemoClient("http://nemo.test", "key", http)

    assert client.speakers is True
    assert client.sample_rate == 22050


def test_engine_without_speakers_is_refused_at_startup():
    fake = FakeNemo()
    fake.speakers = False

    with pytest.raises(ValueError, match="STT_ENGINE.*dictee-diarize"):
        _check(Engines(stt=fake, live=fake, tts=fake))


def test_openai_replaces_only_the_synthesizer(http):
    engines = build_engines(
        _settings(TTS_ENGINE="openai", TTS_URL="http://phonon.test:8880", TTS_SAMPLE_RATE="24000"), http
    )

    assert isinstance(engines.tts, OpenAISpeech)
    assert engines.tts.sample_rate == 24000
    assert isinstance(engines.stt, NemoClient)
    assert engines.live is engines.stt


def test_crispasr_stream_replaces_only_the_live_transcriber(http):
    engines = build_engines(_settings(LIVE_ENGINE="crispasr-stream"), http)

    assert isinstance(engines.live, CrispasrLive)
    assert isinstance(engines.stt, NemoClient)
    assert engines.tts is engines.stt


def test_crispasr_stream_receives_its_settings_with_split_stream_args(http, monkeypatch):
    created = []
    monkeypatch.setattr(
        "dictee.engines.factory.CrispasrLive", lambda *args, **kwargs: created.append((args, kwargs)) or FakeNemo()
    )

    build_engines(
        _settings(
            LIVE_ENGINE="crispasr-stream",
            LIVE_COMMAND="/opt/crispasr/crispasr",
            LIVE_MODEL_PATH="/models/live.gguf",
            LIVE_VAD_MODEL_PATH="/models/vad.bin",
            LIVE_MAX_SESSIONS="3",
            LIVE_THREADS="2",
            LIVE_STREAM_ARGS="--stream-length 8000 --keep 'a b'",
        ),
        http,
    )

    assert created == [
        (
            ("/opt/crispasr/crispasr", "/models/live.gguf"),
            {
                "vad_model_path": "/models/vad.bin",
                "max_sessions": 3,
                "threads": 2,
                "stream_args": ["--stream-length", "8000", "--keep", "a b"],
            },
        )
    ]
