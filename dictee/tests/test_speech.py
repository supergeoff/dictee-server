"""Route de synthèse vocale : découpage en phrases, choix de la voix, formats, pannes et client qui raccroche."""

import asyncio
import functools
import json
import logging
import os
import re
import signal
import time

import anyio
import pytest
from conftest import wait_until
from fakes import FakeNemo
from starlette.requests import Request

from dictee import speech
from dictee.app import create_app
from dictee.audio import OUTPUT_FORMATS, SPEECH_RATE
from dictee.nemo import TTS_SAMPLE_RATE, NemoError, NemoOverloaded, NemoUnavailable
from dictee.speech import MAX_INPUT_CHARS, MAX_SENTENCE_CHARS, resolve_voice, split_sentences

URL = "/v1/audio/speech"
MIB = 1024 * 1024
MP3_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3")
MAGICS = {
    "mp3": MP3_MAGIC,
    "opus": (b"OggS",),
    "aac": (b"\xff\xf1", b"\xff\xf9"),
    "flac": (b"fLaC",),
    "wav": (b"RIFF",),
    "pcm": (b"",),
}
UNEXPECTED_BODY = {
    "error": {
        "message": "The server failed to process the request.",
        "type": "server_error",
        "param": None,
        "code": None,
    }
}


def silence(seconds: float) -> bytes:
    """PCM16 mono à la cadence de la synthèse de NeMo : du silence, que ffmpeg encode comme n'importe quel son."""
    return bytes(2 * round(TTS_SAMPLE_RATE * seconds))


def squash(text: str) -> str:
    """Le texte sans aucune espace : ce qu'un découpage ne doit ni perdre ni réordonner."""
    return "".join(text.split())


def log_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "dictee"]


# --- Découpage en phrases ---


def test_split_on_final_punctuation_and_newlines():
    assert split_sentences("Bonjour. Ça va ? Oui !\nTrès bien") == ["Bonjour.", "Ça va ?", "Oui !", "Très bien"]


def test_split_keeps_a_sign_with_the_space_before_it():
    assert split_sentences("Ça va\u00a0? Oui\u202f!") == ["Ça va\u00a0?", "Oui\u202f!"]


def test_split_cuts_after_an_ellipsis_and_after_repeated_signs():
    assert split_sentences("Alors… Vraiment ?! Oui...") == ["Alors…", "Vraiment ?!", "Oui..."]


def test_split_does_not_cut_inside_a_number_or_a_domain_name():
    text = "Il vaut 3.14 euros. Voir example.org maintenant."
    assert split_sentences(text) == ["Il vaut 3.14 euros.", "Voir example.org maintenant."]


def test_split_keeps_a_closing_quote_with_its_sentence():
    text = 'Elle dit « Salut. » Puis elle part. Il répond "Non." Fin'
    assert split_sentences(text) == ['Elle dit « Salut. »', "Puis elle part.", 'Il répond "Non."', "Fin"]
    assert split_sentences("Elle dit «\u00a0Salut.\u00a0» Puis") == ["Elle dit «\u00a0Salut.\u00a0»", "Puis"]


def test_split_drops_blank_pieces():
    assert split_sentences("  \n\n Un.   \r\n  \n Deux  ") == ["Un.", "Deux"]
    assert split_sentences("") == [] and split_sentences(" \n\t ") == []


def test_long_sentence_is_cut_on_commas():
    sentence = ", ".join(f"la clause numéro {number} continue" for number in range(40)) + "."
    parts = split_sentences(sentence)
    assert len(sentence) > MAX_SENTENCE_CHARS
    assert len(parts) > 1 and all(len(part) <= MAX_SENTENCE_CHARS for part in parts)
    assert all(part.endswith(",") for part in parts[:-1])  # chaque coupure tombe après une virgule
    assert squash("".join(parts)) == squash(sentence)


def test_long_sentence_without_comma_is_cut_on_spaces():
    sentence = " ".join(f"mot{number}" for number in range(200)) + "."
    parts = split_sentences(sentence)
    assert len(parts) > 1 and all(len(part) <= MAX_SENTENCE_CHARS for part in parts)
    assert " ".join(parts).split() == sentence.split()  # aucun mot coupé, aucun mot perdu


def test_clause_over_the_limit_is_cut_on_spaces_between_short_clauses():
    long_clause = " ".join(["mot"] * 100)
    sentence = f"Court, {long_clause}, fin."
    parts = split_sentences(sentence)
    assert parts[0] == "Court," and parts[-1] == "fin."
    assert all(len(part) <= MAX_SENTENCE_CHARS for part in parts)
    assert squash("".join(parts)) == squash(sentence)


def test_word_over_the_limit_is_cut_at_the_character():
    assert split_sentences("a" * 700) == ["a" * 300, "a" * 300, "a" * 100]


@pytest.mark.parametrize("max_chars", [1, 5, 17, MAX_SENTENCE_CHARS])
@pytest.mark.parametrize(
    "text",
    [
        "Un deux, trois quatre cinq, six sept huit neuf. Dix !\nOnze…",
        "Ça va\u00a0? " * 12,
        "abc" * 40 + ", " + "de fg " * 30 + ".",
        "Élève, étudiante: ça marche. Voilà 3.14 et example.org. « Oui. » Non !",
        "x" * 50,
        "Oui ! ... Non. ?\n... Fin ,; ",
        "?! Bonjour, ... , ; ça va ? -- . Fin",
        "... " * 30 + "mot ! " + ", " * 30,
    ],
)
def test_no_character_is_lost_or_reordered(text, max_chars):
    parts = split_sentences(text, max_chars)
    assert squash("".join(parts)) == squash(text)
    assert all(part and part == part.strip() for part in parts)
    # La limite compte les lettres et les chiffres : les signes rattachés à un morceau ne la font pas dépasser.
    assert all(sum(char.isalnum() for char in part) <= max_chars for part in parts)
    assert all(any(char.isalnum() for char in part) for part in parts)  # chaque morceau a de quoi être dit


def test_split_refuses_a_limit_below_one():
    with pytest.raises(ValueError):
        split_sentences("Bonjour.", 0)


@pytest.mark.parametrize("text", ["." * (MAX_INPUT_CHARS - 1) + "x", "!?" * 2047 + "x"])
def test_split_takes_linear_time_on_a_long_run_of_signs_followed_by_a_letter(text):
    started = time.perf_counter()
    parts = split_sentences(text)
    assert time.perf_counter() - started < 0.05  # en temps quadratique : plusieurs secondes
    assert squash("".join(parts)) == squash(text)


def test_split_attaches_a_piece_without_letter_or_digit_to_the_previous_sentence():
    assert split_sentences("Oui ! ... Non") == ["Oui ! ...", "Non"]
    assert split_sentences("Oui. ... ... Non") == ["Oui. ... ...", "Non"]
    assert split_sentences("Oui.\n?\nNon") == ["Oui. ?", "Non"]
    assert split_sentences("Oui. ?!") == ["Oui. ?!"]


def test_split_attaches_a_leading_piece_without_letter_or_digit_to_the_next_sentence():
    assert split_sentences("? Bonjour.") == ["? Bonjour."]
    assert split_sentences("...\n!\nBonjour. Salut") == ["... ! Bonjour.", "Salut"]


def test_split_attaches_a_sign_left_alone_by_a_cut():
    assert split_sentences("abcde , fghij", 5) == ["abcde ,", "fghij"]
    assert split_sentences("abcde fghij ,", 5) == ["abcde", "fghij ,"]


def test_split_counts_letters_and_digits_in_a_piece_not_the_signs_attached_to_it():
    assert split_sentences("Oui. ... Non", 4) == ["Oui. ...", "Non"]  # « Oui. ... » fait 8 caractères, 3 lettres


def test_split_of_text_without_letter_or_digit_keeps_the_pieces_as_they_are():
    assert split_sentences("?!") == ["?!"]
    assert split_sentences("... ?\n!") == ["...", "?", "!"]


# --- Choix de la voix ---


@pytest.mark.parametrize(
    "requested,expected", [("alloy", "Sofia"), ("leo", "Leo"), (None, "Sofia"), ("default", "Sofia")]
)
def test_resolve_voice(requested, expected):
    assert resolve_voice(requested, "Sofia", ["John", "Sofia", "Aria", "Jason", "Leo"]) == expected


def test_resolve_voice_returns_the_name_as_nemo_writes_it():
    assert resolve_voice("JASON", "Sofia", ("John", "Jason")) == "Jason"
    assert resolve_voice("", "Aria", ("John", "Jason")) == "Aria"


# --- La route ---


@pytest.fixture
def say(transcriptions_client, service_headers, fake_nemo):
    """Envoie un corps JSON à la route ; le faux NeMo rend une demi-seconde de silence par phrase.

    Sans `content`, le corps est `body` s'il est donné, ou le texte « Bonjour. » avec les `fields`.
    """
    fake_nemo.pcm = silence(0.5)

    def say(body=None, *, content: bytes | None = None, **fields):
        if content is not None:
            headers = {**service_headers, "Content-Type": "application/json"}
            return transcriptions_client.post(URL, headers=headers, content=content)
        payload = {"input": "Bonjour.", **fields} if body is None else body
        return transcriptions_client.post(URL, headers=service_headers, json=payload)

    return say


def speak_fails(fake_nemo: FakeNemo, error: Exception) -> None:
    """Fait lever `error` à la synthèse, la lecture des voix restant intacte."""

    async def speak(text, *, voice, language):
        raise error

    fake_nemo.speak = speak


def speak_fails_after_the_first_sentence(fake_nemo: FakeNemo, error: Exception) -> None:
    real = fake_nemo.speak

    async def speak(text, *, voice, language):
        pcm = await real(text, voice=voice, language=language)
        if len(fake_nemo.speak_calls) > 1:
            raise error
        return pcm

    fake_nemo.speak = speak


def test_speech_through_the_sdk(openai_client, fake_nemo):
    fake_nemo.pcm = silence(0.5)
    audio = openai_client.audio.speech.create(model="tts-1", voice="alloy", input="Bonjour. Au revoir.")
    assert audio.content.startswith(MP3_MAGIC)
    assert [(c["text"], c["voice"], c["language"]) for c in fake_nemo.speak_calls] == [
        ("Bonjour.", "Leo", "fr-FR"),
        ("Au revoir.", "Leo", "fr-FR"),
    ]


def test_app_starts_without_the_voices_of_nemo(settings):
    assert create_app(settings).state.voices is None


def test_voices_are_read_once_and_kept_in_the_app_state(say, fake_nemo, transcriptions_client):
    assert say(voice="leo").status_code == 200
    assert say(voice="john").status_code == 200
    assert len(fake_nemo.voices_calls) == 1
    assert [call["voice"] for call in fake_nemo.speak_calls] == ["Leo", "John"]
    assert transcriptions_client.app.state.voices == ("John", "Sofia", "Aria", "Jason", "Leo")


@pytest.mark.parametrize("error,status", [(NemoOverloaded("busy"), 429), (NemoUnavailable("down"), 502)])
def test_failed_voice_lookup_is_a_429_or_502_and_is_not_kept(say, fake_nemo, error, status):
    fake_nemo.error = error
    assert say(voice="leo").status_code == status
    fake_nemo.error = None
    assert say(voice="leo").status_code == 200
    assert len(fake_nemo.voices_calls) == 2
    assert [call["voice"] for call in fake_nemo.speak_calls] == ["Leo"]


def test_requested_voice_is_matched_without_regard_to_case(say, fake_nemo):
    for requested in ("leo", "LEO", "Leo"):
        assert say(voice=requested).status_code == 200
    assert [call["voice"] for call in fake_nemo.speak_calls] == ["Leo"] * 3


def test_unknown_or_missing_voice_takes_the_voice_of_the_profile(transcriptions_client, fake_nemo, db):
    fake_nemo.pcm = silence(0.5)
    user = db.upsert_user("sub-1", "ana@example.org", "Ana")
    db.save_settings(user, voice="Aria")
    headers = {"Authorization": f"Bearer {db.create_key(user, 'Android')}"}
    for fields in ({"voice": "nova"}, {"voice": 7}, {"voice": None}, {}):
        r = transcriptions_client.post(URL, headers=headers, json={"input": "Salut.", **fields})
        assert r.status_code == 200
    assert [call["voice"] for call in fake_nemo.speak_calls] == ["Aria"] * 4
    transcriptions_client.post(URL, headers=headers, json={"input": "Salut.", "voice": "JASON"})
    assert fake_nemo.speak_calls[-1]["voice"] == "Jason"


def test_language_is_always_french_and_other_fields_are_ignored(say, fake_nemo):
    r = say(model="tts-1-hd", instructions="Parle doucement", stream_format="sse", language="en", extra=[1])
    assert r.status_code == 200
    assert [call["language"] for call in fake_nemo.speak_calls] == ["fr-FR"]


@pytest.mark.parametrize("fmt", list(OUTPUT_FORMATS))
def test_each_format_has_its_content_type_and_its_container(say, fmt):
    r = say(response_format=fmt)
    assert r.status_code == 200
    assert r.headers["content-type"] == OUTPUT_FORMATS[fmt]
    assert r.content and r.content.startswith(MAGICS[fmt])


def test_mp3_is_the_default_format(say):
    r = say()
    assert r.headers["content-type"] == "audio/mpeg" and r.content.startswith(MP3_MAGIC)


def test_pcm_is_24k_mono_16_bit_and_covers_every_sentence(say, fake_nemo):
    fake_nemo.pcm = silence(1.0)
    r = say(input="Un. Deux.", response_format="pcm")
    expected = SPEECH_RATE * 2 * 2.0
    assert abs(len(r.content) - expected) < expected * 0.02


def test_speed_is_applied_by_ffmpeg(say, fake_nemo):
    fake_nemo.pcm = silence(1.0)
    r = say(input="Un. Deux.", response_format="pcm", speed=2)
    expected = SPEECH_RATE * 2 * 2.0 / 2
    assert abs(len(r.content) - expected) < expected * 0.05


@pytest.mark.parametrize("speed", ["5", "4.5", "0.1", "0", "-1", '"2"', "true", "[1.5]", "NaN", "Infinity"])
def test_out_of_range_or_non_numeric_speed_is_400(say, fake_nemo, speed):
    r = say(content=f'{{"input": "Bonjour.", "speed": {speed}}}'.encode())
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["type"], error["param"]) == ("invalid_request_error", "speed")
    assert fake_nemo.calls == []


@pytest.mark.parametrize("speed", [0.25, 1, 1.5, 4.0])
def test_speed_bounds_are_accepted(say, speed):
    assert say(speed=speed).status_code == 200


@pytest.mark.parametrize("text", ["", " ", "   ", "\n\t \r\n"])
def test_blank_input_is_400_without_calling_nemo(say, fake_nemo, text):
    r = say(input=text)
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["type"], error["param"]) == ("invalid_request_error", "input")
    assert fake_nemo.calls == []


@pytest.mark.parametrize("body", [{}, {"input": None}, {"input": 42}, {"input": ["Bonjour."]}, {"voice": "leo"}])
def test_input_that_is_missing_or_not_text_is_400(say, fake_nemo, body):
    r = say(body)
    assert r.status_code == 400 and r.json()["error"]["param"] == "input"
    assert fake_nemo.calls == []


@pytest.mark.parametrize("text", ["?", "...", "!?\n…", " . , ; ", "« »", "-- ♥ 😀"])
def test_input_without_a_letter_or_a_digit_is_400_without_calling_nemo(say, fake_nemo, text):
    r = say(input=text)
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", "input", "invalid_value")
    assert fake_nemo.calls == []


@pytest.mark.parametrize("text", ["a", "7", "?a", "... Oui"])
def test_input_with_a_single_letter_or_digit_is_accepted(say, fake_nemo, text):
    assert say(input=text).status_code == 200
    assert len(fake_nemo.speak_calls) == 1


def test_piece_without_letter_or_digit_goes_to_nemo_with_its_neighbour(say, fake_nemo):
    assert say(input="Oui ! ... Non").status_code == 200
    assert [call["text"] for call in fake_nemo.speak_calls] == ["Oui ! ...", "Non"]


def json_input_with(*units: str) -> bytes:
    """Un corps JSON dont `input` est « Bonjour » suivi des unités UTF-16 données, écrites en échappements JSON.

    Une unité se donne sans son antislash (`ud800`) : le JSON est monté ici, pour que le fichier ne contienne
    aucun substitut isolé.
    """
    escapes = "".join(chr(92) + unit for unit in units)
    return ('{"input": "Bonjour' + escapes + '"}').encode()


@pytest.mark.parametrize("units", [("ud800",), ("udc00",), ("ude00", "ud83d"), ("ud83d", "u0041")])
def test_input_with_a_lone_surrogate_is_400_without_calling_nemo(say, fake_nemo, units):
    r = say(content=json_input_with(*units))
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", "input", "invalid_value")
    assert fake_nemo.calls == []


def test_input_with_a_surrogate_pair_is_accepted(say, fake_nemo):
    assert say(content=json_input_with("ud83d", "ude00")).status_code == 200
    assert [call["text"] for call in fake_nemo.speak_calls] == ["Bonjour😀"]


def test_input_over_4096_characters_is_400(say, fake_nemo):
    r = say(input="a" * (MAX_INPUT_CHARS + 1))
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["param"], error["code"]) == ("input", "string_above_max_length")
    assert fake_nemo.calls == []


def test_input_of_4096_characters_is_accepted(say, fake_nemo):
    assert MAX_INPUT_CHARS == 4096
    assert say(input="a" * MAX_INPUT_CHARS).status_code == 200
    assert len(fake_nemo.speak_calls) == 14  # treize morceaux de 300 caractères et un de 196


@pytest.mark.parametrize("content", [b"", b"{", b"\xff\xfe\x00", b"[1]", b"null", b'"texte"', b"42", b"[" * 200_000])
def test_body_that_is_not_a_json_object_is_400(say, fake_nemo, content):
    r = say(content=content)
    assert r.status_code == 400
    error = r.json()["error"]
    assert (error["type"], error["param"]) == ("invalid_request_error", None)
    assert fake_nemo.calls == []


@pytest.mark.parametrize("fmt", ["ogg", "", "MP3", 7, ["mp3"]])
def test_unsupported_response_format_is_400(say, fake_nemo, fmt):
    r = say(response_format=fmt)
    assert r.status_code == 400 and r.json()["error"]["param"] == "response_format"
    assert fake_nemo.calls == []


@pytest.mark.parametrize(
    "fields,param",
    [
        ({"input": " ", "response_format": "ogg", "speed": 9}, "input"),
        ({"input": "?!", "response_format": "ogg", "speed": 9}, "input"),
        ({"input": "a" * 5000, "response_format": "ogg", "speed": 9}, "input"),
        ({"input": "Bonjour.", "response_format": "ogg", "speed": 9}, "response_format"),
        ({"input": "Bonjour.", "speed": 9}, "speed"),
    ],
)
def test_fields_are_checked_in_order_before_any_call_to_nemo(say, fake_nemo, fields, param):
    r = say(fields)
    assert r.status_code == 400 and r.json()["error"]["param"] == param
    assert fake_nemo.calls == []


def test_body_over_one_mebibyte_is_413(say, fake_nemo):
    r = say(content=b'{"input": "' + b"a" * (2 * MIB) + b'"}')
    assert r.status_code == 413
    error = r.json()["error"]
    assert (error["type"], error["code"]) == ("invalid_request_error", "payload_too_large")
    assert fake_nemo.calls == []


@pytest.mark.parametrize("where", ["voices", "speak"])
@pytest.mark.parametrize(
    "error,status,error_type,code",
    [
        (NemoOverloaded("the queue is full"), 429, "rate_limit_error", "overloaded"),
        (NemoUnavailable("connection refused"), 502, "server_error", "upstream_error"),
        (NemoError("synthesis failed"), 502, "server_error", "upstream_error"),
    ],
)
def test_nemo_failure_before_the_first_sound_is_a_429_or_502(
    say, fake_nemo, caplog, where, error, status, error_type, code
):
    if where == "voices":
        fake_nemo.error = error
    else:
        speak_fails(fake_nemo, error)
    caplog.set_level(logging.INFO, logger="dictee")
    r = say()
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()["error"]
    assert (body["type"], body["code"], body["param"]) == (error_type, code, None)
    assert str(error) not in body["message"]
    (line,) = log_lines(caplog)
    assert line.endswith(f"status={status}")


@pytest.mark.parametrize(
    "error,status",
    [
        (NemoOverloaded("the queue is full"), 429),
        (NemoUnavailable("connection refused"), 502),
        (NemoError("synthesis failed"), 502),
    ],
)
def test_nemo_failure_after_the_first_sentence_cuts_the_stream(say, fake_nemo, caplog, error, status):
    # Le statut 200 est parti avec le premier son : la panne coupe le flux et se dit au journal, par sa seule classe.
    speak_fails_after_the_first_sentence(fake_nemo, error)
    caplog.set_level(logging.INFO, logger="dictee")
    r = say(input="Bonjour. Au revoir. Merci.")
    assert r.status_code == 200
    assert r.content.startswith(MP3_MAGIC)
    messages = log_lines(caplog)
    assert f"speech stream cut class={type(error).__name__}" in messages
    assert messages[-1].endswith(f"status={status}") and len(messages) == 2
    assert str(error) not in caplog.text
    assert [call["text"] for call in fake_nemo.speak_calls] == ["Bonjour.", "Au revoir."]  # rien après la panne


@pytest.mark.parametrize("where", ["voices", "first-sentence", "next-sentence", "encoder"])
def test_unexpected_error_is_a_500_that_leaks_nothing(say, fake_nemo, caplog, monkeypatch, where):
    # Un texte à dire pourrait se retrouver dans le message d'une exception : il ne sort ni par la réponse, ni par un
    # journal, y compris celui d'uvicorn qui journalise le message d'une exception que Starlette relève.
    marker = "Magpie-marker-7f3a91"
    boom = RuntimeError(f"failed on {marker}")
    match where:
        case "voices":
            fake_nemo.error = boom
        case "first-sentence":
            speak_fails(fake_nemo, boom)
        case "next-sentence":
            speak_fails_after_the_first_sentence(fake_nemo, boom)
        case "encoder":

            async def broken_encoder(pcm_chunks, **kwargs):
                raise boom
                yield b""

            monkeypatch.setattr(speech, "encode_stream", broken_encoder)
    caplog.set_level(logging.DEBUG)
    r = say(input="Bonjour. Au revoir.")
    if where in ("voices", "first-sentence"):
        assert r.status_code == 500
        assert r.json() == UNEXPECTED_BODY
        assert "unexpected error class=RuntimeError" in log_lines(caplog)
    else:
        assert r.status_code == 200  # le flux était ouvert : il est coupé
        assert "speech stream cut class=RuntimeError" in log_lines(caplog)
    assert marker not in r.text and marker not in caplog.text
    for record in caplog.records:
        assert marker not in record.getMessage()
        assert marker not in str(record.exc_info) and marker not in str(record.args)
    assert log_lines(caplog)[-1].endswith("status=500")


def test_log_line_says_who_what_and_how_it_ended(caplog, say, transcriptions_client, db):
    user = db.upsert_user("sub-1", "ana@example.org", "Ana")
    personal = {"Authorization": f"Bearer {db.create_key(user, 'Android')}"}
    caplog.set_level(logging.INFO, logger="dictee")
    say(input="Bonjour. Au revoir.", response_format="wav")
    transcriptions_client.post(URL, headers=personal, json={"input": "Salut", "voice": "sofia", "speed": 1.5})
    say(input="  ")
    transcriptions_client.post(URL, json={"input": "Salut"})
    patterns = [
        r"user=default route=speech chars=19 sentences=2 voice=Leo format=wav status=200",
        rf"user={user} route=speech chars=5 sentences=1 voice=Sofia format=mp3 status=200",
        r"user=default route=speech chars=2 sentences=- voice=- format=- status=400",
        r"user=- route=speech chars=- sentences=- voice=- format=- status=401",
    ]
    lines = log_lines(caplog)
    assert len(lines) == len(patterns)
    for line, pattern in zip(lines, patterns, strict=True):
        assert re.fullmatch(pattern, line), line


def test_logs_never_contain_the_text(caplog, say):
    caplog.set_level(logging.DEBUG)
    for fields in ({}, {"response_format": "wav"}, {"speed": 2}, {"voice": "leo"}):
        r = say(input="Nemotron et Sortformer parlent. Magpie répond.", **fields)
        assert r.status_code == 200
    assert len(log_lines(caplog)) == 4
    for secret in ("Nemotron", "Sortformer", "Magpie", "parlent", "répond"):
        assert secret not in caplog.text


def test_voice_of_the_profile_cannot_forge_a_log_line(caplog, transcriptions_client, fake_nemo, db):
    fake_nemo.pcm = silence(0.5)
    user = db.upsert_user("sub-1", "ana@example.org", "Ana")
    db.save_settings(user, voice="Sofia\nuser=1 status=200 " + "y" * 200)
    headers = {"Authorization": f"Bearer {db.create_key(user, 'Android')}"}
    caplog.set_level(logging.INFO, logger="dictee")
    transcriptions_client.post(URL, headers=headers, json={"input": "Salut."})
    (line,) = log_lines(caplog)
    assert "\n" not in line and len(line) < 250


# --- Client qui raccroche ---


class HoldingNemo(FakeNemo):
    """Rend les `answered` premières phrases aussitôt et retient les suivantes jusqu'à `released`.

    `cancelled` compte les attentes coupées : une synthèse en cours que la route abandonne.
    """

    def __init__(self, *, answered: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self.answered = answered
        self.released = anyio.Event()
        self.cancelled = 0

    async def speak(self, text, *, voice, language):
        pcm = await super().speak(text, voice=voice, language=language)
        if len(self.speak_calls) > self.answered:
            try:
                await self.released.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return pcm


@pytest.fixture
def spawned(monkeypatch):
    """Les processus que le code lance, pour vérifier qu'aucun ne survit au client."""
    processes = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return processes


def json_headers(service_headers, body: bytes) -> dict[str, str]:
    return {**service_headers, "content-type": "application/json", "content-length": str(len(body))}


def audio_sent(sent: list) -> bool:
    return any(message["type"] == "http.response.body" and message.get("body") for message in sent)


def scope_for(headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
    }


async def call_app(
    app, headers: dict[str, str], body: bytes, hung_up: anyio.Event, sent: list, on_send=None, *, complete: bool = True
) -> None:
    """Appelle l'app sans client HTTP ; après le corps, `receive` attend `hung_up` puis dit `http.disconnect`.

    `complete` faux laisse le corps inachevé : le client raccroche pendant l'envoi.
    """
    messages = [{"type": "http.request", "body": body, "more_body": not complete}]

    async def receive():
        if messages:
            return messages.pop(0)
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if on_send is not None:
            await on_send(message)

    await app(scope_for(headers), receive, send)


def open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


def statuses(sent: list) -> list[int]:
    return [message["status"] for message in sent if message["type"] == "http.response.start"]


@pytest.mark.anyio
async def test_the_first_sentence_is_sent_before_the_next_one_is_synthesized(
    caplog, settings, fake_llm, service_headers
):
    nemo = HoldingNemo(pcm=silence(0.5))
    app = create_app(settings, nemo=nemo, http=fake_llm.http)
    body = json.dumps({"input": "Un. Deux. Trois."}).encode()
    sent: list = []
    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, json_headers(service_headers, body), body, anyio.Event(), sent)
        await wait_until(lambda: audio_sent(sent), timeout=10)
        # Le son de la première phrase est parti, la deuxième se synthétise et la réponse n'est pas finie.
        assert len(nemo.speak_calls) == 2
        assert sent[-1].get("more_body") is True
        nemo.released.set()
    assert statuses(sent) == [200]
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert [call["text"] for call in nemo.speak_calls] == ["Un.", "Deux.", "Trois."]
    (line,) = log_lines(caplog)
    assert line.endswith("sentences=3 voice=Leo format=mp3 status=200")


@pytest.mark.anyio
async def test_client_hanging_up_stops_the_synthesis_and_kills_ffmpeg(
    caplog, settings, fake_llm, service_headers, spawned
):
    nemo = HoldingNemo(pcm=silence(0.5))
    app = create_app(settings, nemo=nemo, http=fake_llm.http)
    body = json.dumps({"input": "Un. Deux. Trois. Quatre. Cinq."}).encode()
    hung_up = anyio.Event()
    sent: list = []
    caplog.set_level(logging.INFO, logger="dictee")
    descriptors = open_descriptors()
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, json_headers(service_headers, body), body, hung_up, sent)
        await wait_until(lambda: audio_sent(sent), timeout=10)
        await wait_until(lambda: len(nemo.speak_calls) == 2)
        hung_up.set()
        await wait_until(lambda: nemo.cancelled == 1)  # la synthèse en cours est abandonnée
        await wait_until(lambda: [process.returncode for process in spawned] == [-signal.SIGKILL])
    assert len(nemo.speak_calls) == 2  # aucun appel pour les trois phrases suivantes
    # Les tubes de ffmpeg sont fermés sans attendre le ramasse-miettes.
    await wait_until(lambda: open_descriptors() <= descriptors)
    (line,) = log_lines(caplog)
    assert line.endswith("status=499")


@pytest.mark.anyio
async def test_closing_the_stream_stops_the_synthesis_and_kills_ffmpeg(
    caplog, settings, fake_llm, service_headers, spawned
):
    # Starlette ne reprend pas toujours le générateur d'un client parti (une écriture refusée l'abandonne à son
    # dernier `yield`) : sa fermeture, par le ramasse-miettes, arrête alors la synthèse sans le filet de la réponse.
    nemo = HoldingNemo(pcm=silence(0.5))
    app = create_app(settings, nemo=nemo, http=fake_llm.http)
    body = json.dumps({"input": "Un. Deux. Trois. Quatre."}).encode()

    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await anyio.sleep_forever()  # le client ne raccroche jamais : seule la fermeture du flux arrête la synthèse

    request = Request({**scope_for(json_headers(service_headers, body)), "app": app}, receive)
    caplog.set_level(logging.INFO, logger="dictee")
    response = await speech.speech(request)
    stream = response.body_iterator
    assert await anext(stream)  # le son de la première phrase
    await wait_until(lambda: len(nemo.speak_calls) == 2)
    await stream.aclose()
    await wait_until(lambda: nemo.cancelled == 1)
    await wait_until(lambda: [process.returncode for process in spawned] == [-signal.SIGKILL])
    assert len(nemo.speak_calls) == 2
    (line,) = log_lines(caplog)
    assert line.endswith("status=499")


@pytest.mark.anyio
async def test_client_hanging_up_during_the_first_sentence_is_a_499_without_audio(
    caplog, settings, fake_llm, service_headers, spawned
):
    nemo = HoldingNemo(pcm=silence(0.5), answered=0)
    app = create_app(settings, nemo=nemo, http=fake_llm.http)
    body = json.dumps({"input": "Un. Deux. Trois."}).encode()
    hung_up = anyio.Event()
    sent: list = []
    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, json_headers(service_headers, body), body, hung_up, sent)
        await wait_until(lambda: len(nemo.speak_calls) == 1)
        hung_up.set()
        await wait_until(lambda: nemo.cancelled == 1)
    assert statuses(sent) == [499] and not audio_sent(sent)
    assert len(nemo.speak_calls) == 1 and spawned == []  # ni deuxième appel, ni ffmpeg lancé
    (line,) = log_lines(caplog)
    assert line.endswith("status=499")


@pytest.mark.anyio
async def test_client_hanging_up_before_the_stream_starts_still_stops_the_synthesis(
    caplog, settings, fake_llm, service_headers, spawned
):
    # Le client raccroche entre la création de la réponse et son premier octet : le générateur du flux ne démarre
    # jamais et ses `finally` ne courent pas. Seul le filet de la réponse arrête alors la synthèse et ffmpeg.
    nemo = HoldingNemo(pcm=silence(0.5))
    app = create_app(settings, nemo=nemo, http=fake_llm.http)
    body = json.dumps({"input": "Un. Deux. Trois."}).encode()
    hung_up = anyio.Event()
    sent: list = []

    async def hang_up_at_the_start(message):
        if message["type"] == "http.response.start":
            await wait_until(lambda: len(nemo.speak_calls) == 2, timeout=10)  # l'encodage est en cours
            hung_up.set()
            await anyio.sleep_forever()  # annulée ici, avant que le flux ait rien envoyé

    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        headers = json_headers(service_headers, body)
        tg.start_soon(functools.partial(call_app, app, headers, body, hung_up, sent, hang_up_at_the_start))
        await wait_until(lambda: nemo.cancelled == 1, timeout=10)
        await wait_until(lambda: [process.returncode for process in spawned] == [-signal.SIGKILL])
    assert [message["type"] for message in sent] == ["http.response.start"]
    assert len(nemo.speak_calls) == 2
    (line,) = log_lines(caplog)
    assert line.endswith("status=499")


@pytest.mark.anyio
async def test_client_hanging_up_during_the_body_is_not_an_error(
    caplog, settings, fake_nemo, fake_llm, service_headers
):
    app = create_app(settings, nemo=fake_nemo, http=fake_llm.http)
    body = json.dumps({"input": "Bonjour."}).encode()
    hung_up = anyio.Event()
    hung_up.set()
    sent: list = []
    caplog.set_level(logging.INFO, logger="dictee")
    with anyio.fail_after(2):
        await call_app(app, json_headers(service_headers, body), body[:5], hung_up, sent, complete=False)
    assert statuses(sent) == [499]
    assert fake_nemo.calls == []
    (line,) = log_lines(caplog)
    assert line.endswith("status=499")
