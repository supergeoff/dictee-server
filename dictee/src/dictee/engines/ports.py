"""Les trois rôles du moteur audio et leurs erreurs communes.

dictee confie la transcription d'un fichier, la dictée en direct et la synthèse vocale à trois rôles décrits ici par des
protocoles. Un adaptateur remplit un ou plusieurs rôles et déclare ses capacités par des attributs : le code des routes
lit ces attributs au lieu de supposer un moteur précis. Les adaptateurs lèvent les trois erreurs de ce module, que les
routes savent rendre (voir `dictee.errors.engine_failure`). Rien n'est journalisé ici.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from dictee.types import Cleared, Committed, Final, Partial


class EngineUnavailable(Exception):
    """Le moteur ne répond pas : connexion refusée ou coupée, délai dépassé."""


class EngineOverloaded(Exception):
    """La file de requêtes du moteur est pleine (HTTP 429 ou événement `resource_exhausted`)."""


class EngineError(Exception):
    """Le moteur a refusé la requête, signalé une erreur ou cessé d'émettre pendant une session."""


# Les échecs du moteur que `engine_failure` sait rendre : une route les attrape par ce tuple.
ENGINE_ERRORS = (EngineOverloaded, EngineUnavailable, EngineError)


class FileTranscriber(Protocol):
    """Transcription d'un fichier WAV PCM16 mono 16 kHz, d'un seul bloc ou par morceaux.

    `speakers` dit si le moteur marque les personnes (`Word.speaker`). `language` est le code que
    `dictee.modes.to_nemo_language` produit (`fr-FR` ou `auto`). `on_progress`, s'il est donné, reçoit les secondes
    d'audio déjà traitées (et non une fraction) : l'appelant les rapporte lui-même à la durée du fichier.
    """

    speakers: bool

    def transcribe(
        self,
        wav: Path,
        *,
        language: str,
        phrases: Sequence[str],
        boost: float,
        speakers: bool = False,
        partials: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> AsyncIterator[Final | Partial]:
        """Transcrit `wav` et rend un `Final` par phrase, avec des `Partial` si `partials` est vrai."""
        ...

    async def transcribe_chunk(self, wav: bytes, *, language: str, phrases: Sequence[str], boost: float) -> Final:
        """Transcrit un morceau d'audio donné en octets WAV et rend son `Final`."""
        ...

    async def ready(self) -> bool:
        """Dit si le moteur est prêt à transcrire."""
        ...


class LiveSession(Protocol):
    """Session de dictée en direct ouverte par `LiveTranscriber.live`."""

    async def send_audio(self, pcm: bytes) -> None:
        """Envoie du PCM16 mono, à la fréquence déclarée à l'ouverture."""
        ...

    async def commit(self) -> None:
        """Valide la prise de parole en cours."""
        ...

    async def clear(self) -> None:
        """Vide l'audio de la prise de parole en cours."""
        ...

    def events(self) -> AsyncIterator[Partial | Final | Committed | Cleared]:
        """Rend ce que le moteur répond, dans l'ordre d'arrivée."""
        ...


class LiveTranscriber(Protocol):
    """Dictée en direct.

    `server_vad` dit si le moteur découpe lui-même les prises de parole sur le silence, `many_commits` s'il accepte
    plusieurs commits dans une même session (sinon la session se ferme après son premier `Committed`).
    """

    server_vad: bool
    many_commits: bool

    def live(
        self,
        *,
        sample_rate: int,
        language: str,
        phrases: Sequence[str],
        boost: float,
        endpointing_ms: int | None = None,
    ) -> AbstractAsyncContextManager[LiveSession]:
        """Ouvre une session de dictée en direct, fermée à la sortie du bloc `async with`."""
        ...


class Synthesizer(Protocol):
    """Synthèse vocale : `speak` rend du PCM16 mono à `sample_rate` Hz."""

    sample_rate: int

    async def speak(self, text: str, *, voice: str, language: str) -> bytes:
        """Synthétise `text` avec la voix `voice`."""
        ...

    async def voices(self) -> list[str]:
        """Liste les voix disponibles."""
        ...

    async def ready(self) -> bool:
        """Dit si le moteur est prêt à synthétiser."""
        ...
