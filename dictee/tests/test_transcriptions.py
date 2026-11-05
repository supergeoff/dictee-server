import contextlib
import dataclasses
import errno
import functools
import gc
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
import time
import warnings
from pathlib import Path

import anyio
import httpx
import pytest
from conftest import make_audio, personal_headers, wait_until, write_wav
from fakes import FINALS, FakeNemo
from starlette.testclient import TestClient

from dictee import audio as audio_module
from dictee import transcribe
from dictee.app import create_app
from dictee.errors import ApiError
from dictee.nemo import NemoError, NemoOverloaded, NemoUnavailable
from dictee.taskstore import TERMINAL_STATUSES, TaskStatus
from dictee.types import Final, Partial, Word

# Sans `stream`, une transcription passe par une tâche, que NeMo indisponible ou saturé fait reprendre : aucune
# reprise n'attend ici.
pytestmark = pytest.mark.usefixtures("no_retry_delays")

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


def app_with(settings, nemo, llm, **changes):
    return create_app(dataclasses.replace(settings, **changes), nemo=nemo, http=llm.http)


def asgi_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


@contextlib.asynccontextmanager
async def app_running(app):
    """Démarre l'app (lifespan), donc son runner de tâches, et l'arrête à la sortie : `asgi_client` ne le fait pas."""
    async with app.router.lifespan_context(app):
        yield


def tasks_of(client) -> list:
    """Toutes les tâches du registre de l'app, de la plus ancienne à la plus récente."""
    store = client.app.state.store
    return sorted((store.get(task_id) for task_id in store.known_ids()), key=lambda task: (task.created_at, task.id))


def release(client, fake_nemo) -> None:
    """Pose l'événement `release` du faux NeMo depuis le fil du test, par le portail du `TestClient`."""
    client.portal.call(fake_nemo.release.set)


def poll_task(client, headers, task_id: str) -> dict:
    """Relit l'objet de la tâche jusqu'à ce qu'elle soit finie, 5 s au plus, et rend le dernier lu."""
    deadline = time.monotonic() + 5.0
    while True:
        body = client.get(f"{URL}/{task_id}", headers=headers).json()
        if body["status"] in TERMINAL_STATUSES or time.monotonic() > deadline:
            return body
        time.sleep(0.01)


def post_fields(client, headers, audio: bytes, fields: dict[str, str], order: str):
    """Envoie les champs avant le fichier (`before_file`) ou après lui (`after_file`)."""
    parts = [(name, (None, value)) for name, value in fields.items()]
    file = ("file", ("a.mp3", audio, "audio/mpeg"))
    return client.post(URL, headers=headers, files=[*parts, file] if order == "before_file" else [file, *parts])


def request_lines(caplog) -> list[str]:
    """Les lignes de journal des requêtes de la route, sans celles des tâches."""
    lines = (record.getMessage() for record in caplog.records if record.name == "dictee")
    return [line for line in lines if " route=transcriptions " in line]


def request_line(caplog) -> str:
    """La ligne de journal de la seule requête de la route."""
    (line,) = request_lines(caplog)
    return line


def tone_wav(directory: Path) -> Path:
    """Écrit un WAV de trois secondes de sinus et rend son chemin."""
    path = directory / "tone.wav"
    write_wav(path, 3.0)
    return path


# Chaque appel de `upload` prend la fréquence suivante : deux envois ne sont jamais identiques, donc jamais rejoints.
_FREQUENCIES = itertools.count(300, 25)


def upload(directory: Path, name: str, seconds: float) -> dict:
    """La partie `file` d'un WAV de sinus de `seconds` secondes, d'une fréquence que nul autre envoi n'a."""
    path = directory / name
    write_wav(path, seconds, next(_FREQUENCIES))
    return {"file": (name, path.read_bytes(), "audio/wav")}


def take_every_place(app) -> None:
    """Réserve puis rend toutes les places de la file des tâches longues ; lève 429 si une place est restée prise."""
    lane = app.state.lane
    tickets = [lane.reserve() for _ in range(1 + app.state.settings.max_queued_long)]
    for ticket in tickets:
        ticket.release()


async def assert_lane_is_free(app) -> None:
    """Aucune place réservée et aucun tour tenu dans la file des tâches longues."""
    take_every_place(app)
    with anyio.fail_after(0.5):
        async with app.state.lane.reserve().turn():
            pass


def test_json_with_any_model_name(openai_client, fake_nemo, tone_mp3, settings):
    r = openai_client.audio.transcriptions.create(model="whisper-1", file=("a.mp3", tone_mp3))
    assert r.text == "Bonjour à tous. Salut."
    # Sans `stream`, le fichier passe par morceaux (section 7.2 de la spec) : ni session, ni personnes, ni partiels.
    assert fake_nemo.calls == fake_nemo.chunk_calls
    call = fake_nemo.calls[0]
    assert (call["language"], call["boost"], call["phrases"]) == ("fr-FR", settings.glossary_boost, [])


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
    assert ask(language=sent).status_code == 200
    assert fake_nemo.calls[0]["language"] == expected


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
    # Le même envoi en `text` rejoint la tâche reformulée de `json` : il reçoit son texte sans nouvel appel au LLM.
    assert create(model="dictee-clean", file=("a.mp3", tone_mp3), response_format="text") == fake_llm.reply
    assert len(fake_llm.calls) == 1
    srt = create(model="dictee-clean", file=("a.mp3", tone_mp3), response_format="srt")
    assert "Bonjour à tous." in srt and fake_llm.reply not in srt
    assert len(fake_llm.calls) == 1


def test_a_clean_upload_that_fell_back_to_the_raw_text_is_reformulated_when_sent_again(
    openai_client, transcriptions_client, fake_llm, tone_mp3
):
    create = openai_client.audio.transcriptions.create
    reply, fake_llm.reply = fake_llm.reply, ""
    assert create(model="dictee-clean", file=("a.mp3", tone_mp3)).text == "Bonjour à tous. Salut."
    fake_llm.reply = reply
    # La première tâche a gardé le texte brut faute de réponse du LLM : le même envoi refait la tâche et la reformulation.
    assert create(model="dictee-clean", file=("a.mp3", tone_mp3)).text == reply
    assert len(fake_llm.calls) == 2 and len(tasks_of(transcriptions_client)) == 2


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
    assert [call["speakers"] for call in fake_nemo.calls] == [True]


def test_diarized_json_overrides_the_model_name(ask):
    r = ask(model="whisper-1", response_format="diarized_json")
    assert [s["speaker"] for s in r.json()["segments"]] == ["A", "B"]


def test_diarized_json_takes_its_speakers_from_the_realtime_session(ask, fake_nemo):
    body = ask(response_format="diarized_json").json()
    assert [segment["speaker"] for segment in body["segments"]] == ["A", "B"]
    assert fake_nemo.calls[0]["speakers"] is True


@pytest.mark.parametrize(
    "fields,asked",
    [
        ({}, False),
        ({"model": "dictee-clean"}, False),
        ({"model": "dictee", "stream": "true"}, False),
        ({"model": "dictee-diarize"}, True),
        ({"response_format": "diarized_json"}, True),
        ({"model": "dictee-diarize", "stream": "true"}, True),
    ],
    ids=["raw", "clean", "stream", "diarize", "diarized-json", "diarize-stream"],
)
def test_nemo_is_asked_for_the_speakers_in_meeting_mode_only(ask, fake_nemo, fields, asked):
    assert ask(**fields).status_code == 200
    # Un morceau (sans `stream`, hors réunion) passe par la route REST de NeMo, qui ne marque jamais les personnes.
    assert fake_nemo.calls[0].get("speakers", False) is asked


def test_a_meeting_whose_words_have_no_speaker_goes_all_to_a(ask, fake_nemo):
    fake_nemo.finals = (Final("Bonjour.", (Word("Bonjour.", 0.0, 0.5),)),)
    body = ask(response_format="diarized_json").json()
    assert [(segment["speaker"], segment["text"]) for segment in body["segments"]] == [("A", "Bonjour.")]


def test_prompt_is_accepted_and_never_reaches_nemo(ask, fake_nemo):
    assert ask(prompt="Sortformer").status_code == 200
    assert "prompt" not in fake_nemo.calls[0]


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


# Ce que NeMo produit en cours de route (`Partial`), puis la phrase finale de chaque item : le premier item finit sur
# « tous. » que NeMo n'avait pas encore dit, le second a été dit en entier avant sa phrase finale.
PARTIAL_FINALS = (Partial("Bonjour"), Partial(" à"), FINALS[0], Partial("Sa"), FINALS[1])
PARTIAL_DELTAS = ["Bonjour", " à", " tous.", " Sa", "lut."]


def sent_events(sent: list) -> list[dict]:
    """Événements SSE des messages `http.response.body` déjà envoyés par `call_app`."""
    text = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return [json.loads(block.removeprefix("data: ")) for block in text.decode().split("\n\n") if block]


def deltas(events: list[dict]) -> list[str]:
    return [event["delta"] for event in events if event["type"] == "transcript.text.delta"]


def test_stream_sends_a_delta_per_partial_then_the_rest_of_the_final(ask, fake_nemo):
    fake_nemo.finals = PARTIAL_FINALS
    events = sse_events(ask(stream="true"))
    assert deltas(events) == PARTIAL_DELTAS
    assert events[-1] == {"type": "transcript.text.done", "text": "Bonjour à tous. Salut."}
    assert fake_nemo.calls[0]["partials"] is True


def test_stream_deltas_add_up_to_the_done_text_through_the_sdk(openai_client, fake_nemo, tone_mp3):
    fake_nemo.finals = PARTIAL_FINALS
    events = list(openai_client.audio.transcriptions.create(model="dictee", file=("a.mp3", tone_mp3), stream=True))
    assert [e.delta for e in events if e.type == "transcript.text.delta"] == PARTIAL_DELTAS
    assert events[-1].type == "transcript.text.done"
    assert "".join(e.delta for e in events if e.type == "transcript.text.delta") == events[-1].text


def test_stream_sends_nothing_more_when_the_partials_already_say_the_whole_final(ask, fake_nemo):
    fake_nemo.finals = (Partial("Bonjour à "), Partial("tous."), FINALS[0], Partial("Salut."), FINALS[1])
    events = sse_events(ask(stream="true"))
    assert deltas(events) == ["Bonjour à ", "tous.", " Salut."]
    assert events[-1]["text"] == "Bonjour à tous. Salut."


def test_stream_sends_nothing_more_when_the_final_does_not_start_with_the_partials(ask, fake_nemo):
    # Le texte de `done` fait foi : un item que sa phrase finale contredit n'ajoute rien, le suivant repart normalement.
    corrected = Final("Salut tout le monde.", ())
    fake_nemo.finals = (Partial("Bonjour"), corrected, Partial("Sa"), FINALS[1])
    events = sse_events(ask(stream="true"))
    assert deltas(events) == ["Bonjour", " Sa", "lut."]
    assert events[-1] == {"type": "transcript.text.done", "text": "Salut tout le monde. Salut."}


def test_stream_puts_one_space_between_two_items_whatever_the_partials_begin_with(ask, fake_nemo):
    fake_nemo.finals = (Partial("  "), Partial(" Bonjour"), FINALS[0], Partial("  Salut"), Partial("."), FINALS[1])
    events = sse_events(ask(stream="true"))
    # Ni espace en tête du texte, ni espace en double entre les items, ni delta d'espaces seules.
    assert deltas(events) == ["Bonjour", " à tous.", " Salut", "."]
    assert "".join(deltas(events)) == events[-1]["text"] == "Bonjour à tous. Salut."


def test_stream_ignores_empty_items_and_empty_partials(ask, fake_nemo):
    fake_nemo.finals = (Final("", ()), Partial(""), Partial("Bonjour"), FINALS[0], Final("  ", ()), FINALS[1])
    events = sse_events(ask(stream="true"))
    assert deltas(events) == ["Bonjour", " à tous.", " Salut."]
    assert events[-1]["text"] == "Bonjour à tous. Salut."


def test_stream_with_speakers_sends_the_partials_before_the_segments(ask, fake_nemo):
    fake_nemo.finals = PARTIAL_FINALS
    events = sse_events(ask(model="dictee-diarize", stream="true"))
    assert [e["type"] for e in events] == ["transcript.text.delta"] * 5 + ["transcript.text.segment"] * 2 + [
        "transcript.text.done"
    ]
    assert deltas(events) == PARTIAL_DELTAS
    assert events[-1]["text"] == "Bonjour à tous. Salut."


def test_stream_with_clean_keeps_its_single_delta_whatever_nemo_sends_on_the_way(ask, fake_nemo, fake_llm):
    fake_nemo.finals = PARTIAL_FINALS
    assert sse_events(ask(model="dictee-clean", stream="true")) == [
        {"type": "transcript.text.delta", "delta": fake_llm.reply},
        {"type": "transcript.text.done", "text": fake_llm.reply},
    ]
    assert fake_nemo.calls[0]["partials"] is False


@pytest.mark.parametrize(
    "fields,asked",
    [
        ({"model": "dictee", "stream": "true"}, True),
        ({"model": "dictee-diarize", "stream": "true"}, True),
        ({"model": "dictee-clean", "stream": "true"}, False),
        ({"model": "dictee", "stream": "false"}, False),
        ({"model": "dictee-diarize"}, False),
    ],
    ids=["stream", "stream-with-speakers", "stream-with-clean", "no-stream", "no-stream-with-speakers"],
)
def test_partials_are_asked_of_nemo_only_by_a_stream_that_shows_them(ask, fake_nemo, fields, asked):
    fake_nemo.finals = PARTIAL_FINALS
    assert ask(**fields).status_code == 200
    # Un morceau (sans `stream`, hors réunion) passe par la route REST de NeMo, qui ne rend jamais de partiels.
    assert fake_nemo.calls[0].get("partials", False) is asked


def test_the_text_without_stream_ignores_the_partials(ask, fake_nemo):
    fake_nemo.finals = PARTIAL_FINALS
    assert ask().json() == {"text": "Bonjour à tous. Salut."}
    assert ask(response_format="verbose_json").json()["text"] == "Bonjour à tous. Salut."


@pytest.mark.parametrize(
    "error,status",
    [(NemoOverloaded("the queue is full"), 429), (NemoUnavailable("connection refused"), 502), (NemoError("x"), 502)],
)
def test_nemo_failure_before_the_first_partial_is_a_429_or_502(ask, fake_nemo, error, status):
    # La lecture anticipée s'arrête au premier `Partial` : sans lui, rien n'est parti et le statut dit la panne.
    fake_nemo.finals = PARTIAL_FINALS
    fake_nemo.error = error
    r = ask(stream="true")
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")


def test_nemo_failure_after_the_first_partial_is_an_error_event(ask, fake_nemo):
    # Le premier `Partial` a ouvert la réponse, bien avant la première phrase finale : la panne se dit dans le flux.
    fake_nemo.finals = PARTIAL_FINALS
    fake_nemo.error = NemoOverloaded("the queue is full")
    fake_nemo.after = 1
    r = ask(stream="true")
    assert r.status_code == 200
    delta, event = sse_events(r)
    assert delta == {"type": "transcript.text.delta", "delta": "Bonjour"}
    assert (event["type"], event["error"]["code"]) == ("error", "overloaded")


@pytest.mark.anyio
async def test_stream_opens_with_the_first_partial_not_the_first_final(settings, fake_llm, service_headers, tmp_path):
    release = anyio.Event()
    nemo = FakeNemo(finals=PARTIAL_FINALS, release=release, after=1)
    app = app_with(settings, nemo, fake_llm)
    audio = make_audio(tmp_path, "a.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee", "stream": "true"}, audio)
    headers = streaming_headers(service_headers, body, content_type)
    hung_up = anyio.Event()
    sent: list = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(call_app, app, headers, body, hung_up, sent=sent))
        await wait_until(lambda: sent_events(sent))
        # Le faux NeMo n'a rendu que le premier `Partial` et attend : la réponse est ouverte, avec son premier delta.
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
        assert sent_events(sent) == [{"type": "transcript.text.delta", "delta": "Bonjour"}]
        release.set()
        await wait_until(lambda: sent_events(sent)[-1:] and sent_events(sent)[-1]["type"] == "transcript.text.done")
        hung_up.set()
    events = sent_events(sent)
    assert deltas(events) == PARTIAL_DELTAS
    assert events[-1] == {"type": "transcript.text.done", "text": "Bonjour à tous. Salut."}
    assert nemo.sessions_closed == 1


@pytest.mark.parametrize(
    "error,status,error_type,code",
    [
        (NemoOverloaded("the queue is full"), 429, "rate_limit_error", "overloaded"),
        (NemoUnavailable("connection refused"), 502, "server_error", "upstream_error"),
        (NemoError("session failed"), 502, "server_error", "upstream_error"),
    ],
)
def test_nemo_failure_when_a_stream_opens_is_a_429_or_502(ask, fake_nemo, caplog, error, status, error_type, code):
    # Rien n'est encore parti : le statut de la réponse dit la panne, comme pour une réponse sans `stream`.
    fake_nemo.error = error
    caplog.set_level(logging.INFO, logger="dictee")
    r = ask(stream="true")
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()["error"]
    assert (body["type"], body["code"], body["param"]) == (error_type, code, None)
    assert str(error) not in body["message"]
    (line,) = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert line.endswith(f"status={status} task=-")


def test_nemo_failure_after_the_first_delta_is_an_error_event(ask, fake_nemo, caplog):
    # Le statut 200 est parti avec le premier delta : la panne ne peut plus que se dire dans le flux.
    fake_nemo.error = NemoOverloaded("the queue is full")
    fake_nemo.after = 1
    caplog.set_level(logging.INFO, logger="dictee")
    r = ask(stream="true")
    assert r.status_code == 200
    delta, event = sse_events(r)
    assert delta == {"type": "transcript.text.delta", "delta": "Bonjour à tous."}
    assert event["type"] == "error"
    assert (event["error"]["type"], event["error"]["code"]) == ("rate_limit_error", "overloaded")
    assert "queue" not in event["error"]["message"]
    (line,) = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert line.endswith("status=429 task=-")


UNEXPECTED_BODY = {
    "error": {
        "message": "The server failed to process the request.",
        "type": "server_error",
        "param": None,
        "code": None,
    }
}


@pytest.mark.parametrize(
    "stream,after",
    [("false", 0), ("true", 0), ("true", 1)],
    ids=["plain", "stream-at-open", "stream-after-the-first-delta"],
)
def test_unexpected_error_is_a_500_that_leaks_nothing(ask, fake_nemo, caplog, stream, after):
    # Un texte transcrit pourrait se retrouver dans le message d'une exception : il ne sort ni par la réponse, ni par
    # un journal, y compris celui d'uvicorn qui journalise le message d'une exception que Starlette relève.
    marker = "Magpie-marker-7f3a91"
    fake_nemo.error = RuntimeError(f"failed on {marker}")
    fake_nemo.chunks = [fake_nemo.error]  # sans `stream`, l'audio passe par morceaux
    fake_nemo.after = after
    caplog.set_level(logging.DEBUG)
    r = ask(stream=stream)
    if after:
        assert r.status_code == 200
        delta, event = sse_events(r)
        assert delta["type"] == "transcript.text.delta"
        assert event == {"type": "error", **UNEXPECTED_BODY}
    else:
        assert r.status_code == 500
        assert r.json() == UNEXPECTED_BODY
    assert marker not in r.text and marker not in caplog.text
    for record in caplog.records:
        assert marker not in record.getMessage()
        assert marker not in str(record.exc_info) and marker not in str(record.args)
    messages = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert "unexpected error class=RuntimeError" in messages
    assert re.search(r" status=500 task=\S+$", messages[-1])


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


def test_unreadable_audio_is_422_and_leaves_neither_task_nor_folder(transcriptions_client, service_headers):
    response = post(transcriptions_client, service_headers, b"not audio", model="dictee")
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_audio")
    assert tasks_of(transcriptions_client) == []
    assert not any(transcriptions_client.app.state.settings.tasks_dir.iterdir())


def test_empty_file_is_422(ask):
    assert ask(b"").status_code == 422


def test_audio_over_the_duration_limit_is_400(ask, fake_nemo, monkeypatch):
    monkeypatch.setattr(transcribe, "MAX_AUDIO_SECONDS", 1.0)
    r = ask()
    assert r.status_code == 400
    assert (r.json()["error"]["param"], r.json()["error"]["code"]) == ("file", "audio_too_long")
    assert fake_nemo.calls == []


class ListingNemo(FakeNemo):
    """Faux NeMo qui note, à chaque transcription, ce que le dossier du WAV contient."""

    def __init__(self, **options) -> None:
        super().__init__(**options)
        self.listings: list[list[str]] = []

    async def transcribe(self, wav, **options):
        self.listings.append(sorted(path.name for path in wav.parent.iterdir()))
        async for event in super().transcribe(wav, **options):
            yield event


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Le dossier temporaire du système, remplacé par un dossier du test : ce qu'il garde, dictee le garde.

    Python efface de lui-même un `TemporaryDirectory` que plus rien ne référence, en le signalant par un
    `ResourceWarning` : dictee doit effacer ses dossiers lui-même, sans attendre le ramasse-miettes, qui peut tarder
    et laisser des centaines de Mio sur le disque. Le test échoue si un dossier a été effacé de cette façon.
    """
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        yield root
        gc.collect()
    implicit = [str(warning.message) for warning in caught if "Implicitly cleaning up" in str(warning.message)]
    assert implicit == []


def test_each_upload_lives_alone_in_a_directory_removed_afterwards(
    settings, fake_llm, service_headers, tone_mp3, scratch, monkeypatch
):
    seen = []
    convert = transcribe.decode_wav16k

    async def spy(src, dst, **kwargs):
        # Au début du décodage le dossier ne contient que l'envoi. Le WAV d'un flux s'écrit à côté de lui, celui d'une
        # tâche dans le dossier de la tâche.
        assert list(src.parent.iterdir()) == [src] and src.parent.parent == scratch
        seen.append(dst)
        return await convert(src, dst, **kwargs)

    monkeypatch.setattr(transcribe, "decode_wav16k", spy)
    nemo = ListingNemo(finals=FINALS)
    with TestClient(app_with(settings, nemo, fake_llm, max_upload_mb=1)) as client:
        assert post(client, service_headers, tone_mp3).status_code == 200
        assert post(client, service_headers, b"this is not audio").status_code == 422
        assert post(client, service_headers, tone_mp3, stream="true").status_code == 200
        assert post(client, service_headers, bytes(2 * MIB)).status_code == 413
    assert len(seen) == 3
    assert [wav.parent.parent for wav in seen] == [settings.tasks_dir, settings.tasks_dir, scratch]
    # Quand NeMo lit le WAV d'un flux, l'envoi a déjà disparu : le dossier ne garde que lui, jusqu'à la fin de la
    # réponse. Une tâche lit le sien par tranches, sans le nommer à NeMo.
    assert nemo.listings == [[seen[2].name]]
    assert not any(wav.exists() for wav in seen)
    assert list(scratch.iterdir()) == [] and list(settings.tasks_dir.iterdir()) == []


@pytest.mark.anyio
async def test_the_wav_of_a_task_stays_in_its_folder_until_the_task_ends(
    settings, fake_llm, service_headers, tmp_path, scratch
):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    app = app_with(settings, nemo, fake_llm)
    audio = make_audio(tmp_path, "a.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee"}, audio)
    headers = streaming_headers(service_headers, body, content_type)
    hung_up = anyio.Event()
    async with app_running(app), anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, headers, body, hung_up)
        await wait_until(lambda: nemo.calls)
        # Le faux NeMo attend : la tâche est en cours, son WAV est dans son dossier, et l'envoi a déjà disparu.
        (directory,) = settings.tasks_dir.iterdir()
        assert [path.name for path in directory.iterdir()] == ["audio.wav"]
        assert list(scratch.iterdir()) == []
        release.set()
        await wait_until(lambda: not list(settings.tasks_dir.iterdir()))
        hung_up.set()


@pytest.mark.anyio
async def test_the_decoded_wav_of_a_stream_stays_on_the_disk_until_the_recognition_ends(
    settings, fake_llm, service_headers, tmp_path, scratch
):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    app = app_with(settings, nemo, fake_llm)
    audio = make_audio(tmp_path, "a.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee", "stream": "true"}, audio)
    headers = streaming_headers(service_headers, body, content_type)
    hung_up = anyio.Event()
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, headers, body, hung_up)
        await wait_until(lambda: nemo.calls)
        # Le faux NeMo attend : la reconnaissance est en cours, le WAV qu'il a lu est encore là.
        (directory,) = scratch.iterdir()
        assert [path.name for path in directory.iterdir()] == ["audio.wav"]
        release.set()
        await wait_until(lambda: nemo.sessions_closed == 1)
        await wait_until(lambda: not list(scratch.iterdir()))
        hung_up.set()


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
    fake_nemo.chunks = [error]  # sans `stream`, l'audio passe par morceaux
    r = ask()
    assert r.status_code == status
    body = r.json()["error"]
    assert (body["type"], body["code"], body["param"]) == (error_type, code, None)
    assert str(error) not in body["message"]


def test_silence_gives_empty_text(openai_client, ask, fake_nemo, tone_mp3):
    fake_nemo.finals = ()
    fake_nemo.chunks = [Final("", ())]  # sans `stream`, hors réunion, l'audio passe par morceaux
    assert ask().json() == {"text": ""}
    assert ask(model="dictee-clean").json() == {"text": ""}
    r = openai_client.audio.transcriptions.create(
        model="whisper-1", file=("a.mp3", tone_mp3), response_format="diarized_json"
    )
    assert (r.text, r.segments) == ("", [])
    assert sse_events(ask(model="dictee-diarize", stream="true")) == [{"type": "transcript.text.done", "text": ""}]


def test_silence_reported_as_a_blank_final_gives_empty_text(ask, fake_nemo):
    # NeMo rend `Final("", ())` pour un audio silencieux ou de moins d'une seconde.
    fake_nemo.finals = (Final("", ()),)
    r = ask(response_format="diarized_json")
    assert r.status_code == 200 and r.json()["segments"] == [] and r.json()["text"] == ""


def test_logs_never_contain_the_text(caplog, transcriptions_client, fake_llm, db, tone_mp3):
    user = db.upsert_user("sub-1", "ana@example.org", "Ana")
    db.save_settings(user, glossary="Nemotron")
    headers = {"Authorization": f"Bearer {db.create_key(user, 'Android')}"}
    caplog.set_level(logging.INFO)
    for model, fmt in [("dictee-clean", "json"), ("dictee-diarize", "srt"), ("whisper-1", "json")]:
        r = post(transcriptions_client, headers, tone_mp3, model=model, response_format=fmt, prompt="Sortformer")
        assert r.status_code == 200
    lines = request_lines(caplog)
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
    lines = request_lines(caplog)
    start = r"user=default route=transcriptions "
    # Une tâche nomme son identifiant ; ses temps de NeMo et du LLM sont sur la ligne de la tâche, pas ici.
    of_a_task = r"nemo_ms=- llm_ms=- decision=- mismatch=- status=200 task=trn_[0-9A-HJKMNP-TV-Z]{26}"
    no_task = " task=-"
    patterns = [
        start + r"model=whisper-1 mode=raw audio_s=3\.\d " + of_a_task,
        start + r"model=dictee-clean mode=clean audio_s=3\.\d " + of_a_task,
        start
        + r"model=dictee-diarize mode=diarize audio_s=3\.\d nemo_ms=\d+ llm_ms=- decision=- mismatch=0 status=200"
        + no_task,
        start + r"model=dictee mode=raw audio_s=- nemo_ms=- llm_ms=- decision=- mismatch=- status=422" + no_task,
        r"user=- route=transcriptions model=- mode=- audio_s=- nemo_ms=- llm_ms=- decision=- mismatch=- status=401"
        + no_task,
    ]
    assert len(lines) == len(patterns)
    for line, pattern in zip(lines, patterns, strict=True):
        assert re.fullmatch(pattern, line), line


def mismatch_of(caplog) -> str:
    return re.search(r" mismatch=(\S+) ", request_line(caplog)).group(1)


@pytest.mark.parametrize(
    "finals,expected",
    [
        (PARTIAL_FINALS, "0"),
        (FINALS, "0"),
        ((Partial("Bonjour"), Final("Salut tout le monde.", ()), Partial("Sa"), FINALS[1]), "1"),
        ((Partial("Bonjour"), Final("Salut tout le monde.", ()), Partial("Sal"), Final("Au revoir.", ())), "2"),
        ((Partial("Bonjour"), Final("", ())), "1"),
    ],
    ids=["partials-extended", "no-partials", "one-item", "two-items", "empty-final"],
)
def test_log_line_counts_the_items_whose_final_text_does_not_extend_what_was_sent(
    caplog, ask, fake_nemo, finals, expected
):
    fake_nemo.finals = finals
    caplog.set_level(logging.INFO, logger="dictee")
    assert ask(stream="true").status_code == 200
    assert mismatch_of(caplog) == expected


@pytest.mark.parametrize(
    "fields",
    [{"model": "dictee"}, {"model": "dictee-diarize"}, {"model": "dictee-clean", "stream": "true"}],
    ids=["no-stream", "no-stream-with-speakers", "stream-with-clean"],
)
def test_log_line_has_no_mismatch_count_when_nothing_was_relayed(caplog, ask, fake_nemo, fields):
    fake_nemo.finals = (Partial("Bonjour"), Final("Salut tout le monde.", ()))
    caplog.set_level(logging.INFO, logger="dictee")
    assert ask(**fields).status_code == 200
    assert mismatch_of(caplog) == "-"


def test_log_line_counts_a_mismatch_even_when_the_stream_ends_in_an_error(caplog, ask, fake_nemo):
    fake_nemo.finals = (Partial("Bonjour"), Final("Salut tout le monde.", ()), Partial("Sa"), FINALS[1])
    fake_nemo.error = NemoUnavailable("connection refused")
    fake_nemo.after = 2
    caplog.set_level(logging.INFO, logger="dictee")
    ask(stream="true")
    assert mismatch_of(caplog) == "1"


def test_log_line_never_carries_the_text_of_a_mismatch(caplog, ask, fake_nemo):
    fake_nemo.finals = (Partial("Bonjour"), Final("Salut tout le monde.", ()))
    caplog.set_level(logging.DEBUG)
    ask(stream="true")
    assert "Bonjour" not in caplog.text and "Salut" not in caplog.text


def test_model_name_cannot_forge_a_log_line(caplog, ask):
    caplog.set_level(logging.INFO, logger="dictee")
    ask(model="x\nuser=1 status=200 " + "y" * 200)
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert len(request_lines(caplog)) == 1
    assert all("\n" not in line and len(line) < 250 for line in lines)


def test_audio_limit_is_two_hours():
    assert transcribe.MAX_AUDIO_SECONDS == 7200.0


@pytest.mark.anyio
async def test_short_request_passes_during_a_long_one(settings, fake_llm, service_headers, tmp_path):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    long_audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    short_audio = make_audio(tmp_path, "short.mp3", seconds=0.5).read_bytes()
    answers = {}
    async with app_running(app), asgi_client(app) as client:

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
    answers = []
    async with app_running(app), asgi_client(app) as client:

        async def send(name):
            # Deux audios différents : le second, s'il était identique, rejoindrait la tâche du premier.
            files = upload(tmp_path, name, seconds=3.0)
            answers.append(await client.post(URL, headers=service_headers, files=files, data={"model": "dictee"}))

        async with anyio.create_task_group() as tg:
            tg.start_soon(send, "first.wav")
            await wait_until(lambda: len(nemo.calls) == 1)
            tg.start_soon(send, "second.wav")
            await anyio.sleep(0.3)
            assert len(nemo.calls) == 1  # la seconde attend la place de la première
            release.set()
    assert [a.status_code for a in answers] == [200, 200] and len(nemo.calls) == 2


async def call_app(
    app,
    headers: dict[str, str],
    body: bytes | Path,
    hung_up: anyio.Event | None = None,
    *,
    complete: bool = True,
    sent: list | None = None,
    on_send=None,
    hang_up_after_upload: bool = False,
) -> int | None:
    """Appelle l'app sans client HTTP et rend le statut de sa réponse, ou `None` si elle n'a pas commencé.

    `body` est un corps multipart, dont `headers` donne le type, ou le chemin d'un audio, que la fonction envoie seul
    avec `model=dictee`. Après le corps, `receive` attend que `hung_up` soit posé, puis dit `http.disconnect` ; avec
    `hang_up_after_upload`, il le dit tout de suite. Les messages envoyés s'ajoutent à `sent`, si elle est donnée, au
    fur et à mesure ; `on_send` est attendue avec chacun d'eux.
    """
    if isinstance(body, Path):
        body, content_type = multipart_body({"model": "dictee"}, body.read_bytes())
        headers = streaming_headers(headers, body, content_type)
    hung_up = anyio.Event() if hung_up is None else hung_up
    if hang_up_after_upload:
        hung_up.set()
    messages = [{"type": "http.request", "body": body, "more_body": not complete}]
    sent = [] if sent is None else sent

    async def receive():
        if messages:
            return messages.pop(0)
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if on_send is not None:
            await on_send(message)

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
    return next((message["status"] for message in sent if message["type"] == "http.response.start"), None)


@pytest.mark.anyio
async def test_client_hanging_up_closes_the_session_and_frees_the_slot(
    caplog, settings, fake_llm, service_headers, tmp_path, scratch
):
    # Le faux NeMo ne rend jamais la main : la requête attend dans la session, comme une transcription longue. Sans
    # `stream`, la tâche, elle, continue après le départ du client (voir les tests des tâches).
    nemo = FakeNemo(finals=FINALS, release=anyio.Event())
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee", "stream": "true"}, audio)
    headers = {**service_headers, "content-type": content_type, "content-length": str(len(body))}
    hung_up = anyio.Event()
    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_app, app, headers, body, hung_up)
        await wait_until(lambda: nemo.calls)
        hung_up.set()
        await wait_until(lambda: nemo.sessions_closed == 1)
    await assert_lane_is_free(app)
    await wait_until(lambda: not list(scratch.iterdir()))
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert len(lines) == 1 and lines[0].endswith("status=499 task=-")


@pytest.mark.anyio
async def test_client_hanging_up_during_the_upload_is_not_an_error(settings, fake_nemo, fake_llm, service_headers):
    app = app_with(settings, fake_nemo, fake_llm)
    body, content_type = multipart_body({"model": "dictee"}, bytes(1000))
    hung_up = anyio.Event()
    hung_up.set()
    headers = {**service_headers, "content-type": content_type}
    sent: list = []
    with anyio.fail_after(2):
        await call_app(app, headers, body[:200], hung_up, complete=False, sent=sent)
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [499]
    assert fake_nemo.calls == []


def streaming_headers(service_headers, body: bytes, content_type: str) -> dict[str, str]:
    return {**service_headers, "content-type": content_type, "content-length": str(len(body))}


FIRST_DELTA_FROM = [FINALS, (Partial("Bonjour"), *FINALS)]
FIRST_DELTA_FROM_IDS = ["a-final", "a-partial"]


@pytest.mark.anyio
@pytest.mark.parametrize("finals", FIRST_DELTA_FROM, ids=FIRST_DELTA_FROM_IDS)
async def test_client_hanging_up_mid_stream_closes_the_session_and_frees_the_slot(
    finals, caplog, settings, fake_llm, service_headers, tmp_path, scratch
):
    # Le premier delta est parti : la réponse est ouverte, et c'est Starlette qui voit le client raccrocher.
    nemo = FakeNemo(finals=finals, release=anyio.Event(), after=1)
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee", "stream": "true"}, audio)
    headers = streaming_headers(service_headers, body, content_type)
    hung_up = anyio.Event()
    sent: list = []
    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(call_app, app, headers, body, hung_up, sent=sent))
        await wait_until(lambda: any(b"Bonjour" in message.get("body", b"") for message in sent))
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
        hung_up.set()
        await wait_until(lambda: nemo.sessions_closed == 1)
    await assert_lane_is_free(app)
    await wait_until(lambda: not list(scratch.iterdir()))
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert len(lines) == 1 and lines[0].endswith("status=499 task=-")


@pytest.mark.anyio
@pytest.mark.parametrize("finals", FIRST_DELTA_FROM, ids=FIRST_DELTA_FROM_IDS)
async def test_client_hanging_up_before_the_stream_starts_still_ends_the_recognition(
    finals, caplog, settings, fake_llm, service_headers, tmp_path, scratch
):
    # Le client raccroche entre la création de la réponse et son premier octet : le générateur ne démarre jamais et ses
    # `finally` ne courent pas. Seul le filet de la réponse ferme alors la session de NeMo et libère la place.
    nemo = FakeNemo(finals=finals, release=anyio.Event(), after=1)
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0)
    audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    body, content_type = multipart_body({"model": "dictee", "stream": "true"}, audio)
    headers = streaming_headers(service_headers, body, content_type)
    hung_up = anyio.Event()
    sent: list = []

    async def hang_up_at_the_start(message):
        if message["type"] == "http.response.start":
            hung_up.set()
            await anyio.sleep_forever()  # annulée ici, avant que le flux ait rien envoyé

    caplog.set_level(logging.INFO, logger="dictee")
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(call_app, app, headers, body, hung_up, sent=sent, on_send=hang_up_at_the_start))
        await wait_until(lambda: nemo.sessions_closed == 1)
    assert [message["type"] for message in sent] == ["http.response.start"]
    await assert_lane_is_free(app)
    await wait_until(lambda: not list(scratch.iterdir()))
    lines = [record.getMessage() for record in caplog.records if record.name == "dictee"]
    assert len(lines) == 1 and lines[0].endswith("status=499 task=-")


@pytest.mark.anyio
@pytest.mark.parametrize("stream", ["false", "true"])
async def test_third_concurrent_long_request_is_429_while_decoding_and_decodes_no_further(
    stream, caplog, settings, fake_llm, service_headers, tmp_path, monkeypatch
):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    # Une tâche longue qui tourne et une seule qui attend : la troisième demande longue, tâche ou flux, est de trop.
    app = app_with(settings, nemo, fake_llm, long_audio_seconds=1.0, max_queued_long=1)
    decodes = []
    convert = transcribe.decode_wav16k

    async def spy(src, dst, **kwargs):
        decode = {"max_seconds": kwargs["max_seconds"], "long_seconds": kwargs["long_seconds"], "past": None}
        decodes.append(decode)
        reserve = kwargs["on_long"]

        def on_long():
            # Le rappel est celui de la route : il réserve la place, ou refuse, et le décodage s'arrête alors.
            try:
                reserve()
            except ApiError:
                decode["past"] = False
                raise
            decode["past"] = True

        return await convert(src, dst, **{**kwargs, "on_long": on_long})

    monkeypatch.setattr(transcribe, "decode_wav16k", spy)
    caplog.set_level(logging.INFO, logger="dictee")
    answers = {}
    async with app_running(app), asgi_client(app) as client:

        async def send(name, **fields):
            # Trois audios différents : un audio identique rejoindrait la tâche d'un autre au lieu de prendre une place.
            files = upload(tmp_path, f"{name}.wav", seconds=3.0)
            data = {"model": "dictee", **fields}
            answers[name] = await client.post(URL, headers=service_headers, files=files, data=data)

        async with anyio.create_task_group() as tg:
            tg.start_soon(send, "runner")
            await wait_until(lambda: len(nemo.calls) == 1)
            tg.start_soon(send, "waiter")
            await wait_until(lambda: app.state.lane.full)
            with anyio.fail_after(2):  # le runner tient la place : un refus ne peut pas attendre
                await send("third", stream=stream)
            assert "runner" not in answers and "waiter" not in answers
            release.set()
    third = answers["third"]
    assert third.status_code == 429
    assert third.headers["content-type"].startswith("application/json")
    assert third.json() == {
        "error": {
            "message": "Another long transcription is already waiting, try again later.",
            "type": "rate_limit_error",
            "param": None,
            "code": "long_audio_busy",
        }
    }
    assert [answers["runner"].status_code, answers["waiter"].status_code] == [200, 200]
    assert len(nemo.calls) == 2  # le refusé n'a jamais atteint NeMo
    # Deux décodages seulement passent le seuil : le troisième est refusé à ce moment, et n'est jamais allé plus loin.
    assert [decode["past"] for decode in decodes] == [True, True, False]
    assert {decode["long_seconds"] for decode in decodes} == {1.0}
    assert {decode["max_seconds"] for decode in decodes} == {transcribe.MAX_AUDIO_SECONDS}
    statuses = [re.search(r" status=(\d+) ", line).group(1) for line in request_lines(caplog)]
    assert sorted(statuses) == ["200", "200", "429"]
    await assert_lane_is_free(app)


# Cas de sortie d'une requête longue : options du faux NeMo, champs du formulaire, plafonds remplacés, statut attendu.
# Sans `stream`, l'audio passe par morceaux : la panne de NeMo s'y donne par `chunks`.
EXITS = {
    "done": ({}, {}, {}, 200),
    "stream": ({}, {"stream": "true"}, {}, 200),
    "nemo-fails": ({"chunks": [NemoUnavailable("down")]}, {}, {}, 502),
    "stream-nemo-fails": ({"error": NemoUnavailable("down")}, {"stream": "true"}, {}, 502),
    "too-long": ({}, {}, {"MAX_AUDIO_SECONDS": 2.9}, 400),
}


@pytest.mark.anyio
@pytest.mark.parametrize(("nemo_options", "fields", "limits", "status"), EXITS.values(), ids=EXITS.keys())
async def test_the_place_is_given_back_whatever_the_exit(
    nemo_options, fields, limits, status, settings, fake_llm, service_headers, tmp_path, monkeypatch
):
    # Lectures de 8 Kio : l'audio de 3 s passe le seuil (1 s) bien avant les plafonds de 2,9 s des cas « too-long ».
    monkeypatch.setattr(audio_module, "DECODE_READ_SIZE", 8192)
    for name, value in limits.items():
        monkeypatch.setattr(transcribe, name, value)
    app = app_with(settings, FakeNemo(finals=FINALS, **nemo_options), fake_llm, long_audio_seconds=1.0)
    admitted = []
    convert = transcribe.decode_wav16k

    async def spy(src, dst, **kwargs):
        reserve = kwargs["on_long"]

        def on_long():
            reserve()
            admitted.append(True)  # le décodage a passé le seuil et réservé une place avant de sortir

        return await convert(src, dst, **{**kwargs, "on_long": on_long})

    monkeypatch.setattr(transcribe, "decode_wav16k", spy)
    audio = make_audio(tmp_path, "long.mp3", seconds=3.0).read_bytes()
    async with app_running(app), asgi_client(app) as client:
        answer = await client.post(
            URL, headers=service_headers, files={"file": ("a.mp3", audio)}, data={"model": "dictee", **fields}
        )
        assert answer.status_code == status
        assert admitted == [True]
        await assert_lane_is_free(app)


@pytest.fixture(scope="module")
def long_mp3(tmp_path_factory):
    """Deux cents secondes de sinus en MP3 : plus que les plafonds remplacés par les tests de message."""
    return make_audio(tmp_path_factory.mktemp("long"), "long.mp3", seconds=200.0).read_bytes()


@pytest.mark.parametrize("fields", [{}, {"model": "dictee-diarize"}], ids=["transcription", "speakers"])
def test_the_duration_limit_message_follows_the_constant_in_every_mode(ask, long_mp3, monkeypatch, fields):
    monkeypatch.setattr(transcribe, "MAX_AUDIO_SECONDS", 120.0)
    r = ask(long_mp3, **fields)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "The audio is longer than 2 minutes."
    assert r.json()["error"]["code"] == "audio_too_long"


def test_the_duration_limit_message_names_two_hours_in_minutes(ask, monkeypatch):
    async def too_long(*args, **kwargs):
        raise audio_module.AudioTooLong("too long")

    monkeypatch.setattr(transcribe, "decode_wav16k", too_long)
    r = ask()
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "The audio is longer than 120 minutes."


def test_the_decoding_is_bounded_by_the_same_limit_in_every_mode(
    settings, fake_nemo, fake_llm, service_headers, tone_mp3, monkeypatch
):
    seen = []
    convert = transcribe.decode_wav16k

    async def spy(src, dst, **kwargs):
        seen.append((kwargs["max_seconds"], kwargs["long_seconds"]))
        return await convert(src, dst, **kwargs)

    monkeypatch.setattr(transcribe, "decode_wav16k", spy)
    with TestClient(app_with(settings, fake_nemo, fake_llm, long_audio_seconds=120.0)) as client:
        for fields in (
            {},
            {"model": "dictee-clean"},
            {"model": "dictee-diarize"},
            {"response_format": "diarized_json"},
        ):
            assert post(client, service_headers, tone_mp3, **fields).status_code == 200
    # Le dernier envoi, en réunion comme le troisième, rejoint sa tâche : il n'a rien à décoder.
    assert seen == [(transcribe.MAX_AUDIO_SECONDS, 120.0)] * 3


# Tâches : sans `stream`, une transcription de fichier passe par une tâche enregistrée, synchrone ou en arrière-plan.


def test_a_synchronous_request_leaves_a_finished_task(ask, transcriptions_client):
    assert ask().json() == {"text": "Bonjour à tous. Salut."}
    [task] = tasks_of(transcriptions_client)
    assert task.status is TaskStatus.COMPLETED


def test_background_gives_the_task_object_at_once_then_its_result(
    ask, transcriptions_client, fake_nemo, service_headers
):
    fake_nemo.release = anyio.Event()
    body = ask(background="true").json()
    assert body["object"] == "audio.transcription.task" and body["status"] in ("queued", "in_progress")
    assert body["output"] is None
    release(transcriptions_client, fake_nemo)
    final = poll_task(transcriptions_client, service_headers, body["id"])
    assert (final["status"], final["output"]) == ("completed", {"text": "Bonjour à tous. Salut."})


@pytest.mark.parametrize("field_order", ["before_file", "after_file"])
def test_stream_with_background_is_refused_before_any_task(
    transcriptions_client, service_headers, tone_mp3, field_order
):
    response = post_fields(
        transcriptions_client, service_headers, tone_mp3, {"stream": "true", "background": "true"}, field_order
    )
    assert (response.status_code, response.json()["error"]["code"]) == (400, "unsupported_combination")
    assert response.json()["error"]["param"] == "background" and tasks_of(transcriptions_client) == []


def test_background_accepts_only_true_or_false(ask):
    assert ask(background="yes").json()["error"]["param"] == "background"


def test_an_identical_upload_joins_the_running_task(ask, transcriptions_client, fake_nemo):
    fake_nemo.release = anyio.Event()
    first, second = ask(background="true").json(), ask(background="true").json()
    assert first["id"] == second["id"]
    release(transcriptions_client, fake_nemo)
    assert len([call for call in fake_nemo.calls if "chunk" in call]) == 1


def test_an_identical_upload_gets_the_finished_result_without_nemo(ask, fake_nemo):
    ask()
    calls = len(fake_nemo.calls)
    assert ask().json() == {"text": "Bonjour à tous. Salut."}
    assert len(fake_nemo.calls) == calls


def test_an_identical_upload_after_a_failure_makes_a_new_task(ask, transcriptions_client, fake_nemo, no_retry_delays):
    fake_nemo.chunks = [NemoError("bad")]
    assert ask().status_code == 502
    fake_nemo.chunks = [Final("Bonjour.", ())]
    assert ask().json() == {"text": "Bonjour."}
    assert [task.status for task in tasks_of(transcriptions_client)] == [TaskStatus.FAILED, TaskStatus.COMPLETED]


@pytest.fixture
def failing_removal(monkeypatch):
    """Fait échouer l'effacement des dossiers temporaires, comme un `rmtree` refusé ; les dossiers restent à effacer.

    L'échec passe par le `onexc` de `tempfile`, qui décide seul de lever ou non. Les dossiers laissés sont effacés à la
    fin du test.
    """
    left: list[str] = []

    def rmtree(path, onexc):
        left.append(path)
        # Comme `shutil.rmtree`, l'appel de `onexc` se fait pendant que l'exception est levée : `tempfile` la relance.
        try:
            raise OSError(errno.EBUSY, "Device or resource busy", path)
        except OSError as error:
            onexc(os.rmdir, path, error)

    monkeypatch.setattr(tempfile._shutil, "rmtree", rmtree)
    yield
    monkeypatch.undo()
    for path in left:
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.parametrize("stream", ["false", "true"])
def test_a_temporary_folder_that_cannot_be_removed_does_not_change_the_answer(ask, caplog, failing_removal, stream):
    caplog.set_level(logging.DEBUG)
    response = ask(stream=stream)
    assert response.status_code == 200 and "Bonjour à tous." in response.text
    assert not [record for record in caplog.records if "dictee-" in record.getMessage() or record.exc_info]


def test_another_glossary_makes_another_task(transcriptions_client, db, tone_mp3):
    headers, owner = personal_headers(db, "alice@example.test")
    post(transcriptions_client, headers, tone_mp3, model="dictee")
    db.save_settings(owner, glossary="Sortformer")
    post(transcriptions_client, headers, tone_mp3, model="dictee")
    assert len(tasks_of(transcriptions_client)) == 2


def test_each_request_gets_its_own_format_from_the_same_task(ask, transcriptions_client):
    assert ask(response_format="json").json() == {"text": "Bonjour à tous. Salut."}
    assert ask(response_format="srt").text.startswith("1\n00:00:00,000 --> ")
    assert len(tasks_of(transcriptions_client)) == 1


@pytest.mark.anyio
async def test_a_client_hanging_up_leaves_the_task_running_to_its_end(
    settings, fake_nemo, fake_llm, service_headers, tmp_path
):
    fake_nemo.release = anyio.Event()
    app = app_with(settings, fake_nemo, fake_llm)
    async with app_running(app):
        status = await call_app(app, service_headers, tone_wav(tmp_path), hang_up_after_upload=True)
        assert status == 499
        [task] = app.state.store.unfinished()
        fake_nemo.release.set()
        assert (await app.state.runner.wait(task.id)).status is TaskStatus.COMPLETED


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NemoOverloaded("full"), 429, "overloaded"),
        (NemoUnavailable("down"), 502, "upstream_error"),
        (NemoError("bad"), 502, "upstream_error"),
    ],
)
def test_a_failed_task_gives_the_error_of_a_synchronous_request(ask, fake_nemo, no_retry_delays, error, status, code):
    fake_nemo.chunks = [error]
    response = ask()
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)


def test_audio_over_two_hours_is_refused_before_any_task(ask, transcriptions_client, monkeypatch):
    monkeypatch.setattr(transcribe, "MAX_AUDIO_SECONDS", 1.0)
    response = ask()
    assert (response.status_code, response.json()["error"]["code"]) == (400, "audio_too_long")
    tasks_dir = transcriptions_client.app.state.settings.tasks_dir
    assert tasks_of(transcriptions_client) == [] and not any(tasks_dir.iterdir())


@pytest.mark.anyio
async def test_the_fifth_long_upload_is_429_while_decoding_and_leaves_nothing(
    settings, fake_nemo, fake_llm, service_headers, tmp_path
):
    fake_nemo.release = anyio.Event()
    app = app_with(settings, fake_nemo, fake_llm, long_audio_seconds=1.0)
    async with app_running(app), asgi_client(app) as client:
        for index in range(4):
            response = await client.post(
                "/v1/audio/transcriptions",
                headers=service_headers,
                files=upload(tmp_path, f"long{index}.wav", seconds=3.0),
                data={"model": "dictee", "background": "true"},
            )
            assert response.status_code == 200
        refused = await client.post(
            "/v1/audio/transcriptions",
            headers=service_headers,
            files=upload(tmp_path, "long4.wav", seconds=3.0),
            data={"model": "dictee", "background": "true"},
        )
        assert (refused.status_code, refused.json()["error"]["code"]) == (429, "long_audio_busy")
        assert len(app.state.store.unfinished()) == 4 and len(list(settings.tasks_dir.iterdir())) == 4
        fake_nemo.release.set()


def test_a_stream_makes_no_task(ask, transcriptions_client):
    assert ask(stream="true").status_code == 200
    assert tasks_of(transcriptions_client) == []


def test_the_upload_digest_is_the_sha256_of_the_file_bytes(transcriptions_client, service_headers, tone_mp3):
    post(transcriptions_client, service_headers, tone_mp3, model="dictee")
    [task] = tasks_of(transcriptions_client)
    assert task.upload_sha256 == hashlib.sha256(tone_mp3).hexdigest()


def test_the_request_line_names_its_task(caplog, ask, transcriptions_client):
    caplog.set_level(logging.INFO, logger="dictee")
    ask()
    [task] = tasks_of(transcriptions_client)
    assert request_line(caplog).endswith(f"status=200 task={task.id}")


def test_an_audio_decoded_to_zero_seconds_is_refused_like_an_unreadable_one(ask, transcriptions_client, monkeypatch):
    # La durée sert de diviseur au traitement, et un morceau vide n'a rien à transcrire : la tâche n'est pas créée.
    unreadable = ask(b"this is not audio")

    async def nothing(src, dst, **kwargs):
        kwargs["on_long"]()  # une place réservée pendant le décodage doit être rendue avec le refus
        dst.write_bytes(b"")
        return 0.0

    monkeypatch.setattr(transcribe, "decode_wav16k", nothing)
    refused = ask()
    assert refused.status_code == 422
    assert (refused.status_code, refused.json()) == (unreadable.status_code, unreadable.json())
    tasks_dir = transcriptions_client.app.state.settings.tasks_dir
    assert tasks_of(transcriptions_client) == [] and not any(tasks_dir.iterdir())
    take_every_place(transcriptions_client.app)
