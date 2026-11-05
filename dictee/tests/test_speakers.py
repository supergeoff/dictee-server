import json
from pathlib import Path

import pytest

from dictee.nemo import SPEAKER_KEY
from dictee.speakers import build_segments, label_speakers
from dictee.types import Segment, Word

FIXTURES = Path(__file__).parent / "fixtures" / "nemo"


def words_of(*speakers):
    return [Word(f"w{i}", float(i), i + 0.5, speaker) for i, speaker in enumerate(speakers)]


def labels(words):
    return [word.speaker for word in label_speakers(words)]


# --- Étiquettes des personnes ------------------------------------------------------------------------------------


def test_speakers_are_renamed_in_order_of_first_appearance():
    assert labels(words_of("3", "1", "3", "2")) == ["A", "B", "A", "C"]


def test_a_word_without_speaker_takes_the_speaker_of_the_word_before():
    assert labels(words_of("1", None, "2", None)) == ["A", "A", "B", "B"]


def test_leading_words_without_speaker_take_the_first_speaker():
    assert labels(words_of(None, None, "5")) == ["A", "A", "A"]


def test_words_without_any_speaker_all_go_to_a():
    assert labels(words_of(None, None)) == ["A", "A"]


def test_more_than_26_speakers_go_on_with_s27():
    assert labels(words_of(*map(str, range(27))))[-1] == "S27"


def test_labels_go_on_after_z_with_s27_and_s28():
    result = labels(words_of(*map(str, range(28))))
    assert result[:3] == ["A", "B", "C"]
    assert result[24:] == ["Y", "Z", "S27", "S28"]


def test_label_speakers_returns_new_words_and_leaves_its_input_untouched():
    words = [Word("a", 0.0, 1.0, "7"), Word("b", 1.0, 2.0, None)]
    labelled = label_speakers(words)
    assert [w.speaker for w in labelled] == ["A", "A"]
    assert [(w.text, w.start, w.end) for w in labelled] == [("a", 0.0, 1.0), ("b", 1.0, 2.0)]
    assert [w.speaker for w in words] == ["7", None]
    assert all(new is not old for new, old in zip(labelled, words, strict=True))


def test_label_speakers_keeps_the_order_of_the_words():
    words = (Word("un", 0.0, 1.0, "2"), Word("deux", 1.0, 2.0, "1"), Word("trois", 0.0, 1.0, "2"))
    assert [w.text for w in label_speakers(words)] == ["un", "deux", "trois"]


def test_no_words_gives_an_empty_list():
    assert label_speakers([]) == []


def test_recorded_session_words_get_their_labels_and_group_into_segments():
    events = [json.loads(line) for line in (FIXTURES / "realtime_speakers_events.jsonl").read_text().splitlines()]
    completed = next(event for event in events if event["type"].endswith(".completed"))
    # La session numérote les personnes à partir de 1.
    words = [Word(w["word"], float(w["start"]), float(w["end"]), str(w[SPEAKER_KEY])) for w in completed["words"]]
    assert {word.speaker for word in words} == {"1", "2"}
    assert build_segments(label_speakers(words)) == [
        Segment("seg_0", 0.4, 5.12, "A", "Bonjour, je teste la transcription d'une motron et de sort former ce matin."),
        Segment("seg_1", 6.8, 10.0, "B", "Très bien, parlons maintenant de la synthèse vocale en français"),
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
