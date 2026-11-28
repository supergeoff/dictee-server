"""Correction du glossaire : les variantes entendues deviennent leur terme."""

import pytest

from dictee import glossary
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


def test_variant_repeated_in_one_word_is_replaced_each_time_like_in_the_text():
    assert correct_text("tarvél-tarvél", [TARVEL]) == "Tarvel-Tarvel"
    assert correct_words([Word("tarvél-tarvél", 0.0, 1.0)], [TARVEL]) == (Word("Tarvel-Tarvel", 0.0, 1.0),)


IA = GlossaryEntry("l'IA", ("l'ia",))
IA_CURLY = GlossaryEntry("l’IA", ("l’ia",))


@pytest.mark.parametrize(("entry", "heard"), [(IA, "l’ia"), (IA_CURLY, "l'ia")])
def test_both_apostrophes_match_a_variant_in_the_text(entry, heard):
    assert correct_text(f"« {heard} » parle", [entry]) == f"« {entry.term} » parle"


@pytest.mark.parametrize(("entry", "heard"), [(IA, "l’ia,"), (IA_CURLY, "l'ia,")])
def test_both_apostrophes_match_a_variant_in_the_words(entry, heard):
    assert correct_words([Word(heard, 0.0, 1.0)], [entry]) == (Word(f"{entry.term},", 0.0, 1.0),)


def test_apostrophes_around_a_variant_are_kept_as_written():
    assert correct_text("d’tarvél et l'tarvél", [TARVEL]) == "d’Tarvel et l'Tarvel"


def test_apostrophes_around_a_variant_are_kept_as_written_in_the_words():
    assert correct_words([Word("d’tarvél", 0.0, 1.0)], [TARVEL]) == (Word("d’Tarvel", 0.0, 1.0),)


@pytest.mark.parametrize(
    ("entries", "heard", "expected"),
    [
        ([GlossaryEntry("IA", ("ia",)), GlossaryEntry("L-IA", ("l'ia",))], "l'ia", "L-IA"),
        ([TARVEL, GlossaryEntry("ST", ("sous-tarvél",))], "sous-tarvél", "ST"),
    ],
)
def test_longest_variant_wins_in_the_words_as_in_the_text(entries, heard, expected):
    final = correct_final(Final(heard, (Word(heard, 0.0, 1.0),)), entries)
    assert final.text == expected
    assert " ".join(word.text for word in final.words) == final.text


def test_words_without_any_variant_try_no_pattern(monkeypatch):
    # Un mot qui ne contient aucune variante est écarté avant la boucle sur les motifs.
    tries = []
    make_pattern = glossary._word_pattern

    class Counting:
        def __init__(self, pattern):
            self._pattern = pattern

        def fullmatch(self, text):
            tries.append(text)
            return self._pattern.fullmatch(text)

    monkeypatch.setattr(glossary, "_word_pattern", lambda *args, **kwargs: Counting(make_pattern(*args, **kwargs)))
    entries = [GlossaryEntry(f"T{i}", (f"vx{i}q", f"wy{i} zz{i}")) for i in range(100)]
    words = [Word(f"mot{i}", i, i + 0.5) for i in range(1000)] + [Word("vx7q,", 1000.0, 1000.5)]
    result = correct_words(words, entries)
    assert result[-1] == Word("T7,", 1000.0, 1000.5)
    assert set(tries) == {"vx7q,"}
