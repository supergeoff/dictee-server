"""Correction du glossaire : les variantes entendues deviennent leur terme."""

from dictee.glossary import correct_final, correct_text, correct_words
from dictee.profile import GlossaryEntry
from dictee.types import Final, Word

SEPTEO = GlossaryEntry("Septeo", ("septéo",))


def test_variant_is_replaced_by_term():
    assert correct_text("le projet septéo avance", [SEPTEO]) == "le projet Septeo avance"


def test_match_ignores_case():
    assert correct_text("SEPTÉO", [SEPTEO]) == "Septeo"
    assert correct_text("Septéo", [SEPTEO]) == "Septeo"


def test_variant_inside_longer_word_is_kept():
    assert correct_text("itlm", [GlossaryEntry("IT", ("it",))]) == "itlm"


def test_variant_with_regex_characters():
    entry = GlossaryEntry("C++", ("c++ (lang)",))
    assert correct_text("du c++ (lang) et (x) a.b", [entry]) == "du C++ et (x) a.b"


def test_longest_variant_wins():
    entries = [GlossaryEntry("Claude", ("claude",)), GlossaryEntry("Claude Enterprise", ("claude enterprise",))]
    assert correct_text("claude enterprise et claude", entries) == "Claude Enterprise et Claude"


def test_multi_word_variant_merges_words():
    words = [Word("le", 0.0, 0.2, "A"), Word("cloude", 0.3, 0.6, "A"), Word("code", 0.7, 1.0, "B")]
    entry = GlossaryEntry("Claude Code", ("cloude code",))
    assert correct_words(words, [entry]) == (Word("le", 0.0, 0.2, "A"), Word("Claude Code", 0.3, 1.0, "A"))


def test_punctuation_is_kept():
    assert correct_text("septéo,", [SEPTEO]) == "Septeo,"
    assert correct_words([Word("septéo,", 0.0, 1.0)], [SEPTEO]) == (Word("Septeo,", 0.0, 1.0),)


def test_entry_without_variant_changes_nothing():
    entry = GlossaryEntry("Septeo")
    assert correct_text("le projet septéo", [entry]) == "le projet septéo"
    words = [Word("septéo", 0.0, 1.0)]
    assert correct_words(words, [entry]) == tuple(words)


def test_final_without_words_is_corrected():
    assert correct_final(Final("bonjour septéo", ()), [SEPTEO]) == Final("bonjour Septeo", ())
