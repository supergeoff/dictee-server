"""Les moteurs audio de dictee : un adaptateur par rôle, choisi par `STT_ENGINE`, `LIVE_ENGINE` et `TTS_ENGINE`."""

from dataclasses import dataclass

import httpx

from dictee.engines.crispasr import CrispasrTranscriber
from dictee.engines.nemo import NemoClient
from dictee.engines.ports import FileTranscriber, LiveTranscriber, Synthesizer
from dictee.settings import Settings


@dataclass(frozen=True)
class Engines:
    """Les adaptateurs qui jouent les trois rôles."""

    stt: FileTranscriber
    live: LiveTranscriber
    tts: Synthesizer


def _check(engines: Engines) -> Engines:
    """Refuse au démarrage un choix que les capacités déclarées rendent incohérent ; rend `engines` tel quel."""
    if not engines.stt.speakers:
        raise ValueError(
            "STT_ENGINE does not mark speakers, which the model dictee-diarize needs: choose an engine that does"
        )
    return engines


def build_engines(settings: Settings, http: httpx.AsyncClient) -> Engines:
    """Construit les adaptateurs que les réglages nomment ; `ValueError` si un nom est inconnu ou le choix incohérent.

    Le moteur « nemo » est un seul client, partagé par les rôles qui le choisissent. Le moteur « crispasr » ne joue que
    le rôle de transcription de fichier (`STT_ENGINE`).
    """
    nemo = NemoClient(settings.nemo_url, settings.nemo_api_key, http)
    adapters = {
        "STT_ENGINE": {
            "nemo": nemo,
            "crispasr": CrispasrTranscriber(settings.crispasr_url, settings.crispasr_api_key, http),
        },
        "LIVE_ENGINE": {"nemo": nemo},
        "TTS_ENGINE": {"nemo": nemo},
    }
    chosen = []
    for variable, name in (
        ("STT_ENGINE", settings.stt_engine),
        ("LIVE_ENGINE", settings.live_engine),
        ("TTS_ENGINE", settings.tts_engine),
    ):
        if name not in adapters[variable]:
            raise ValueError(f"{variable} must be one of {', '.join(adapters[variable])}, got {name!r}")
        chosen.append(adapters[variable][name])
    return _check(Engines(stt=chosen[0], live=chosen[1], tts=chosen[2]))
