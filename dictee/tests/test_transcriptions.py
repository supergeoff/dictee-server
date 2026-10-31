import dataclasses
import json
import logging
import re
import tempfile

import anyio
import httpx
import pytest
from conftest import make_audio
from fakes import FINALS, FakeNemo
from starlette.testclient import TestClient

from dictee import transcribe
from dictee.app import create_app
from dictee.nemo import NemoError, NemoOverloaded, NemoUnavailable
from dictee.transcribe import LongAudioGate
from dictee.types import Final

URL = "/v1/audio/transcriptions"
BOUNDARY = "dictee-test-boundary"
MIB = 1024 * 1024
BAD_FIELDS = [
    ({"language": "xx1"}, "language"),
    ({"response_format": "xml"}, "response_format"),
    ({"stream": "x"}, "stream"),
]


def post(client, headers, audio, **fields):
    return client.post(URL, headers=headers, files={"file": ("a.mp3", audio, "audio/mpeg")}, data=fields)


@pytest.fixture
def ask(transcriptions_client, service_headers, tone_mp3):
    """Envoie trois secondes de sinus à la route, avec les champs donnés, ou `audio` à la place."""

    def ask(audio=None, **fields):
        return post(transcriptions_client, service_headers, tone_mp3 if audio is None else audio, **fields)

    return ask


def sse_events(response) -> list[dict]:
    return [json.loads(block.removeprefix("data: ")) for block in response.text.split("\n\n") if block]


def multipart_body(fields: dict[str, str], audio: bytes) -> tuple[bytes, str]:
    """Corps multipart écrit à la main : les champs d'abord, puis le fichier, comme le font httpx et le SDK d'OpenAI."""
    parts = [
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    file_head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n\r\n'
    parts += [file_head.encode(), audio, b"\r\n", f"--{BOUNDARY}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={BOUNDARY}"


async def chunked(body: bytes):
    """Corps rendu en blocs : httpx l'envoie sans `Content-Length`."""
    for start in range(0, len(body), 65536):
        yield body[start : start + 65536]


async def wait_until(condition, timeout: float = 2.0) -> None:
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(0.01)


def app_with(settings, nemo, llm, **changes):
    return create_app(dataclasses.replace(settings, **changes), nemo=nemo, http=llm.http)


def asgi_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


def test_json_with_any_model_name(openai_client, fake_nemo, tone_mp3):
    r = openai_client.audio.transcriptions.create(model="whisper-1", file=("a.mp3", tone_mp3))
    assert r.text == "Bonjour à tous. Salut."
    call = fake_nemo.calls[0]
    assert (call["language"], call["boost"], call["wav_rate"]) == ("fr-FR", 3.0, 16000)
    assert (call["phrases"], call["prompt"]) == ([], None)
    assert fake_nemo.diarize_calls == []


def test_user_glossary_becomes_boosted_phrases(openai_client, fake_nemo, db, tone_mp3):
    user = db.upsert_user("sub-1", "Ana@Example.org", "Ana")
    db.save_settings(user, glossary="Nemotron\nMagpie")
    openai_client.audio.transcriptions.create(
        model="whisper-1", file=("a.mp3", tone_mp3), extra_headers={"X-OpenWebUI-User-Email": "ana@example.org"}
    )
    assert fake_nemo.calls[0]["phrases"] == ["Nemotron", "Magpie"]


def test_personal_key_uses_its_owner_glossary(transcriptions_client, fake_nemo, db, tone_mp3):
    user = db.upsert_user("sub-1", "ana@example.org", "Ana")
    db.save_settings(user, glossary="Sortformer")
    key = db.create_key(user, "Android")
    headers = {"Authorization": f"Bearer {key}", "X-OpenWebUI-User-Email": "other@example.org"}
    assert post(transcriptions_client, headers, tone_mp3).status_code == 200
    assert fake_nemo.calls[0]["phrases"] == ["Sortformer"]


@pytest.mark.parametrize("sent,expected", [("fr-FR", "fr-FR"), ("EN", "en-US"), ("de", "de-DE"), ("", "fr-FR")])
def test_language_reaches_nemo_in_its_own_form(ask, fake_nemo, sent, expected):
    assert ask(language=sent, prompt="Sortformer").status_code == 200
    assert (fake_nemo.calls[0]["language"], fake_nemo.calls[0]["prompt"]) == (expected, "Sortformer")


def test_verbose_json_reports_the_iso_language(ask):
    assert ask(language="en-US", response_format="verbose_json").json()["language"] == "en"


def test_empty_and_blank_finals_are_left_out_of_the_text(ask, fake_nemo):
    fake_nemo.finals = (Final("", ()), FINALS[0], Final("  ", ()), FINALS[1])
    assert ask().json() == {"text": "Bonjour à tous. Salut."}
    events = sse_events(ask(stream="true"))
    assert [e.get("delta") for e in events] == ["Bonjour à tous.", " Salut.", None]


def test_clean_applies_to_json_and_text_only(openai_client, fake_llm, tone_mp3):
    create = openai_client.audio.transcriptions.create
    assert create(model="dictee-clean", file=("a.mp3", tone_mp3)).text == fake_llm.reply
    assert create(model="dictee-clean", file=("a.mp3", tone_mp3), response_format="text") == fake_llm.reply
    assert len(fake_llm.calls) == 2
    srt = create(model="dictee-clean", file=("a.mp3", tone_mp3), response_format="srt")
    assert "Bonjour à tous." in srt and fake_llm.reply not in srt
    assert len(fake_llm.calls) == 2


def test_diarized_json_through_the_sdk(openai_client, fake_nemo, tone_mp3):
    r = openai_client.audio.transcriptions.create(
        model="gpt-4o-transcribe-diarize",
        file=("a.mp3", tone_mp3),
        response_format="diarized_json",
        chunking_strategy="auto",
    )
    assert [s.speaker for s in r.segments] == ["A", "B"]
    assert [s.text for s in r.segments] == ["Bonjour à tous.", "Salut."]
    assert r.text == "Bonjour à tous. Salut."
    assert len(fake_nemo.diarize_calls) == 1


def test_diarized_json_overrides_the_model_name(ask):
    r = ask(model="whisper-1", response_format="diarized_json")
    assert [s["speaker"] for s in r.json()["segments"]] == ["A", "B"]


def test_verbose_json_words_through_the_sdk(openai_client, tone_mp3):
    r = openai_client.audio.transcriptions.create(
        model="whisper-1", file=("a.mp3", tone_mp3), response_format="verbose_json", timestamp_granularities=["word"]
    )
    assert r.words[0].word == "Bonjour"
    assert not r.segments


@pytest.mark.parametrize(
    "fields,keys",
    [
        ({}, {"segments"}),
        ({"timestamp_granularities": "word"}, {"words"}),
        ({"timestamp_granularities[]": ["word", "segment"]}, {"words", "segments"}),
    ],
)
def test_verbose_json_granularities(ask, fields, keys):
    body = ask(response_format="verbose_json", **fields).json()
    assert {"words", "segments"} & body.keys() == keys


def test_srt_and_vtt_through_the_sdk(openai_client, tone_mp3):
    create = openai_client.audio.transcriptions.create
    assert create(model="whisper-1", file=("a.mp3", tone_mp3), response_format="srt").startswith("1\n")
    assert create(model="whisper-1", file=("a.mp3", tone_mp3), response_format="vtt").startswith("WEBVTT")


def test_stream_through_the_sdk(openai_client, tone_mp3):
    stream = openai_client.audio.transcriptions.create(model="dictee-diarize", file=("a.mp3", tone_mp3), stream=True)
    events = list(stream)
    types = [e.type for e in events]
    assert types[-1] == "transcript.text.done" and "transcript.text.segment" in types
    assert "".join(e.delta for e in events if e.type == "transcript.text.delta") == events[-1].text


def test_stream_sends_one_delta_per_final_then_the_done_event(ask):
    r = ask(stream="TRUE")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert sse_events(r) == [
        {"type": "transcript.text.delta", "delta": "Bonjour à tous."},
        {"type": "transcript.text.delta", "delta": " Salut."},
        {"type": "transcript.text.done", "text": "Bonjour à tous. Salut."},
    ]


def test_stream_with_speakers_sends_the_segments_after_the_deltas(ask):
    events = sse_events(ask(model="dictee-diarize", stream="true"))
    types = [e["type"] for e in events]
    assert types == ["transcript.text.delta"] * 2 + ["transcript.text.segment"] * 2 + ["transcript.text.done"]
    assert [e["speaker"] for e in events if e["type"] == "transcript.text.segment"] == ["A", "B"]


def test_stream_with_clean_sends_the_cleaned_text_in_one_delta(ask, fake_llm):
    assert sse_events(ask(model="dictee-clean", stream="true")) == [
        {"type": "transcript.text.delta", "delta": fake_llm.reply},
        {"type": "transcript.text.done", "text": fake_llm.reply},
    ]


def test_nemo_failure_during_a_stream_is_an_error_event(ask, fake_nemo):
    fake_nemo.error = NemoOverloaded("the queue is full")
    r = ask(stream="true")
    assert r.status_code == 200
    (event,) = sse_events(r)
    assert event["type"] == "error"
    assert (event["error"]["type"], event["error"]["code"]) == ("rate_limit_error", "overloaded")
    assert "queue" not in event["error"]["message"]


def test_stream_accepts_only_true_or_false(ask):
    r = ask(stream="maybe")
    assert r.status_code == 400 and r.json()["error"]["param"] == "stream"
    assert ask(stream="false").json() == {"text": "Bonjour à tous. Salut."}


def test_body_over_the_limit_is_413(settings, fake_nemo, fake_llm, service_headers):
    with TestClient(app_with(settings, fake_nemo, fake_llm, max_upload_mb=1)) as client:
        r = post(client, service_headers, bytes(2 * MIB), model="whisper-1")
    assert r.status_code == 413
    error = r.json()["error"]
    assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", "file", "payload_too_large")
    assert fake_nemo.calls == []


@pytest.mark.anyio
async def test_body_over_the_limit_is_413_without_content_length(settings, fake_nemo, fake_llm, service_headers):
    app = app_with(settings, fake_nemo, fake_llm, max_upload_mb=1)
    body, content_type = multipart_body({"model": "whisper-1"}, bytes(2 * MIB))
    async with asgi_client(app) as client:
        headers = {**service_headers, "Content-Type": content_type}
        request = client.build_request("POST", URL, headers=headers, content=chunked(body))
        assert "content-length" not in request.headers
        r = await client.send(request)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert fake_nemo.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("fields,param", BAD_FIELDS)
async def test_fields_are_checked_before_the_audio_is_read(
    settings, fake_nemo, fake_llm, service_headers, fields, param
):
    # Un corps trop gros pour passer : seul un champ refusé avant la lecture du fichier répond autrement que 413.
    app = app_with(settings, fake_nemo, fake_llm, max_upload_mb=1)
    body, content_type = multipart_body(fields, bytes(2 * MIB))
    async with asgi_client(app) as client:
        headers = {**service_headers, "Content-Type": content_type}
        r = await client.post(URL, headers=headers, content=chunked(body))
    assert r.status_code == 400 and r.json()["error"]["param"] == param


@pytest.mark.parametrize("fields,param", BAD_FIELDS)
def test_fields_are_checked_before_the_audio_is_converted(ask, fake_nemo, fields, param):
    r = ask(b"this is not audio", **fields)
    assert r.status_code == 400 and r.json()["error"]["param"] == param
    assert fake_nemo.calls == []


def test_fields_sent_after_the_file_are_checked_too(transcriptions_client, service_headers):
    files = {"file": ("a.mp3", b"this is not audio"), "language": (None, "xx1")}
    r = transcriptions_client.post(URL, headers=service_headers, files=files)
    assert r.status_code == 400 and r.json()["error"]["param"] == "language"


def test_unreadable_audio_is_422(ask, fake_nemo):
    r = ask(b"this is not audio")
    assert r.status_code == 422
    error = r.json()["error"]
    assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", "file", "invalid_audio")
    assert "ffmpeg" not in error["message"] and "dictee-" not in error["message"]
    assert fake_nemo.calls == []


def test_empty_file_is_422(ask):
    assert ask(b"").status_code == 422


def test_audio_over_the_duration_limit_is_400(ask, fake_nemo, monkeypatch):
    monkeypatch.setattr(transcribe, "MAX_AUDIO_SECONDS", 1.0)
    r = ask()
    assert r.status_code == 400
    assert (r.json()["error"]["param"], r.json()["error"]["code"]) == ("file", "audio_too_long")
    assert fake_nemo.calls == []


def test_speaker_separation_has_its_own_duration_limit(ask, monkeypatch):
    monkeypatch.setattr(transcribe, "MAX_DIARIZE_SECONDS", 1.0)
    assert ask().status_code == 200
    r = ask(model="dictee-diarize")
    assert r.status_code == 400 and r.json()["error"]["code"] == "audio_too_long"
    assert ask(response_format="diarized_json").status_code == 400


def test_each_upload_lives_alone_in_a_directory_removed_afterwards(
    settings, fake_nemo, fake_llm, service_headers, tone_mp3, tmp_path, monkeypatch
):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(uploads))
    seen = []
    convert = transcribe.to_wav16k

    async def spy(path, **kwargs):
        assert list(path.parent.iterdir()) == [path] and path.parent.parent == uploads
        seen.append(path)
        return await convert(path, **kwargs)

    monkeypatch.setattr(transcribe, "to_wav16k", spy)
    with TestClient(app_with(settings, fake_nemo, fake_llm, max_upload_mb=1)) as client:
        assert post(client, service_headers, tone_mp3).status_code == 200
        assert post(client, service_headers, b"this is not audio").status_code == 422
        assert post(client, service_headers, tone_mp3, stream="true").status_code == 200
        assert post(client, service_headers, bytes(2 * MIB)).status_code == 413
    assert len(seen) == 3
    assert list(uploads.iterdir()) == []


def test_request_without_a_file_is_400(transcriptions_client, service_headers):
    r = transcriptions_client.post(URL, headers=service_headers, files={"model": (None, "whisper-1")})
    assert r.status_code == 400
    assert (r.json()["error"]["param"], r.json()["error"]["code"]) == ("file", "missing_required_parameter")


def test_body_that_is_not_multipart_is_400(transcriptions_client, service_headers):
    r = transcriptions_client.post(URL, headers=service_headers, json={"model": "whisper-1"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


def test_malformed_multipart_is_400(transcriptions_client, service_headers):
    headers = {**service_headers, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    r = transcriptions_client.post(URL, headers=headers, content=f"--{BOUNDARY}\r\nbroken header\r\n\r\n".encode())
    assert r.status_code == 400


def test_oversized_text_field_is_400(ask):
    assert ask(prompt="x" * (2 * MIB)).status_code == 400


@pytest.mark.parametrize(
    "error,status,error_type,code",
    [
        (NemoOverloaded("the queue is full"), 429, "rate_limit_error", "overloaded"),
        (NemoUnavailable("connection refused"), 502, "server_error", "upstream_error"),
        (NemoError("session failed"), 502, "server_error", "upstream_error"),
    ],
)
def test_nemo_overloaded_is_429_and_unavailable_is_502(ask, fake_nemo, error, status, error_type, code):
    fake_nemo.error = error
    r = ask()
    assert r.status_code == status
    body = r.json()["error"]
    assert (body["type"], body["code"], body["param"]) == (error_type, code, None)
    assert str(error) not in body["message"]


def test_silence_gives_empty_text(openai_client, ask, fake_nemo, tone_mp3):
    fake_nemo.finals = ()
    assert ask().json() == {"text": ""}
    assert ask(model="dictee-clean").json() == {"text": ""}
    r = openai_client.audio.transcriptions.create(
        model="whisper-1", file=("a.mp3", tone_mp3), response_format="diarized_json"
    )
    assert (r.text, r.segments) == ("", [])
    assert sse_events(ask(model="dictee-diarize", stream="true")) == [{"type": "transcript.text.done", "text": ""}]
    assert fake_nemo.diarize_calls == []


def test_silence_reported_as_a_blank_final_gives_empty_text(ask, fake_nemo):
    # NeMo rend `Final("", ())` pour un audio silencieux ou de moins d'une seconde.
    fake_nemo.finals = (Final("", ()),)
    r = ask(response_format="diarized_json")
    assert r.status_code == 200 and r.json()["segments"] == [] and r.json()["text"] == ""
    assert fake_nemo.diarize_calls == []


def test_logs_never_contain_the_text(caplog, transcriptions_client, fake_llm, db, tone_mp3):
    user = db.upsert_user("sub-1", "ana@example.org", "Ana")
    db.save_settings(user, glossary="Nemotron")
    headers = {"Authorization": f"Bearer {db.create_key(user, 'Android')}"}
    caplog.set_level(logging.INFO)
    for model, fmt in [("dictee-clean", "json"), ("dictee-diarize", "srt"), ("whisper-1", "json")]:
        r = post(transcriptions_client, headers, tone_mp3, model=model, response_format=fmt, prompt="Sortformer")
        assert r.status_code == 200
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert len(lines) == 3
    assert all("decision=" in line and f"user={user} " in line for line in lines)
    for secret in ("Bonjour", "Salut", "Nemotron", "Sortformer", fake_llm.reply):
        assert secret not in caplog.text


def test_log_line_says_who_what_how_long_and_how_it_ended(caplog, ask, transcriptions_client):
    caplog.set_level(logging.INFO, logger="dictee")
    ask(model="whisper-1")
    ask(model="dictee-clean")
    ask(model="dictee-diarize", stream="true")
    ask(b"not audio", model="dictee")
    transcriptions_client.post(URL)
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    start = r"user=default route=transcriptions "
    patterns = [
        start + r"model=whisper-1 mode=raw audio_s=3\.\d nemo_ms=\d+ llm_ms=- decision=- status=200",
        start + r"model=dictee-clean mode=clean audio_s=3\.\d nemo_ms=\d+ llm_ms=\d+ decision=cleaned status=200",
        start + r"model=dictee-diarize mode=diarize audio_s=3\.\d nemo_ms=\d+ llm_ms=- decision=- status=200",
        start + r"model=dictee mode=raw audio_s=- nemo_ms=- llm_ms=- decision=- status=422",
        r"user=- route=transcriptions model=- mode=- audio_s=- nemo_ms=- llm_ms=- decision=- status=401",
    ]
    assert len(lines) == len(patterns)
    for line, pattern in zip(lines, patterns, strict=True):
        assert re.fullmatch(pattern, line), line


def test_model_name_cannot_forge_a_log_line(caplog, ask):
    caplog.set_level(logging.INFO, logger="dictee")
    ask(model="x\nuser=1 status=200 " + "y" * 200)
    (line,) = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert "\n" not in line and len(line) < 250


@pytest.mark.anyio
async def test_gate_lets_short_audio_through_and_serializes_long_audio():
    gate = LongAudioGate(1.0)
    async with gate.slot(5.0):
        with anyio.fail_after(0.5):
            async with gate.slot(1.0):  # pile au seuil : l'audio est court
                pass
        with pytest.raises(TimeoutError), anyio.fail_after(0.1):
            async with gate.slot(5.0):
                pass
    with anyio.fail_after(0.5):
        async with gate.slot(5.0):
            pass


@pytest.mark.anyio
async def test_cancelled_long_request_frees_the_slot():
    gate = LongAudioGate(1.0)
    holding = anyio.Event()

    async def hold():
        async with gate.slot(5.0):
            holding.set()
            await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        tg.cancel_scope.cancel()
    with anyio.fail_after(0.5):
        async with gate.slot(5.0):
            pass


@pytest.mark.anyio
async def test_waiting_for_the_slot_can_be_cancelled():
    gate = LongAudioGate(1.0)
    async with gate.slot(5.0):
        with anyio.move_on_after(0.1):
            async with gate.slot(5.0):
                pytest.fail("the slot is taken")
    with anyio.fail_after(0.5):
        async with gate.slot(5.0):
            pass


@pytest.mark.anyio
async def test_short_request_passes_during_a_long_one(settings, fake_llm, service_headers, tmp_path):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    long_audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    short_audio = make_audio(tmp_path, "short.mp3", seconds=0.5).read_bytes()
    answers = {}
    async with asgi_client(app) as client:

        async def send(name, audio):
            files = {"file": ("a.mp3", audio)}
            answers[name] = await client.post(URL, headers=service_headers, files=files, data={"model": "dictee"})

        async with anyio.create_task_group() as tg:
            tg.start_soon(send, "long", long_audio)
            await wait_until(lambda: len(nemo.calls) == 1)
            with anyio.fail_after(2):
                await send("short", short_audio)
            assert answers["short"].status_code == 200
            assert "long" not in answers
            release.set()
    assert answers["long"].status_code == 200


@pytest.mark.anyio
async def test_second_long_request_waits_for_the_first(settings, fake_llm, service_headers, tmp_path):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    answers = []
    async with asgi_client(app) as client:

        async def send():
            files = {"file": ("a.mp3", audio)}
            answers.append(await client.post(URL, headers=service_headers, files=files, data={"model": "dictee"}))

        async with anyio.create_task_group() as tg:
            tg.start_soon(send)
            await wait_until(lambda: len(nemo.calls) == 1)
            tg.start_soon(send)
            await anyio.sleep(0.3)
            assert len(nemo.calls) == 1  # la seconde attend la place de la première
            release.set()
    assert [a.status_code for a in answers] == [200, 200] and len(nemo.calls) == 2


async def call_app(app, headers: dict[str, str], body: bytes, hung_up: anyio.Event, *, complete: bool = True) -> list:
    """Appelle l'app sans client HTTP et rend les messages envoyés.

    Après le corps, `receive` attend que `hung_up` soit posé, puis dit `http.disconnect`.
    """
    messages = [{"type": "http.request", "body": body, "more_body": not complete}]
    sent = []

    async def receive():
        if messages:
            return messages.pop(0)
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
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
    await app(scope, receive, send)
    return sent


@pytest.mark.anyio
@pytest.mark.parametrize("stream", ["false", "true"])
async def test_client_hanging_up_closes_the_session_and_frees_the_slot(
    stream, caplog, settings, fake_llm, service_headers, tmp_path
):
    # Le faux NeMo ne rend jamais la main : la requête attend dans la session, comme une transcription longue.
    nemo = FakeNemo(finals=FINALS, release=anyio.Event())
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee", "stream": stream}, audio)
    headers = {**service_headers, "content-type": content_type, "content-length": str(len(body))}
    hung_up = anyio.Event()
    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, headers, body, hung_up)
        await wait_until(lambda: nemo.calls)
        hung_up.set()
        await wait_until(lambda: nemo.sessions_closed == 1)
    with anyio.fail_after(0.5):
        async with app.state.gate.slot(3.0):
            pass
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert len(lines) == 1 and lines[0].endswith("status=499")


@pytest.mark.anyio
async def test_client_hanging_up_during_the_upload_is_not_an_error(settings, fake_nemo, fake_llm, service_headers):
    app = app_with(settings, fake_nemo, fake_llm)
    body, content_type = multipart_body({"model": "dictee"}, bytes(1000))
    hung_up = anyio.Event()
    hung_up.set()
    headers = {**service_headers, "content-type": content_type}
    with anyio.fail_after(2):
        sent = await call_app(app, headers, body[:200], hung_up, complete=False)
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [499]
    assert fake_nemo.calls == []
