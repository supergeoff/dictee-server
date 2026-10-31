"""Glossaire, consignes de reformulation et voix effectifs d'un appelant."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files

from dictee.auth import Caller
from dictee.db import Database, StoredSettings
from dictee.settings import Settings

# Le fichier voyage avec le paquet : c'est le texte de l'ancien proxy, neutre et sans rien de personnel.
DEFAULT_INSTRUCTIONS = files("dictee").joinpath("defaults/instructions.txt").read_text(encoding="utf-8").strip()

# « Terme (souvent entendu : a, b) » (forme de la page) ou « Terme (often heard as: a, b) » (forme de l'ancien proxy).
_HINTED_LINE = re.compile(
    r"(?P<term>.+?)\s*\(\s*(?:souvent entendu|often heard as)\s*:\s*(?P<hints>.*?)\s*\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GlossaryEntry:
    """Un terme du glossaire et les formes sous lesquelles la transcription l'entend parfois."""

    term: str
    heard_as: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    """Réglages effectifs d'un appelant : ce que la transcription et la synthèse utilisent."""

    glossary: tuple[GlossaryEntry, ...]
    instructions: str
    voice: str


def parse_glossary(text: str) -> list[GlossaryEntry]:
    """Lit un glossaire, un terme par ligne.

    Une ligne vide ou commençant par `#` est ignorée. Les déformations se donnent entre parenthèses,
    séparées par des virgules : `Terme (souvent entendu : a, b)`. Toute autre parenthèse reste dans le terme.
    """
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _HINTED_LINE.fullmatch(line)
        if match is None:
            entries.append(GlossaryEntry(line))
            continue
        hints = tuple(hint for hint in (part.strip() for part in match["hints"].split(",")) if hint)
        entries.append(GlossaryEntry(match["term"], hints))
    return entries


def glossary_terms(entries: Iterable[GlossaryEntry]) -> list[str]:
    """Termes seuls, tels que NeMo les reçoit pour renforcer leur reconnaissance."""
    return [entry.term for entry in entries]


def glossary_prompt(entries: Iterable[GlossaryEntry]) -> str:
    """Glossaire en anglais pour le modèle de reformulation ; chaîne vide s'il n'y a aucun terme."""
    lines = [
        f"- {entry.term} (often heard as: {', '.join(entry.heard_as)})" if entry.heard_as else f"- {entry.term}"
        for entry in entries
    ]
    return "Glossary:\n" + "\n".join(lines) if lines else ""


def load_profile(db: Database, settings: Settings, caller: Caller) -> Profile:
    """Réglages de l'appelant, ceux par défaut pour tout réglage vide ou fait d'espaces.

    Un appelant sans compte reçoit le glossaire vide, les consignes de `instructions.txt` et la voix de `Settings`.
    """
    stored = StoredSettings("", "", "") if caller.user_id is None else db.get_settings(caller.user_id)
    return Profile(
        glossary=tuple(parse_glossary(stored.glossary)),
        instructions=stored.instructions.strip() or DEFAULT_INSTRUCTIONS,
        voice=stored.voice.strip() or settings.default_voice,
    )
