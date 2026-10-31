"""Attribution des mots aux locuteurs de la séparation NeMo, puis regroupement des mots en segments."""

from collections.abc import Sequence
from dataclasses import replace
from itertools import groupby
from operator import attrgetter
from string import ascii_uppercase

from dictee.types import Segment, Turn, Word


def _label(index: int) -> str:
    """Étiquette du locuteur de rang `index` : A à Z, puis S27, S28... (NeMo ne sépare pas plus de 8 voix)."""
    return ascii_uppercase[index] if index < len(ascii_uppercase) else f"S{index + 1}"


def _distance(turn: Turn, instant: float) -> float:
    """Écart en secondes entre `instant` et le bord le plus proche du tour, nul si le tour le contient."""
    return max(turn.start - instant, instant - turn.end, 0.0)


def _turn_at(turns: Sequence[Turn], instant: float) -> Turn | None:
    """Tour qui parle à `instant`, à défaut le plus proche dans le temps, ou None s'il n'y a aucun tour.

    Si plusieurs tours contiennent l'instant (voix qui se chevauchent), le dernier à avoir commencé l'emporte, puis
    le plus petit identifiant NeMo. Si aucun ne le contient, le plus proche l'emporte, puis le plus ancien.
    """
    containing = [turn for turn in turns if turn.start <= instant <= turn.end]
    if containing:
        return max(containing, key=lambda turn: (turn.start, -turn.speaker))
    return min(turns, key=lambda turn: (_distance(turn, instant), turn.start, turn.speaker), default=None)


def assign_speakers(words: Sequence[Word], turns: Sequence[Turn]) -> list[Word]:
    """Renvoie les mots avec leur locuteur, sans toucher à ceux reçus.

    Le locuteur d'un mot est celui du tour qui contient le milieu du mot, sinon celui du tour le plus proche. Les
    identifiants de NeMo deviennent A, B, C dans l'ordre de leur première apparition parmi les mots. Sans aucun
    tour, tous les mots sont de A.
    """
    labels: dict[int | None, str] = {}  # Identifiant NeMo (None sans tour) vers étiquette.
    assigned: list[Word] = []
    for word in words:
        turn = _turn_at(turns, (word.start + word.end) / 2)
        nemo_id = turn.speaker if turn else None
        if nemo_id not in labels:
            labels[nemo_id] = _label(len(labels))
        assigned.append(replace(word, speaker=labels[nemo_id]))
    return assigned


def build_segments(words: Sequence[Word]) -> list[Segment]:
    """Regroupe les mots consécutifs d'un même locuteur en segments seg_0, seg_1...

    Un segment va du début de son premier mot à la fin de son dernier ; son texte joint les mots par une espace.
    Chaque mot doit déjà porter son locuteur (voir `assign_speakers`).
    """
    segments: list[Segment] = []
    for speaker, group in groupby(words, key=attrgetter("speaker")):
        if speaker is None:
            raise ValueError("build_segments needs words with a speaker: call assign_speakers first")
        run = list(group)
        segments.append(
            Segment(f"seg_{len(segments)}", run[0].start, run[-1].end, speaker, " ".join(word.text for word in run))
        )
    return segments
