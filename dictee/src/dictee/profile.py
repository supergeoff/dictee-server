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

# Début des déformations d'une ligne : « (souvent entendu : » (forme de la page) ou « (often heard as: » (forme de
# l'ancien proxy). Chaque `\s*` est suivi d'un caractère qui n'est pas une espace, donc sans retour en arrière coûteux.
_HINTS_OPENING = re.compile(r"\(\s*(?:souvent entendu|often heard as)\s*:\s*", re.IGNORECASE)


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


def _split_hints(line: str) -> tuple[str, str] | None:
    """Sépare `Terme (souvent entendu : a, b)` en terme et déformations ; `None` si la ligne n'a pas cette forme.

    La ligne doit être épurée et finir par `)`. Le terme s'arrête à la première parenthèse suivie du marqueur.
    Les parenthèses se parcourent une à une : une seule expression régulière, avec ses espaces ambigus de part et
    d'autre des déformations, reviendrait en arrière de façon cubique sur une longue suite d'espaces.
    """
    if not line.endswith(")"):
        return None
    opening = line.find("(", 1)
    while opening != -1:
        marker = _HINTS_OPENING.match(line, opening)
        if marker:
            return line[:opening].rstrip(), line[marker.end() : -1]
        opening = line.find("(", opening + 1)
    return None


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
        split = _split_hints(line)
        if split is None:
            entries.append(GlossaryEntry(line))
            continue
        term, hints_text = split
        hints = tuple(hint for hint in (part.strip() for part in hints_text.split(",")) if hint)
        entries.append(GlossaryEntry(term, hints))
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
