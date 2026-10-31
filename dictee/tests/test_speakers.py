import json
from pathlib import Path

import pytest

from dictee.speakers import assign_speakers, build_segments
from dictee.types import Segment, Turn, Word

FIXTURES = Path(__file__).parent / "fixtures" / "nemo"


def speakers_of(words, turns):
    return [word.speaker for word in assign_speakers(words, turns)]


# --- Attribution des mots aux tours ------------------------------------------------------------------------------


def test_word_takes_the_speaker_active_at_its_middle():
    words = [Word("Oui", 0.1, 0.5), Word("non", 0.8, 1.4)]
    turns = [Turn(0.0, 1.0, 2), Turn(1.0, 2.0, 1)]
    assert [w.speaker for w in assign_speakers(words, turns)] == ["A", "B"]  # ordre de première apparition


def test_word_outside_any_turn_takes_the_nearest():
    turns = [Turn(0.0, 1.0, 1), Turn(3.0, 4.0, 2)]
    assert assign_speakers([Word("hm", 1.8, 2.0)], turns)[0].speaker == "A"


def test_without_turns_everyone_is_a():
    assert {w.speaker for w in assign_speakers([Word("a", 0, 1), Word("b", 1, 2)], [])} == {"A"}


def test_equidistant_word_takes_the_earlier_turn():
    # Le milieu du mot (2,0 s) est à 1 s des deux tours ; la liste est à l'envers pour que son ordre ne compte pas.
    turns = [Turn(3.0, 4.0, 2), Turn(0.0, 1.0, 1)]
    words = [Word("hm", 1.75, 2.25), Word("ok", 3.25, 3.75)]
    assert speakers_of(words, turns) == ["A", "B"]


def test_overlapping_turns_give_the_word_to_the_latest_start():
    turns = [Turn(0.0, 3.0, 1), Turn(1.0, 3.0, 2)]
    words = [Word("a", 0.25, 0.75), Word("b", 1.5, 2.5)]  # milieux à 0,5 s (un seul tour) puis 2,0 s (les deux)
    assert speakers_of(words, turns) == ["A", "B"]


def test_overlapping_turns_with_the_same_start_give_the_word_to_the_lower_id():
    turns = [Turn(0.0, 2.0, 2), Turn(0.0, 2.0, 1), Turn(3.0, 4.0, 2)]
    words = [Word("a", 0.5, 1.5), Word("b", 3.25, 3.75)]
    # « a » revient au locuteur 1 et « b » au locuteur 2 : deux étiquettes (une seule si « a » allait au locuteur 2).
    assert speakers_of(words, turns) == ["A", "B"]


def test_turn_bounds_are_inclusive():
    turns = [Turn(0.0, 1.0, 1), Turn(1.0, 2.0, 2)]
    words = [Word("x", 0.0, 0.5), Word("y", 0.5, 1.5)]  # le milieu de « y » (1,0 s) borde le tour 1 et le tour 2
    assert speakers_of(words, turns) == ["A", "B"]


def test_labels_follow_the_first_appearance_of_speakers_among_the_words():
    turns = [Turn(0.0, 1.0, 3), Turn(1.0, 2.0, 1), Turn(2.0, 3.0, 2)]
    words = [Word("a", 0.0, 1.0), Word("b", 1.0, 2.0), Word("c", 2.0, 3.0), Word("d", 0.0, 1.0)]
    assert speakers_of(words, turns) == ["A", "B", "C", "A"]


def test_labels_go_on_after_z_with_s27_and_s28():
    words = [Word(f"w{i}", i, i + 1) for i in range(28)]
    turns = [Turn(i, i + 1, i + 1) for i in range(28)]
    labels = speakers_of(words, turns)
    assert labels[:3] == ["A", "B", "C"]
    assert labels[24:] == ["Y", "Z", "S27", "S28"]


def test_assign_speakers_returns_new_words_and_leaves_its_input_untouched():
    words = [Word("a", 0.0, 1.0, "Z"), Word("b", 1.0, 2.0, "Z")]
    assigned = assign_speakers(words, [Turn(0.0, 2.0, 1)])
    assert [w.speaker for w in assigned] == ["A", "A"]
    assert [(w.text, w.start, w.end) for w in assigned] == [("a", 0.0, 1.0), ("b", 1.0, 2.0)]
    assert [w.speaker for w in words] == ["Z", "Z"]
    assert all(new is not old for new, old in zip(assigned, words, strict=True))


def test_assign_speakers_keeps_the_order_of_the_words():
    turns = [Turn(0.0, 1.0, 1), Turn(1.0, 2.0, 2)]
    words = (Word("un", 0.0, 1.0), Word("deux", 1.0, 2.0), Word("trois", 0.0, 1.0))
    assert [w.text for w in assign_speakers(words, turns)] == ["un", "deux", "trois"]


@pytest.mark.parametrize("turns", [[], [Turn(0.0, 1.0, 1)]])
def test_no_words_gives_an_empty_list(turns):
    assert assign_speakers([], turns) == []


def test_recorded_turns_attribute_words_and_group_them_into_segments():
    recorded = json.loads((FIXTURES / "diarization.json").read_text())["segments"]
    turns = [Turn(start=s["start"], end=s["end"], speaker=s["speaker"]) for s in recorded]
    words = (
        Word("Bonjour", 0.5, 1.0),
        Word("hm", 4.8, 5.2),  # dans le silence entre les deux tours, plus près du premier
        Word("alors", 5.4, 5.6),  # dans le même silence, plus près du second
        Word("merci", 6.0, 6.5),
    )
    assigned = assign_speakers(words, turns)
    assert [w.speaker for w in assigned] == ["A", "A", "B", "B"]
    assert build_segments(assigned) == [
        Segment("seg_0", 0.5, 5.2, "A", "Bonjour hm"),
        Segment("seg_1", 5.4, 6.5, "B", "alors merci"),
    ]


# --- Segments ----------------------------------------------------------------------------------------------------


def test_segments_merge_consecutive_words_of_a_speaker():
    words = [
        Word("Bonjour", 0.0, 0.4, "A"),
        Word("à", 0.5, 0.6, "A"),
        Word("tous.", 0.6, 1.2, "A"),
        Word("Salut.", 2.0, 2.5, "B"),
    ]
    assert build_segments(words) == [
        Segment("seg_0", 0.0, 1.2, "A", "Bonjour à tous."),
        Segment("seg_1", 2.0, 2.5, "B", "Salut."),
    ]


def test_no_words_no_segments():
    assert build_segments([]) == []


def test_a_speaker_who_comes_back_opens_a_new_segment():
    words = [Word("un", 0.0, 1.0, "A"), Word("deux", 1.0, 2.0, "B"), Word("trois", 2.0, 3.0, "A")]
    assert build_segments(words) == [
        Segment("seg_0", 0.0, 1.0, "A", "un"),
        Segment("seg_1", 1.0, 2.0, "B", "deux"),
        Segment("seg_2", 2.0, 3.0, "A", "trois"),
    ]


def test_a_single_word_makes_a_segment():
    assert build_segments((Word("seul", 1.5, 2.0, "A"),)) == [Segment("seg_0", 1.5, 2.0, "A", "seul")]


def test_segments_need_words_that_have_a_speaker():
    with pytest.raises(ValueError, match="speaker"):
        build_segments([Word("a", 0.0, 1.0, "A"), Word("b", 1.0, 2.0)])
