"""Types partagés entre le client NeMo, la reformulation et les routes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    speaker: str | None = None


@dataclass(frozen=True)
class Final:
    """Phrase finale d'une session de transcription en continu."""

    text: str
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Partial:
    """Morceau de texte que NeMo produit en cours de session, avant la phrase finale de son item."""

    text: str


@dataclass(frozen=True)
class Turn:
    """Tour de parole renvoyé par la séparation des locuteurs."""

    start: float
    end: float
    speaker: int


@dataclass(frozen=True)
class Segment:
    id: str
    start: float
    end: float
    speaker: str
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    words: tuple[Word, ...]
    duration: float
    language: str
    segments: tuple[Segment, ...] | None = None
