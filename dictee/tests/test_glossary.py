"""Correction du glossaire : les variantes entendues deviennent leur terme."""

import pytest

from dictee.glossary import correct_final, correct_text, correct_words
from dictee.profile import GlossaryEntry
from dictee.types import Final, Word

TARVEL = GlossaryEntry("Tarvel", ("tarvél",))


def test_variant_is_replaced_by_term():
    assert correct_text("le projet tarvél avance", [TARVEL]) == "le projet Tarvel avance"


def test_match_ignores_case():
    assert correct_text("TARVÉL", [TARVEL]) == "Tarvel"
    assert correct_text("Tarvél", [TARVEL]) == "Tarvel"


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
    assert correct_text("tarvél,", [TARVEL]) == "Tarvel,"
    assert correct_words([Word("tarvél,", 0.0, 1.0)], [TARVEL]) == (Word("Tarvel,", 0.0, 1.0),)


def test_entry_without_variant_changes_nothing():
    entry = GlossaryEntry("Tarvel")
    assert correct_text("le projet tarvél", [entry]) == "le projet tarvél"
    words = [Word("tarvél", 0.0, 1.0)]
    assert correct_words(words, [entry]) == tuple(words)


def test_final_without_words_is_corrected():
    assert correct_final(Final("bonjour tarvél", ()), [TARVEL]) == Final("bonjour Tarvel", ())


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_variant_after_an_elision_is_replaced_in_the_word(apostrophe):
    words = [Word(f"d{apostrophe}tarvél", 0.0, 1.0)]
    assert correct_words(words, [TARVEL]) == (Word(f"d{apostrophe}Tarvel", 0.0, 1.0),)


def test_elision_and_punctuation_around_the_variant_are_kept():
    assert correct_words([Word("l'tarvél,", 0.0, 1.0)], [TARVEL]) == (Word("l'Tarvel,", 0.0, 1.0),)


def test_prefix_and_hyphen_before_the_variant_are_kept():
    assert correct_words([Word("sous-tarvél", 0.0, 1.0)], [TARVEL]) == (Word("sous-Tarvel", 0.0, 1.0),)


def test_variant_glued_to_other_letters_is_kept_in_the_word():
    words = [Word("tarvélment", 0.0, 1.0)]
    assert correct_words(words, [TARVEL]) == tuple(words)


def test_multi_word_variant_keeps_what_surrounds_it_in_its_first_and_last_words():
    words = [Word("d'cloude", 0.0, 0.5, "A"), Word("code-là.", 0.6, 1.0, "A")]
    entry = GlossaryEntry("Claude Code", ("cloude code",))
    assert correct_words(words, [entry]) == (Word("d'Claude Code-là.", 0.0, 1.0, "A"),)


def test_final_with_an_elision_gives_a_text_and_words_that_agree():
    words = (Word("Le", 0.0, 0.2), Word("modèle", 0.3, 0.6), Word("d'tarvél", 0.7, 0.9), Word("est", 1.0, 1.1))
    final = correct_final(Final("Le modèle d'tarvél est bon.", words), [TARVEL])
    assert final.text == "Le modèle d'Tarvel est bon."
    assert " ".join(word.text for word in final.words) in final.text
