import json

import pytest

from dictee.errors import ApiError
from dictee.formats import Cue, build_cues, delta_event, done_event, error_event, render, segment_event
from dictee.types import Segment, Transcript, Word

NO_GRANULARITY = frozenset()

WORDS = (
    Word("Bonjour", 0.0, 0.4, "A"),
    Word("à", 0.5, 0.6, "A"),
    Word("tous.", 0.6, 1.2, "A"),
    Word("Salut.", 2.0, 2.5, "B"),
)
SEGMENTS = (Segment("seg_0", 0.0, 1.2, "A", "Bonjour à tous."), Segment("seg_1", 2.0, 2.5, "B", "Salut."))
DIARIZED = Transcript("Bonjour à tous. Salut.", WORDS, 2.5, "fr", SEGMENTS)
PLAIN_WORDS = tuple(Word(w.text, w.start, w.end) for w in WORDS)
PLAIN = Transcript("Bonjour à tous. Salut.", PLAIN_WORDS, 2.5, "fr")
EMPTY = Transcript("", (), 0.0, "fr")
EMPTY_DIARIZED = Transcript("", (), 0.0, "fr", ())


def timed(texts, step=1.0):
    """Mots d'une durée de `step` secondes, mis bout à bout à partir de zéro."""
    return [Word(text, i * step, (i + 1) * step) for i, text in enumerate(texts)]


def body_of(transcript, fmt, granularities=NO_GRANULARITY):
    return render(transcript, fmt, granularities=granularities).body.decode()


# --- Sous-titres -------------------------------------------------------------------------------------------------


def test_cue_closes_after_a_sentence_end():
    cues = build_cues(timed(["Un", "deux.", "Trois?", "Quatre!", "Cinq…", "Six"]), split_on_speaker=False)
    assert [c.text for c in cues] == ["Un deux.", "Trois?", "Quatre!", "Cinq…", "Six"]


def test_cue_does_not_close_on_a_comma():
    cues = build_cues(timed(["Un,", "deux;", "trois:"]), split_on_speaker=False)
    assert [c.text for c in cues] == ["Un, deux; trois:"]


def test_cue_carries_the_timing_of_its_first_and_last_word():
    cues = build_cues(WORDS[:3], split_on_speaker=False)
    assert cues == [Cue(0.0, 1.2, "Bonjour à tous.", None)]


def test_cue_closes_after_seven_seconds():
    cues = build_cues(timed(["mot"] * 10), split_on_speaker=False)
    assert [(c.start, c.end, c.text) for c in cues] == [
        (0.0, 7.0, " ".join(["mot"] * 7)),
        (7.0, 10.0, " ".join(["mot"] * 3)),
    ]


def test_cue_may_last_exactly_seven_seconds():
    cues = build_cues(timed(["mot"] * 7), split_on_speaker=False)
    assert len(cues) == 1 and cues[0].end - cues[0].start == 7.0


def test_cue_closes_beyond_84_characters():
    cues = build_cues(timed(["abcdefghij"] * 8, step=0.1), split_on_speaker=False)
    assert [len(c.text) for c in cues] == [76, 10]


def test_cue_may_hold_exactly_84_characters():
    cues = build_cues(timed(["a" * 40, "b" * 43, "c"], step=0.1), split_on_speaker=False)
    assert [c.text for c in cues] == ["a" * 40 + " " + "b" * 43, "c"]
    assert len(cues[0].text) == 84


def test_a_word_longer_than_the_limits_stays_alone_in_its_cue():
    cues = build_cues(timed(["x" * 100, "fin"], step=10.0), split_on_speaker=False)
    assert [c.text for c in cues] == ["x" * 100, "fin"]


def test_cue_closes_when_the_speaker_changes():
    words = [Word("Oui", 0.0, 0.5, "A"), Word("non", 0.5, 1.0, "B"), Word("peut-être", 1.0, 1.5, "B")]
    cues = build_cues(words, split_on_speaker=True)
    assert cues == [Cue(0.0, 0.5, "Oui", "A"), Cue(0.5, 1.5, "non peut-être", "B")]


def test_cue_ignores_the_speaker_when_not_asked_to_split():
    words = [Word("Oui", 0.0, 0.5, "A"), Word("non", 0.5, 1.0, "B")]
    assert build_cues(words, split_on_speaker=False) == [Cue(0.0, 1.0, "Oui non", None)]


def test_no_words_gives_no_cues():
    assert build_cues([], split_on_speaker=True) == []


@pytest.mark.parametrize(
    "seconds,stamp",
    [
        (0.0, "00:00:00,000"),
        (1.2, "00:00:01,200"),
        (1.2346, "00:00:01,235"),
        (61.0, "00:01:01,000"),
        (3661.5, "01:01:01,500"),
    ],
)
def test_srt_timestamps_are_rounded_to_the_millisecond(seconds, stamp):
    transcript = Transcript("mot", (Word("mot", seconds, seconds + 1.0),), seconds + 1.0, "fr")
    assert body_of(transcript, "srt").splitlines()[1].startswith(stamp)


# --- srt et vtt ----------------------------------------------------------------------------------------------------


def test_srt_prefixes_each_cue_with_its_speaker():
    assert render(DIARIZED, "srt", granularities=NO_GRANULARITY).body.decode() == (
        "1\n00:00:00,000 --> 00:00:01,200\nA : Bonjour à tous.\n\n"
        "2\n00:00:02,000 --> 00:00:02,500\nB : Salut.\n"
    )


def test_srt_without_speakers_has_no_prefix():
    assert body_of(PLAIN, "srt") == (
        "1\n00:00:00,000 --> 00:00:01,200\nBonjour à tous.\n\n2\n00:00:02,000 --> 00:00:02,500\nSalut.\n"
    )


def test_srt_ignores_word_speakers_when_the_transcript_has_no_segments():
    assert body_of(Transcript("Bonjour à tous. Salut.", WORDS, 2.5, "fr"), "srt") == body_of(PLAIN, "srt")


def test_srt_has_no_prefix_when_the_words_carry_no_speaker():
    no_speaker = Transcript("Bonjour à tous. Salut.", PLAIN_WORDS, 2.5, "fr", SEGMENTS)
    assert body_of(no_speaker, "srt") == body_of(PLAIN, "srt")


def test_vtt_uses_voice_tags():
    assert render(DIARIZED, "vtt", granularities=NO_GRANULARITY).body.decode() == (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.200\n<v A>Bonjour à tous.\n\n"
        "00:00:02.000 --> 00:00:02.500\n<v B>Salut.\n"
    )


def test_vtt_without_speakers_has_no_voice_tag():
    assert body_of(PLAIN, "vtt") == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.200\nBonjour à tous.\n\n00:00:02.000 --> 00:00:02.500\nSalut.\n"
    )


def test_vtt_escapes_the_characters_that_would_open_a_tag():
    transcript = Transcript("a<b & c", (Word("a<b", 0.0, 0.5), Word("&", 0.5, 0.8), Word("c", 0.8, 1.0)), 1.0, "fr")
    assert body_of(transcript, "vtt").endswith("a&lt;b &amp; c\n")


def test_srt_keeps_the_characters_vtt_escapes():
    transcript = Transcript("a<b & c", (Word("a<b", 0.0, 0.5), Word("&", 0.5, 0.8), Word("c", 0.8, 1.0)), 1.0, "fr")
    assert body_of(transcript, "srt").endswith("a<b & c\n")


def test_vtt_media_type():
    response = render(PLAIN, "vtt", granularities=NO_GRANULARITY)
    assert response.headers["content-type"] == "text/vtt; charset=utf-8"


def test_srt_and_text_media_type():
    for fmt in ("srt", "text"):
        response = render(PLAIN, fmt, granularities=NO_GRANULARITY)
        assert response.headers["content-type"] == "text/plain; charset=utf-8"


# --- json, text ----------------------------------------------------------------------------------------------------


def test_json_renders_the_text_only():
    response = render(PLAIN, "json", granularities=NO_GRANULARITY)
    assert response.headers["content-type"] == "application/json"
    assert json.loads(response.body) == {"text": "Bonjour à tous. Salut."}


def test_json_with_speakers_has_one_paragraph_per_turn():
    assert json.loads(render(DIARIZED, "json", granularities=NO_GRANULARITY).body) == {
        "text": "A : Bonjour à tous.\n\nB : Salut."
    }


def test_json_keeps_accents_unescaped():
    body = render(PLAIN, "json", granularities=NO_GRANULARITY).body
    assert "à".encode() in body and b"\\u" not in body


def test_text_without_speakers_is_the_transcript_text():
    assert body_of(PLAIN, "text") == "Bonjour à tous. Salut."


def test_text_with_speakers_has_one_paragraph_per_turn():
    assert render(DIARIZED, "text", granularities=NO_GRANULARITY).body.decode() == "A : Bonjour à tous.\n\nB : Salut."


# --- verbose_json --------------------------------------------------------------------------------------------------


def test_verbose_json_without_granularity_has_neither_words_nor_segments():
    body = json.loads(render(PLAIN, "verbose_json", granularities=NO_GRANULARITY).body)
    assert body == {"task": "transcribe", "language": "fr", "duration": 2.5, "text": "Bonjour à tous. Salut."}


def test_verbose_json_words_only_when_asked():
    body = json.loads(render(PLAIN, "verbose_json", granularities=frozenset({"word"})).body)
    assert body["words"][0] == {"word": "Bonjour", "start": 0.0, "end": 0.4}
    assert len(body["words"]) == 4
    assert "segments" not in body


def test_verbose_json_segments_only_when_asked():
    body = json.loads(render(PLAIN, "verbose_json", granularities=frozenset({"segment"})).body)
    assert "words" not in body
    assert body["segments"][0] == {
        "id": 0, "seek": 0, "start": 0.0, "end": 1.2, "text": "Bonjour à tous.", "tokens": [],
        "temperature": 0.0, "avg_logprob": 0.0, "compression_ratio": 0.0, "no_speech_prob": 0.0,
    }
    assert [s["id"] for s in body["segments"]] == [0, 1]


def test_verbose_json_with_both_granularities():
    body = json.loads(render(PLAIN, "verbose_json", granularities=frozenset({"word", "segment"})).body)
    assert len(body["words"]) == 4 and len(body["segments"]) == 2


def test_verbose_json_segments_follow_the_speaker_turns_of_a_diarized_transcript():
    body = json.loads(render(DIARIZED, "verbose_json", granularities=frozenset({"segment"})).body)
    assert [s["text"] for s in body["segments"]] == ["Bonjour à tous.", "Salut."]
    assert body["text"] == "Bonjour à tous. Salut."


def test_verbose_json_language_is_the_iso_code():
    body = json.loads(render(Transcript("mot", (), 1.0, "fr-FR"), "verbose_json", granularities=NO_GRANULARITY).body)
    assert body["language"] == "fr"


def test_verbose_json_rounds_the_times_to_the_millisecond():
    transcript = Transcript("mot", (Word("mot", 0.12345, 0.98765),), 1.23456, "fr")
    body = json.loads(render(transcript, "verbose_json", granularities=frozenset({"word", "segment"})).body)
    assert body["duration"] == 1.235
    assert body["words"][0] == {"word": "mot", "start": 0.123, "end": 0.988}
    assert (body["segments"][0]["start"], body["segments"][0]["end"]) == (0.123, 0.988)


def test_verbose_json_is_accepted_by_the_openai_client_models():
    from openai.types.audio import TranscriptionVerbose

    body = json.loads(render(PLAIN, "verbose_json", granularities=frozenset({"word", "segment"})).body)
    parsed = TranscriptionVerbose.model_validate(body)
    assert parsed.text == "Bonjour à tous. Salut." and len(parsed.segments) == 2 and len(parsed.words) == 4


# --- diarized_json -------------------------------------------------------------------------------------------------


def test_diarized_json_segments():
    body = json.loads(render(DIARIZED, "diarized_json", granularities=NO_GRANULARITY).body)
    assert body["segments"][0] == {
        "id": "seg_0", "start": 0.0, "end": 1.2, "speaker": "A", "text": "Bonjour à tous.",
        "type": "transcript.text.segment",
    }
    assert len(body["segments"]) == 2


def test_diarized_json_text_has_no_speaker_labels():
    body = json.loads(render(DIARIZED, "diarized_json", granularities=NO_GRANULARITY).body)
    assert body["task"] == "transcribe"
    assert body["duration"] == 2.5
    assert body["text"] == "Bonjour à tous. Salut."


# --- transcription vide --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("transcript", [EMPTY, EMPTY_DIARIZED])
def test_empty_transcript_renders_an_empty_text_everywhere(transcript):
    assert json.loads(render(transcript, "json", granularities=NO_GRANULARITY).body) == {"text": ""}
    assert render(transcript, "text", granularities=NO_GRANULARITY).body == b""
    assert render(transcript, "srt", granularities=NO_GRANULARITY).body == b""
    assert render(transcript, "vtt", granularities=NO_GRANULARITY).body == b"WEBVTT\n\n"


def test_empty_transcript_in_verbose_json_has_empty_lists_when_asked():
    body = json.loads(render(EMPTY, "verbose_json", granularities=frozenset({"word", "segment"})).body)
    assert body == {"task": "transcribe", "language": "fr", "duration": 0.0, "text": "", "words": [], "segments": []}


def test_empty_transcript_in_diarized_json_has_no_segments():
    body = json.loads(render(EMPTY_DIARIZED, "diarized_json", granularities=NO_GRANULARITY).body)
    assert body == {"task": "transcribe", "duration": 0.0, "text": "", "segments": []}


# --- format inconnu ------------------------------------------------------------------------------------------------


def test_render_refuses_an_unknown_format():
    with pytest.raises(ApiError) as e:
        render(PLAIN, "xml", granularities=NO_GRANULARITY)
    assert e.value.status == 400 and e.value.param == "response_format"


# --- événements SSE ------------------------------------------------------------------------------------------------


def test_delta_event():
    assert delta_event("Bon") == 'data: {"type": "transcript.text.delta", "delta": "Bon"}\n\n'


def test_segment_event():
    event = segment_event(SEGMENTS[0])
    assert event.startswith("data: ") and event.endswith("\n\n")
    assert json.loads(event.removeprefix("data: ")) == {
        "type": "transcript.text.segment", "id": "seg_0", "start": 0.0, "end": 1.2, "speaker": "A",
        "text": "Bonjour à tous.",
    }


def test_done_event():
    assert done_event("Bonjour.") == 'data: {"type": "transcript.text.done", "text": "Bonjour."}\n\n'


def test_events_keep_accents_unescaped_and_escape_line_breaks():
    event = delta_event("à\nbientôt")
    assert "à" in event and "\\u" not in event
    assert event.count("\n") == 2 and event.endswith("\n\n")


def test_error_event_carries_the_body_of_the_api_error():
    failure = ApiError(429, "The speech engine is busy.", type="rate_limit_error", code="overloaded")
    event = error_event(failure)
    assert event.startswith("data: ") and event.endswith("\n\n")
    assert json.loads(event.removeprefix("data: ")) == {
        "type": "error",
        "error": {
            "message": "The speech engine is busy.",
            "type": "rate_limit_error",
            "param": None,
            "code": "overloaded",
        },
    }


def test_error_event_keeps_accents_and_the_param():
    event = error_event(ApiError(400, "Langue refusée.", param="language"))
    assert "Langue refusée." in event
    assert json.loads(event.removeprefix("data: "))["error"]["param"] == "language"
