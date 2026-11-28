"""Correction du glossaire : dictee remplace lui-même, après la transcription, chaque variante entendue par son terme.

Les moteurs n'appliquent pas le glossaire de l'appelant. La correction est déterministe : une variante se reconnaît
sans tenir compte de la casse quand elle est bornée par autre chose qu'un caractère de mot (début ou fin du texte,
espace, apostrophe, trait d'union, ponctuation), et les variantes les plus longues passent d'abord. La même règle vaut
pour le texte et pour les mots, que le moteur rend avec l'élision et la ponctuation collées (`d'tarvél,`).
L'apostrophe droite et l'apostrophe typographique se valent dans une variante, puisque les moteurs rendent l'une ou
l'autre selon le modèle. Elle ne journalise rien.
"""

import re
from collections.abc import Callable, Iterable, Sequence

from dictee.profile import GlossaryEntry
from dictee.types import Final, Word


_APOSTROPHES = "'’"


def _key(variant: str) -> str:
    """Clé d'une variante : en minuscules, à espaces simples et à apostrophe droite."""
    return " ".join(variant.casefold().replace("’", "'").split())


def _escape(token: str) -> str:
    """Le motif d'un mot de variante, où chaque apostrophe reconnaît les deux apostrophes."""
    return "".join(f"[{_APOSTROPHES}]" if char in _APOSTROPHES else re.escape(char) for char in token)


def _terms_by_variant(entries: Iterable[GlossaryEntry]) -> dict[str, str]:
    """Terme de chaque variante (clé repliée en minuscules, espaces simples) ; la première entrée l'emporte."""
    terms: dict[str, str] = {}
    for entry in entries:
        for variant in entry.heard_as:
            if key := _key(variant):
                terms.setdefault(key, entry.term)
    return terms


def _text_corrector(terms: dict[str, str]) -> Callable[[str], str]:
    """La fonction qui remplace, dans un texte, chaque variante de `terms` par son terme."""
    if not terms:
        return lambda text: text
    alternatives = (r"\s+".join(map(_escape, key.split())) for key in sorted(terms, key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.IGNORECASE)
    return lambda text: pattern.sub(lambda match: terms[_key(match.group())], text)


def correct_text(text: str, entries: Iterable[GlossaryEntry]) -> str:
    """Le texte dont chaque variante entendue est remplacée par son terme."""
    return _text_corrector(_terms_by_variant(entries))(text)


def _word_pattern(token: str, *, first: bool, last: bool) -> re.Pattern[str]:
    """Motif d'un mot qui porte `token` ; le premier mot garde ce qui précède la variante, le dernier ce qui la suit.

    Comme dans `correct_text`, la variante doit être bornée dans le mot par autre chose qu'un caractère de mot.
    """
    lead = r"(?P<lead>.*?)(?<!\w)" if first else ""
    trail = r"(?!\w)(?P<trail>.*)" if last else ""
    return re.compile(f"{lead}{_escape(token)}{trail}", re.IGNORECASE | re.DOTALL)


def correct_words(words: Sequence[Word], entries: Iterable[GlossaryEntry]) -> tuple[Word, ...]:
    """Les mots dont chaque suite égale à une variante devient un seul mot du terme.

    Le nouveau mot commence où commence le premier, finit où finit le dernier et garde la personne du premier. Ce qui
    entoure la variante dans le premier et le dernier mot (élision, préfixe et trait d'union, ponctuation) est gardé,
    et corrigé comme le texte quand la variante y revient (`tarvél-tarvél`).
    """
    terms = _terms_by_variant(entries)
    correct = _text_corrector(terms)
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
                text = f"{correct(lead)}{term}{correct(trail)}"
                result.append(Word(text, span[0].start, span[-1].end, span[0].speaker))
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
