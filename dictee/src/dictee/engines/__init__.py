"""Les moteurs audio de dictee : un adaptateur par rôle, choisi par `STT_ENGINE`, `LIVE_ENGINE` et `TTS_ENGINE`.

Ce fichier n'importe aucun adaptateur : `dictee.errors` importe `dictee.engines.ports`, ce qui exécute ce fichier, et un
adaptateur importe `dictee.modes`, qui importe `dictee.errors`. Les adaptateurs se construisent dans
`dictee.engines.factory`.
"""

from dataclasses import dataclass

from dictee.engines.ports import FileTranscriber, LiveTranscriber, Synthesizer


@dataclass(frozen=True)
class Engines:
    """Les adaptateurs qui jouent les trois rôles."""

    stt: FileTranscriber
    live: LiveTranscriber
    tts: Synthesizer
