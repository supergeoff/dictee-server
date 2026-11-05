"""Étiquettes des personnes que la session temps réel de NeMo marque sur les mots, puis regroupement en segments."""

from collections.abc import Sequence
from dataclasses import replace
from itertools import groupby
from operator import attrgetter
from string import ascii_uppercase

from dictee.types import Segment, Word


def _label(index: int) -> str:
    """Étiquette de la personne de rang `index` : A à Z, puis S27, S28... (NeMo ne sépare pas plus de 8 voix)."""
    return ascii_uppercase[index] if index < len(ascii_uppercase) else f"S{index + 1}"


def label_speakers(words: Sequence[Word]) -> list[Word]:
    """Renvoie les mots avec leur personne en A, B, C..., sans toucher à ceux reçus.

    Les personnes brutes de NeMo deviennent A, B, C dans l'ordre de leur première apparition parmi les mots. Un mot
    sans personne prend celle du mot précédent ; les premiers mots, sans personne avant eux, prennent la première qui
    apparaît ensuite. Quand aucun mot n'a de personne, tous sont de A.
    """
    first = next((word.speaker for word in words if word.speaker is not None), None)
    labels: dict[str | None, str] = {}  # Personne brute (None sans aucune personne) vers étiquette.
    current = first
    labelled: list[Word] = []
    for word in words:
        if word.speaker is not None:
            current = word.speaker
        if current not in labels:
            labels[current] = _label(len(labels))
        labelled.append(replace(word, speaker=labels[current]))
    return labelled


def build_segments(words: Sequence[Word]) -> list[Segment]:
    """Regroupe les mots consécutifs d'une même personne en segments seg_0, seg_1...

    Un segment va du début de son premier mot à la fin de son dernier ; son texte joint les mots par une espace.
    Chaque mot doit déjà porter sa personne (voir `label_speakers`).
    """
    segments: list[Segment] = []
    for speaker, group in groupby(words, key=attrgetter("speaker")):
        if speaker is None:
            raise ValueError("build_segments needs words with a speaker: call label_speakers first")
        run = list(group)
        segments.append(
            Segment(f"seg_{len(segments)}", run[0].start, run[-1].end, speaker, " ".join(word.text for word in run))
        )
    return segments
