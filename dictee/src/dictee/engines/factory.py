"""Construction des adaptateurs audio que les réglages nomment."""

import httpx

from dictee.engines import Engines
from dictee.engines.crispasr import CrispasrTranscriber
from dictee.engines.nemo import NemoClient
from dictee.engines.openai_tts import OpenAISpeech
from dictee.settings import Settings


def _check(engines: Engines) -> Engines:
    """Refuse au démarrage un choix que les capacités déclarées rendent incohérent ; rend `engines` tel quel."""
    if not engines.stt.speakers:
        raise ValueError(
            "STT_ENGINE does not mark speakers, which the model dictee-diarize needs: choose an engine that does"
        )
    return engines


def build_engines(settings: Settings, http: httpx.AsyncClient) -> Engines:
    """Construit les adaptateurs que les réglages nomment ; `ValueError` si un nom est inconnu ou le choix incohérent.

    Un adaptateur n'est construit que si un rôle le choisit. Le moteur « nemo » est un seul client, partagé par les
    rôles qui le choisissent. Le moteur « crispasr » ne joue que le rôle de transcription de fichier (`STT_ENGINE`),
    et le moteur « openai » que celui de synthèse (`TTS_ENGINE`).
    """

    def nemo() -> NemoClient:
        return NemoClient(settings.nemo_url, settings.nemo_api_key, http)

    def crispasr() -> CrispasrTranscriber:
        return CrispasrTranscriber(settings.crispasr_url, settings.crispasr_api_key, http)

    def openai_speech() -> OpenAISpeech:
        return OpenAISpeech(
            settings.tts_url,
            http,
            sample_rate=settings.tts_sample_rate,
            max_concurrent=settings.tts_max_concurrent,
        )

    factories = {
        "STT_ENGINE": {"nemo": nemo, "crispasr": crispasr},
        "LIVE_ENGINE": {"nemo": nemo},
        "TTS_ENGINE": {"nemo": nemo, "openai": openai_speech},
    }
    built: dict[str, object] = {}
    chosen = []
    for variable, name in (
        ("STT_ENGINE", settings.stt_engine),
        ("LIVE_ENGINE", settings.live_engine),
        ("TTS_ENGINE", settings.tts_engine),
    ):
        if name not in factories[variable]:
            raise ValueError(f"{variable} must be one of {', '.join(factories[variable])}, got {name!r}")
        if name not in built:
            built[name] = factories[variable][name]()
        chosen.append(built[name])
    return _check(Engines(stt=chosen[0], live=chosen[1], tts=chosen[2]))
