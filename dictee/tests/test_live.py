"""Dictée en direct : `/v1/realtime?intent=transcription`, au format du Realtime d'OpenAI en mode transcription."""

import base64
import json
import logging
import time

import pytest
from conftest import personal_headers
from openai import OpenAI

from dictee import live
from dictee.nemo import NemoError, NemoOverloaded
from dictee.types import Final, Partial

DELTA = "conversation.item.input_audio_transcription.delta"
COMPLETED = "conversation.item.input_audio_transcription.completed"


def wait_for(condition, timeout: float = 2.0) -> None:
    """Attend que `condition()` devienne vraie, `timeout` secondes au plus : l'app tourne dans un autre fil."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition did not come true in time"
        time.sleep(0.01)


def open_live(client, headers, query="intent=transcription"):
    return client.websocket_connect(f"/v1/realtime?{query}", headers=headers)


def pcm_b64(seconds: float, rate: int) -> str:
    """`seconds` secondes de silence PCM16 mono à `rate` Hz, en base64."""
    return base64.b64encode(bytes(2 * round(seconds * rate))).decode()


def update(ws, **audio_input) -> None:
    """Envoie un `session.update` de type `transcription` avec ces champs de `audio.input`."""
    ws.send_json({"type": "session.update", "session": {"type": "transcription", "audio": {"input": audio_input}}})


def until(ws, kind: str) -> list[dict]:
    """Lit les événements jusqu'au premier du type `kind` et les rend tous ; une `error` inattendue fait échouer."""
    events = [ws.receive_json()]
    while events[-1]["type"] != kind:
        assert events[-1]["type"] != "error", events[-1]
        events.append(ws.receive_json())
    return events


def lines(caplog) -> list[str]:
    return [line for record in caplog.records if (line := record.getMessage()).startswith("route=realtime ")]


def test_without_the_transcription_intent_an_error_comes_then_the_socket_closes(live_client, service_headers):
    with open_live(live_client, service_headers, query="model=gpt-realtime") as ws:
        assert ws.receive_json()["error"]["code"] == "unsupported_intent"
        assert ws.receive()["code"] == 1008


def test_a_conversation_model_is_refused(live_client, service_headers):
    with open_live(live_client, service_headers, query="intent=transcription&model=gpt-realtime") as ws:
        assert ws.receive_json()["error"]["code"] == "unsupported_model"


def test_an_unknown_key_gets_an_error_and_the_socket_closes(live_client):
    with open_live(live_client, {"Authorization": "Bearer nope"}) as ws:
        assert ws.receive_json()["error"]["code"] == "invalid_api_key"
        assert ws.receive()["code"] == 1008


def test_the_session_in_force_reaches_nemo(live_client, fake_nemo, db):
    headers, owner = personal_headers(db, "alice@example.test")
    db.save_settings(owner, glossary="Nemotron")
    with open_live(live_client, headers) as ws:
        until(ws, "session.created")
        update(
            ws,
            format={"type": "audio/pcm", "rate": 16000},
            transcription={"languages": ["en"], "keywords": ["Kubernetes", "Nemotron"], "prompt": "x", "delay": "low"},
        )
        updated = until(ws, "session.updated")[-1]["session"]["audio"]["input"]
        assert (updated["format"]["rate"], updated["transcription"]["language"]) == (16000, "en")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 16000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        until(ws, COMPLETED)
    options = fake_nemo.live_sessions[0].options
    assert (options["sample_rate"], options["language"]) == (16000, "en-US")
    assert options["phrases"] == ["Nemotron", "Kubernetes"] and "prompt" not in options


def test_appended_audio_reaches_nemo_decoded(live_client, service_headers, fake_nemo):
    pcm = bytes(range(256)) * 8
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm[:1000]).decode()})
        ws.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm[1000:]).decode()})
        ws.send_json({"type": "input_audio_buffer.commit"})
        until(ws, COMPLETED)
    assert bytes(fake_nemo.live_sessions[0].audio) == pcm


def test_a_commit_gives_committed_with_its_item_then_completed(live_client, service_headers):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(1.4, 24000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        events = until(ws, COMPLETED)
    (committed,) = [event for event in events if event["type"] == "input_audio_buffer.committed"]
    completed = events[-1]
    assert (committed["item_id"], committed["previous_item_id"]) == ("item_1", None)
    assert (completed["item_id"], completed["content_index"], completed["transcript"]) == (
        "item_1",
        0,
        "Bonjour à tous.",
    )
    assert completed["usage"] == {"type": "duration", "seconds": 1.4}


def test_the_final_texts_of_a_turn_reach_the_client_as_one_completed_text(live_client, service_headers, fake_nemo):
    # NeMo peut rendre plusieurs textes finals pour un même commit, dont un vide : aucun ne se perd.
    fake_nemo.live_turns = [
        [Partial("Bonjour"), Final("Bonjour à tous.", ()), Final("", ()), Final("On commence.", ())],
        [Partial("Salut"), Final("Salut.", ())],
    ]
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        events = []
        for _ in range(2):
            ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
            ws.send_json({"type": "input_audio_buffer.commit"})
            events += until(ws, COMPLETED)
    kinds = [(event["type"], event.get("item_id")) for event in events if event["type"] != DELTA]
    assert kinds == [
        ("input_audio_buffer.committed", "item_1"),
        (COMPLETED, "item_1"),
        ("input_audio_buffer.committed", "item_2"),
        (COMPLETED, "item_2"),
    ]
    transcripts = [event["transcript"] for event in events if event["type"] == COMPLETED]
    assert transcripts == ["Bonjour à tous. On commence.", "Salut."]


def test_partials_carry_the_item_that_committed_announces_later(live_client, service_headers):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        delta = until(ws, DELTA)[-1]
        ws.send_json({"type": "input_audio_buffer.commit"})
        committed = until(ws, "input_audio_buffer.committed")[-1]
    assert (delta["item_id"], delta["content_index"], delta["delta"]) == ("item_1", 0, "Bonjour")
    assert committed["item_id"] == "item_1"


def test_each_turn_gets_its_own_item_and_links_to_the_previous_one(live_client, service_headers):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        until(ws, COMPLETED)
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        events = until(ws, COMPLETED)
    (committed,) = [event for event in events if event["type"] == "input_audio_buffer.committed"]
    assert (committed["item_id"], committed["previous_item_id"]) == ("item_2", "item_1")
    assert (events[-1]["item_id"], events[-1]["transcript"]) == ("item_2", "Salut.")


def test_clear_is_passed_on_and_acknowledged(live_client, service_headers, fake_nemo):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        ws.send_json({"type": "input_audio_buffer.clear"})
        until(ws, "input_audio_buffer.cleared")
    assert fake_nemo.live_sessions[0].clears == 1


@pytest.mark.parametrize("server_vad", [True, False])
def test_semantic_vad_is_always_refused(live_client, service_headers, monkeypatch, server_vad):
    monkeypatch.setattr(live, "SERVER_VAD", server_vad)
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        update(ws, turn_detection={"type": "semantic_vad"})
        error = until(ws, "error")[-1]["error"]
    assert (error["type"], error["code"], error["param"]) == (
        "invalid_request_error",
        "unsupported_turn_detection",
        "session.audio.input.turn_detection",
    )


@pytest.mark.parametrize("server_vad", [True, False])
def test_server_vad_follows_what_nemo_can_do(live_client, service_headers, fake_nemo, monkeypatch, server_vad):
    monkeypatch.setattr(live, "SERVER_VAD", server_vad)
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        update(ws, turn_detection={"type": "server_vad", "silence_duration_ms": 700})
        answer = ws.receive_json()
        if server_vad:
            turn_detection = answer["session"]["audio"]["input"]["turn_detection"]
            assert turn_detection == {"type": "server_vad", "silence_duration_ms": 700}
            # Aucun commit : NeMo ferme seul la prise de parole après le silence.
            ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
            events = until(ws, COMPLETED)
    if server_vad:
        assert [event["type"] for event in events][-2:] == ["input_audio_buffer.committed", COMPLETED]
        assert (events[-2]["item_id"], events[-1]["item_id"]) == ("item_1", "item_1")
        session = fake_nemo.live_sessions[0]
        assert (session.options["endpointing_ms"], session.commits) == (700, 0)
    else:
        assert (answer["type"], answer["error"]["code"]) == ("error", "unsupported_turn_detection")
        assert answer["error"]["param"] == "session.audio.input.turn_detection"


def test_server_vad_without_a_silence_takes_the_default_one(live_client, service_headers, fake_nemo, monkeypatch):
    monkeypatch.setattr(live, "SERVER_VAD", True)
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        update(ws, turn_detection={"type": "server_vad"})
        until(ws, "session.updated")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        until(ws, COMPLETED)
    assert fake_nemo.live_sessions[0].options["endpointing_ms"] == live.DEFAULT_SILENCE_MS == 500


@pytest.mark.parametrize("many_commits", [True, False])
def test_nemo_sessions_follow_the_turns_as_nemo_requires(
    live_client, service_headers, fake_nemo, monkeypatch, many_commits
):
    monkeypatch.setattr(live, "NEMO_MANY_COMMITS", many_commits)
    transcripts = []
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        for _ in range(2):
            ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
            ws.send_json({"type": "input_audio_buffer.commit"})
            transcripts.append(until(ws, COMPLETED)[-1]["transcript"])
            if not many_commits:
                # Une session NeMo n'accepte alors qu'un commit : elle se ferme après son `Committed`.
                wait_for(lambda: fake_nemo.live_sessions[-1].closed)
        sessions = list(fake_nemo.live_sessions)
        if many_commits:
            assert not sessions[0].closed and sessions[0].commits == 2
    assert transcripts == ["Bonjour à tous.", "Salut."]
    assert len(sessions) == (1 if many_commits else 2)
    wait_for(lambda: all(session.closed for session in sessions))


def test_a_nemo_error_becomes_an_error_event_and_closes(live_client, service_headers, fake_nemo):
    fake_nemo.live_error = NemoOverloaded("queue full")
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        error = until(ws, "error")[-1]["error"]
        assert (error["type"], error["code"]) == ("rate_limit_error", "overloaded")
        assert ws.receive()["code"] == 1011


def test_a_client_leaving_mid_turn_closes_the_nemo_session_and_writes_its_line(
    live_client, service_headers, fake_nemo, caplog
):
    caplog.set_level(logging.INFO)
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        wait_for(lambda: fake_nemo.live_sessions)
    wait_for(lambda: fake_nemo.live_sessions[0].closed)
    wait_for(lambda: lines(caplog))
    assert "route=realtime user=default audio_s=0.5 turns=0 status=closed" in caplog.text


def test_an_idle_session_is_closed(live_client, service_headers, monkeypatch):
    monkeypatch.setattr(live, "IDLE_TIMEOUT_S", 0.05)
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        assert until(ws, "error")[-1]["error"]["code"] == "idle_timeout"
        assert ws.receive()["code"] == 1000


def test_audio_over_the_limit_ends_the_session(live_client, service_headers, monkeypatch):
    monkeypatch.setattr(live, "MAX_AUDIO_S", 1.0)
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.6, 24000)})
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.6, 24000)})
        assert until(ws, "error")[-1]["error"]["code"] == "audio_too_long"
        assert ws.receive()["code"] == 1000


def test_the_session_line_never_carries_any_text(live_client, fake_nemo, db, caplog):
    caplog.set_level(logging.DEBUG)
    headers, owner = personal_headers(db, "alice@example.test")
    db.save_settings(owner, glossary="Nemotron")
    with open_live(live_client, headers) as ws:
        until(ws, "session.created")
        update(ws, transcription={"language": "fr", "keywords": ["Kubernetes"]})
        until(ws, "session.updated")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        until(ws, COMPLETED)
    wait_for(lambda: lines(caplog))
    assert lines(caplog) == [f"route=realtime user={owner} audio_s=0.5 turns=1 status=closed"]
    for text in ("Bonjour", "Nemotron", "Kubernetes"):
        assert text not in caplog.text


@pytest.mark.parametrize(
    ("limits", "status"),
    [({"IDLE_TIMEOUT_S": 0.05}, "idle"), ({"MAX_AUDIO_S": 0.1}, "limit"), ({}, "overloaded")],
)
def test_the_session_line_says_how_the_session_ended(
    live_client, service_headers, fake_nemo, monkeypatch, caplog, limits, status
):
    caplog.set_level(logging.INFO)
    for name, value in limits.items():
        monkeypatch.setattr(live, name, value)
    fake_nemo.live_error = NemoOverloaded("queue full")
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        if status != "idle":
            ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        until(ws, "error")
    wait_for(lambda: lines(caplog))
    assert lines(caplog)[0].endswith(f" status={status}")


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (NemoError("invalid_argument: unknown term Nemotron"), "upstream_error"),
        (RuntimeError("Nemotron"), "server_error"),
    ],
)
def test_a_failure_reaches_neither_the_client_nor_the_log_with_its_message(
    live_client, service_headers, fake_nemo, caplog, error, status
):
    caplog.set_level(logging.DEBUG)
    fake_nemo.live_error = error
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        sent = until(ws, "error")[-1]["error"]
        assert (sent["type"], ws.receive()["code"]) == ("server_error", 1011)
    wait_for(lambda: lines(caplog))
    assert lines(caplog)[0].endswith(f" status={status}")
    assert "Nemotron" not in json.dumps(sent) and "Nemotron" not in caplog.text


@pytest.mark.parametrize(
    ("audio_input", "param"),
    [
        ({"format": {"type": "audio/pcmu"}}, "session.audio.input.format.type"),
        ({"format": {"type": "audio/pcm", "rate": 7999}}, "session.audio.input.format.rate"),
        ({"format": {"type": "audio/pcm", "rate": 96001}}, "session.audio.input.format.rate"),
        ({"transcription": {"language": "french"}}, "session.audio.input.transcription.language"),
        ({"transcription": {"languages": ["french"]}}, "session.audio.input.transcription.languages"),
    ],
)
def test_a_refused_setting_names_its_field_and_leaves_the_session_as_it_was(
    live_client, service_headers, audio_input, param
):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json(
            {
                "type": "session.update",
                "event_id": "evt_client_1",
                "session": {
                    "type": "transcription",
                    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 8000}, **audio_input}},
                },
            }
        )
        error = until(ws, "error")[-1]["error"]
        update(ws)
        session = until(ws, "session.updated")[-1]["session"]["audio"]["input"]
    assert (error["code"], error["param"], error["event_id"]) == ("invalid_value", param, "evt_client_1")
    assert (session["format"]["rate"], session["transcription"]["language"]) == (24000, "fr")


def test_the_session_cannot_change_once_its_audio_has_started(live_client, service_headers):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        update(ws, format={"type": "audio/pcm", "rate": 16000})
        assert until(ws, "error")[-1]["error"]["code"] == "session_already_started"


def test_a_commit_without_audio_is_refused_and_the_session_goes_on(live_client, service_headers):
    with open_live(live_client, service_headers) as ws:
        until(ws, "session.created")
        ws.send_json({"type": "input_audio_buffer.commit"})
        assert until(ws, "error")[-1]["error"]["code"] == "input_audio_buffer_commit_empty"
        ws.send_json({"type": "input_audio_buffer.append", "audio": pcm_b64(0.5, 24000)})
        ws.send_json({"type": "input_audio_buffer.commit"})
        assert until(ws, COMPLETED)[-1]["item_id"] == "item_1"


# Le SDK d'OpenAI ouvre sa connexion par `websockets.sync` hors d'un bloc `with`, ce que `websockets` 17 déprécie :
# l'avertissement vient du SDK, pas de dictee.
@pytest.mark.filterwarnings("ignore:connect\\(\\) must be used as a context manager:DeprecationWarning")
def test_the_openai_sdk_runs_a_transcription_session(live_server, settings, fake_nemo):
    client = OpenAI(api_key=settings.openwebui_service_key, base_url=f"{live_server}/v1", max_retries=0)
    with client.realtime.connect(extra_query={"intent": "transcription"}) as connection:
        connection.send(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None}},
                },
            }
        )
        connection.input_audio_buffer.append(audio=pcm_b64(0.2, 24000))
        connection.input_audio_buffer.commit()
        events = []
        while not events or events[-1].type != "conversation.item.input_audio_transcription.completed":
            events.append(connection.recv())
    assert events[-1].transcript == "Bonjour à tous."
