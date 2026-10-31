import dataclasses
import time

import pytest

from dictee.auth import Caller
from dictee.profile import (
    DEFAULT_INSTRUCTIONS,
    GlossaryEntry,
    Profile,
    glossary_prompt,
    glossary_terms,
    load_profile,
    parse_glossary,
)


def test_parses_french_hints():
    assert parse_glossary("Nemotron (souvent entendu : némo trône, nemo tron)") == [
        GlossaryEntry("Nemotron", ("némo trône", "nemo tron"))
    ]


def test_parses_the_old_english_hints():
    assert parse_glossary("Sortformer (often heard as: sort former)") == [GlossaryEntry("Sortformer", ("sort former",))]


def test_hint_marker_ignores_case_and_spacing():
    assert parse_glossary("Magpie (Souvent entendu: mag pie)") == [GlossaryEntry("Magpie", ("mag pie",))]


def test_ignores_comments_and_blank_lines():
    assert parse_glossary("# note\n\nMagpie\n") == [GlossaryEntry("Magpie")]


def test_trims_lines_and_accepts_windows_line_endings():
    assert parse_glossary("  # note\r\n  Starlette  \r\n\r\nMagpie (often heard as: mag pie)\r\n") == [
        GlossaryEntry("Starlette"),
        GlossaryEntry("Magpie", ("mag pie",)),
    ]


def test_other_parentheses_stay_in_the_term():
    assert parse_glossary("Starlette (framework)") == [GlossaryEntry("Starlette (framework)")]


def test_empty_text_has_no_entries():
    assert parse_glossary("") == []
    assert parse_glossary(" \n\t\n") == []


SPACES = " " * 100_000


# Le glossaire d'un compte est lu à chaque requête, sur la boucle d'événements partagée : une ligne mal formée
# faite d'une longue suite d'espaces ne doit pas la bloquer.
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            f"a (souvent entendu :{SPACES}x", GlossaryEntry(f"a (souvent entendu :{SPACES}x"), id="unclosed-parenthesis"
        ),
        pytest.param(
            f"a (souvent entendu{SPACES}x)", GlossaryEntry(f"a (souvent entendu{SPACES}x)"), id="missing-colon"
        ),
        pytest.param(f"a ({SPACES}x)", GlossaryEntry(f"a ({SPACES}x)"), id="no-marker"),
        pytest.param("a" + "(" * 100_000 + ")", GlossaryEntry("a" + "(" * 100_000 + ")"), id="many-parentheses"),
        pytest.param(f"a (souvent entendu :{SPACES}x)", GlossaryEntry("a", ("x",)), id="well-formed"),
    ],
)
def test_long_runs_of_spaces_parse_quickly(line, expected):
    start = time.perf_counter()
    entries = parse_glossary(line)
    elapsed = time.perf_counter() - start
    assert elapsed < 1, f"parsing took {elapsed:.1f} s"
    assert entries == [expected]


def test_prompt_lists_terms_with_their_hints():
    entries = parse_glossary("Nemotron (souvent entendu : nemo tron)\nMagpie")
    assert glossary_terms(entries) == ["Nemotron", "Magpie"]
    assert glossary_prompt(entries) == "Glossary:\n- Nemotron (often heard as: nemo tron)\n- Magpie"
    assert glossary_prompt([]) == ""


def test_prompt_joins_several_hints_with_commas():
    entries = parse_glossary("Nemotron (souvent entendu : némo trône, nemo tron)")
    assert glossary_prompt(entries) == "Glossary:\n- Nemotron (often heard as: némo trône, nemo tron)"


def test_default_instructions_come_from_the_old_proxy():
    assert DEFAULT_INSTRUCTIONS.startswith("You clean up raw speech-to-text transcripts.")
    assert DEFAULT_INSTRUCTIONS.endswith("Return only the cleaned transcript.")


def test_default_instructions_are_one_trimmed_line():
    assert DEFAULT_INSTRUCTIONS == DEFAULT_INSTRUCTIONS.strip()
    assert "\n" not in DEFAULT_INSTRUCTIONS


def test_unidentified_caller_gets_the_defaults(db, settings):
    assert load_profile(db, settings, Caller(None)) == Profile((), DEFAULT_INSTRUCTIONS, settings.default_voice)


def test_empty_stored_values_fall_back_to_the_defaults(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    assert load_profile(db, settings, Caller(uid)) == Profile((), DEFAULT_INSTRUCTIONS, settings.default_voice)


def test_whitespace_only_values_count_as_empty(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.save_settings(uid, glossary=" \n\t\n", instructions="  \n", voice=" ")
    assert load_profile(db, settings, Caller(uid)) == Profile((), DEFAULT_INSTRUCTIONS, settings.default_voice)


def test_stored_values_win(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.save_settings(
        uid,
        glossary="Starlette (souvent entendu : star lette)\nMagpie",
        instructions="Keep it short.",
        voice="Marco",
    )
    assert load_profile(db, settings, Caller(uid)) == Profile(
        (GlossaryEntry("Starlette", ("star lette",)), GlossaryEntry("Magpie")),
        "Keep it short.",
        "Marco",
    )


def test_each_value_falls_back_on_its_own(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.save_settings(uid, voice="Marco")
    assert load_profile(db, settings, Caller(uid)) == Profile((), DEFAULT_INSTRUCTIONS, "Marco")


def test_stored_values_are_trimmed(db, settings):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    db.save_settings(uid, instructions="  Keep it short.\n", voice=" Marco ")
    profile = load_profile(db, settings, Caller(uid))
    assert (profile.instructions, profile.voice) == ("Keep it short.", "Marco")


def test_one_account_does_not_see_the_settings_of_another(db, settings):
    first = db.upsert_user("sub-1", "a@example.org", "A")
    second = db.upsert_user("sub-2", "b@example.org", "B")
    db.save_settings(first, glossary="Sortformer", voice="Marco")
    assert load_profile(db, settings, Caller(second)) == Profile((), DEFAULT_INSTRUCTIONS, settings.default_voice)


def test_profile_is_immutable(db, settings):
    profile = load_profile(db, settings, Caller(None))
    assert isinstance(profile.glossary, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.voice = "Marco"  # type: ignore[misc]
