import pytest

from dictee.errors import ApiError
from dictee.modes import MODELS, RESPONSE_FORMATS, Mode, resolve_mode, to_iso_language, to_nemo_language


@pytest.mark.parametrize(
    "model,fmt,mode",
    [
        ("dictee", "json", Mode.RAW),
        ("dictee-clean", "json", Mode.CLEAN),
        ("dictee-clean", "text", Mode.CLEAN),
        ("dictee-clean", "srt", Mode.RAW),
        ("dictee-clean", "vtt", Mode.RAW),
        ("dictee-clean", "verbose_json", Mode.RAW),
        ("dictee-diarize", "vtt", Mode.DIARIZE),
        ("dictee-diarize", "json", Mode.DIARIZE),
        ("gpt-4o-transcribe-diarize", "json", Mode.DIARIZE),
        ("whisper-1", "json", Mode.RAW),
        ("gpt-4o-transcribe", "text", Mode.RAW),
        ("whisper-1", "diarized_json", Mode.DIARIZE),
        ("dictee", "diarized_json", Mode.DIARIZE),
        ("dictee-clean", "diarized_json", Mode.DIARIZE),
    ],
)
def test_resolve_mode(model, fmt, mode):
    assert resolve_mode(model, fmt) is mode


def test_unknown_format_is_refused():
    with pytest.raises(ApiError) as e:
        resolve_mode("dictee", "xml")
    assert e.value.status == 400 and e.value.param == "response_format"


@pytest.mark.parametrize("fmt", RESPONSE_FORMATS)
def test_every_known_format_is_accepted(fmt):
    assert isinstance(resolve_mode("dictee", fmt), Mode)


def test_format_names_are_case_sensitive():
    with pytest.raises(ApiError) as e:
        resolve_mode("dictee", "JSON")
    assert e.value.status == 400 and e.value.param == "response_format"


def test_models_and_formats_are_the_documented_ones():
    assert MODELS == ("dictee", "dictee-clean", "dictee-diarize")
    assert RESPONSE_FORMATS == ("json", "text", "srt", "vtt", "verbose_json", "diarized_json")


@pytest.mark.parametrize(
    "code,expected",
    [
        (None, "fr-FR"),
        ("", "fr-FR"),
        ("fr", "fr-FR"),
        ("FR", "fr-FR"),
        ("fr-fr", "fr-FR"),
        ("FR-fr", "fr-FR"),
        ("fr-FR", "fr-FR"),
        ("en", "en-US"),
        ("EN", "en-US"),
        ("de", "de-DE"),
        ("en-gb", "en-GB"),
    ],
)
def test_to_nemo_language(code, expected):
    assert to_nemo_language(code) == expected


@pytest.mark.parametrize("code", ["french", "f", "fra", "fr_FR", "fr-FRA", "fr-", "-fr", "1f", " fr", "é", "fé"])
def test_to_nemo_language_refuses_what_is_not_a_language_code(code):
    with pytest.raises(ApiError) as e:
        to_nemo_language(code)
    assert e.value.status == 400 and e.value.param == "language"


@pytest.mark.parametrize(
    "code,expected",
    [
        (None, "fr"),
        ("", "fr"),
        ("fr", "fr"),
        ("FR", "fr"),
        ("fr-FR", "fr"),
        ("FR-fr", "fr"),
        ("en-US", "en"),
        ("de", "de"),
    ],
)
def test_to_iso_language(code, expected):
    assert to_iso_language(code) == expected


@pytest.mark.parametrize("code", ["french", "fr_FR", "fra", "fr-"])
def test_to_iso_language_refuses_what_is_not_a_language_code(code):
    with pytest.raises(ApiError) as e:
        to_iso_language(code)
    assert e.value.status == 400 and e.value.param == "language"
