"""Correction du glossaire : dictee remplace lui-même, après la transcription, chaque variante entendue par son terme.

Les moteurs n'appliquent pas le glossaire de l'appelant. La correction est déterministe : une variante se reconnaît
par mot entier, sans tenir compte de la casse, et les variantes les plus longues passent d'abord. Elle ne journalise
rien.
"""

import re
from collections.abc import Iterable, Sequence

from dictee.profile import GlossaryEntry
from dictee.types import Final, Word

_LEADING = "«\"'(¿¡"
_TRAILING = ".,;:!?…»\"')"


def _key(variant: str) -> str:
    return " ".join(variant.casefold().split())


def _terms_by_variant(entries: Iterable[GlossaryEntry]) -> dict[str, str]:
    """Terme de chaque variante (clé repliée en minuscules, espaces simples) ; la première entrée l'emporte."""
    terms: dict[str, str] = {}
    for entry in entries:
        for variant in entry.heard_as:
            if key := _key(variant):
                terms.setdefault(key, entry.term)
    return terms


def correct_text(text: str, entries: Iterable[GlossaryEntry]) -> str:
    """Le texte dont chaque variante entendue est remplacée par son terme."""
    terms = _terms_by_variant(entries)
    if not terms:
        return text
    alternatives = (r"\s+".join(map(re.escape, key.split())) for key in sorted(terms, key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.IGNORECASE)
    return pattern.sub(lambda match: terms[_key(match.group())], text)


def _word_pattern(token: str, *, first: bool, last: bool) -> re.Pattern[str]:
    lead = f"(?P<lead>[{re.escape(_LEADING)}]*)" if first else ""
    trail = f"(?P<trail>[{re.escape(_TRAILING)}]*)" if last else ""
    return re.compile(f"{lead}{re.escape(token)}{trail}", re.IGNORECASE)


def correct_words(words: Sequence[Word], entries: Iterable[GlossaryEntry]) -> tuple[Word, ...]:
    """Les mots dont chaque suite égale à une variante devient un seul mot du terme.

    Le nouveau mot commence où commence le premier, finit où finit le dernier et garde la personne du premier. La
    ponctuation collée à ses extrémités est gardée.
    """
    terms = _terms_by_variant(entries)
    patterns = [
        (
            terms[key],
            [_word_pattern(token, first=i == 0, last=i == len(tokens) - 1) for i, token in enumerate(tokens)],
        )
        for key, tokens in sorted(((key, key.split()) for key in terms), key=lambda pair: len(pair[1]), reverse=True)
    ]
    result: list[Word] = []
    index = 0
    while index < len(words):
        for term, parts in patterns:
            span = words[index : index + len(parts)]
            if len(span) < len(parts):
                continue
            matches = [part.fullmatch(word.text) for part, word in zip(parts, span)]
            if all(matches):
                lead = matches[0].groupdict().get("lead") or ""
                trail = matches[-1].groupdict().get("trail") or ""
                result.append(Word(f"{lead}{term}{trail}", span[0].start, span[-1].end, span[0].speaker))
                index += len(parts)
                break
        else:
            result.append(words[index])
            index += 1
    return tuple(result)


def correct_final(final: Final, entries: Iterable[GlossaryEntry]) -> Final:
    """Le texte final et ses mots, corrigés ; une suite de mots vide reste vide."""
    entries = list(entries)
    return Final(correct_text(final.text, entries), correct_words(final.words, entries))
