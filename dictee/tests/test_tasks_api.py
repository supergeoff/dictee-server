"""Objet tâche, lecture et annulation d'une tâche de transcription."""

import time
from dataclasses import replace

import pytest
from conftest import personal_headers
from taskdata import NOW, RAW, RESULT, make

from dictee.errors import ApiError
from dictee.modes import Mode
from dictee.tasks_api import task_error, task_not_found, task_object
from dictee.taskstore import TaskStatus


def app_store(client):
    """Le registre des tâches de l'app : le test y crée ses tâches directement."""
    return client.app.state.store


# --- objet tâche ---------------------------------------------------------------------------------------------------


def test_the_object_of_a_queued_task(store):
    task = make(store, model="dictee-diarize", duration=7180.4)
    assert task_object(task) == {
        "id": task.id,
        "object": "audio.transcription.task",
        "status": "queued",
        "model": "dictee-diarize",
        "created_at": NOW,
        "completed_at": None,
        "expires_at": None,
        "duration": 7180.4,
        "progress": 0.0,
        "error": None,
        "output": None,
    }


def test_duration_and_progress_are_rounded_to_the_thousandth(store):
    task = make(store, duration=7180.123456)
    store.set_progress(task.id, 0.123456)
    shown = task_object(store.get(task.id))
    assert (shown["duration"], shown["progress"]) == (7180.123, 0.123)


def test_a_task_in_progress_has_no_output_yet(store):
    shown = task_object(store.start(make(store).id, NOW))
    assert (shown["status"], shown["output"], shown["completed_at"]) == ("in_progress", None, None)


def test_a_finished_task_carries_its_output_in_the_format_of_its_creation(store):
    srt = store.complete(make(store, response_format="srt").id, RESULT, NOW, 86_400)
    assert task_object(srt)["output"].startswith("1\n00:00:00,000 --> ")
    verbose = store.complete(
        make(store, response_format="verbose_json", granularities=frozenset({"word"})).id, RESULT, NOW, 86_400
    )
    assert "words" in task_object(verbose)["output"]


def test_a_finished_task_gives_its_dates_and_an_object_output_for_a_json_format(store):
    done = store.complete(make(store, response_format="diarized_json").id, RESULT, NOW + 5, 86_400)
    shown = task_object(done)
    assert (shown["status"], shown["progress"], shown["completed_at"], shown["expires_at"]) == (
        "completed",
        1.0,
        NOW + 5,
        NOW + 5 + 86_400,
    )
    assert [segment["speaker"] for segment in shown["output"]["segments"]] == ["A", "B"]


def test_a_failed_task_carries_its_code_and_message(store):
    failed = store.fail(make(store).id, "overloaded", "The speech engine is busy, try again shortly.", NOW, 86_400)
    shown = task_object(failed)
    assert shown["error"] == {"code": "overloaded", "message": "The speech engine is busy, try again shortly."}
    assert (shown["status"], shown["output"]) == ("failed", None)
    assert (task_error(failed).status, task_error(failed).code) == (429, "overloaded")


def test_a_cancelled_task_has_neither_error_nor_output(store):
    cancelled = store.cancel(make(store).id, NOW, 86_400)
    shown = task_object(cancelled)
    assert (shown["status"], shown["error"], shown["output"]) == ("cancelled", None, None)


# --- erreurs d'une tâche -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,status,error_type",
    [
        ("overloaded", 429, "rate_limit_error"),
        ("upstream_error", 502, "server_error"),
        ("server_error", 500, "server_error"),
        ("interrupted", 500, "server_error"),
    ],
)
def test_a_failed_task_gives_the_error_of_its_code(store, code, status, error_type):
    failed = store.fail(make(store).id, code, "Message enregistré.", NOW, 86_400)
    error = task_error(failed)
    assert isinstance(error, ApiError)
    assert (error.status, error.type, error.code, error.message) == (status, error_type, code, "Message enregistré.")


def test_a_cancelled_task_gives_a_conflict(store):
    error = task_error(store.cancel(make(store).id, NOW, 86_400))
    assert (error.status, error.type, error.code) == (409, "invalid_request_error", "task_cancelled")
    assert error.message == "The transcription task was cancelled."


def test_a_task_that_neither_failed_nor_was_cancelled_has_no_error(store):
    with pytest.raises(ValueError):
        task_error(make(store))


def test_the_not_found_error_names_the_id():
    error = task_not_found("trn_x")
    assert (error.status, error.type, error.code) == (404, "invalid_request_error", "not_found")
    assert error.message == "No transcription task found with id 'trn_x'."


# --- lecture -------------------------------------------------------------------------------------------------------


def test_the_owner_reads_its_task(transcriptions_client, db):
    headers, owner = personal_headers(db, "alice@example.test")
    task = make(app_store(transcriptions_client), owner=owner)
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=headers).json()["id"] == task.id


def test_the_read_gives_the_object_of_the_task(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    done = store.complete(make(store, owner=None, now=now).id, RESULT, now, 86_400)
    response = transcriptions_client.get(f"/v1/audio/transcriptions/{done.id}", headers=service_headers)
    assert (response.status_code, response.json()) == (200, task_object(done))


def test_get_vtt_renders_diarized_task_without_changing_its_default(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    params = replace(RAW, mode=Mode.DIARIZE)
    task = make(store, owner=None, params=params, response_format="json", now=now)
    done = store.complete(task.id, RESULT, now, 86_400)
    url = f"/v1/audio/transcriptions/{done.id}"
    response = transcriptions_client.get(url, headers=service_headers, params={"response_format": "vtt"})
    assert response.status_code == 200
    assert response.json()["output"].startswith("WEBVTT\n")
    assert "<v A>" in response.json()["output"] and "<v B>" in response.json()["output"]
    assert isinstance(transcriptions_client.get(url, headers=service_headers).json()["output"], dict)
    assert store.get(done.id).response_format == "json"


def test_get_alternate_formats_keeps_stored_settings(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    task = make(store, owner=None, params=replace(RAW, mode=Mode.DIARIZE), now=now)
    done = store.complete(task.id, RESULT, now, 86_400)
    url = f"/v1/audio/transcriptions/{done.id}"
    for fmt in ("json", "vtt", "json"):
        body = transcriptions_client.get(url, headers=service_headers, params={"response_format": fmt}).json()
        assert body["id"] == done.id and body["expires_at"] == done.expires_at
        assert isinstance(body["output"], dict if fmt == "json" else str)
    saved = store.get(done.id)
    assert (saved.response_format, saved.granularities) == ("json", frozenset({"segment"}))


def test_get_rejects_formats_incompatible_with_processing(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    raw = make(store, owner=None, now=now)
    clean = make(store, owner=None, params=replace(raw.params, mode=Mode.CLEAN), now=now)
    for task, bad, good in ((raw, "diarized_json", "vtt"), (clean, "vtt", "text")):
        store.complete(task.id, RESULT, now, 86_400)
        url = f"/v1/audio/transcriptions/{task.id}"
        response = transcriptions_client.get(url, headers=service_headers, params={"response_format": bad})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "incompatible_response_format"
        assert transcriptions_client.get(url, headers=service_headers, params={"response_format": good}).status_code == 200


def test_get_verbose_json_granularities(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    task = make(store, owner=None, response_format="verbose_json", granularities=frozenset({"word"}), now=now)
    store.complete(task.id, RESULT, now, 86_400)
    url = f"/v1/audio/transcriptions/{task.id}"
    words = transcriptions_client.get(url, headers=service_headers, params={
        "response_format": "verbose_json", "timestamp_granularities[]": "word"
    }).json()["output"]
    segments = transcriptions_client.get(url, headers=service_headers, params={
        "response_format": "verbose_json", "timestamp_granularities[]": "segment"
    }).json()["output"]
    default = transcriptions_client.get(url, headers=service_headers).json()["output"]
    assert "words" in words and "segments" not in words
    assert "segments" in segments and "words" not in segments
    assert "words" in default and "segments" not in default


def test_get_options_validate_without_disclosing_task(transcriptions_client, service_headers, db):
    store, now = app_store(transcriptions_client), int(time.time())
    owner_headers, owner = personal_headers(db, "owner@example.test")
    other_headers, _ = personal_headers(db, "other@example.test")
    task = make(store, owner=owner, now=now)
    store.complete(task.id, RESULT, now, 86_400)
    url = f"/v1/audio/transcriptions/{task.id}"
    assert transcriptions_client.get(url, headers=owner_headers, params={"response_format": "bad"}).status_code == 400
    assert transcriptions_client.get(url, headers=owner_headers, params={
        "response_format": "verbose_json", "timestamp_granularities[]": "bad"
    }).status_code == 400
    assert transcriptions_client.get(url, headers=owner_headers, params={
        "response_format": "vtt", "timestamp_granularities[]": "word"
    }).status_code == 400
    assert transcriptions_client.get(url, headers=other_headers, params={"response_format": "bad"}).status_code == 404
    assert transcriptions_client.get("/v1/audio/transcriptions/trn_unknown", headers=owner_headers,
                                     params={"response_format": "bad"}).status_code == 404
    expired = make(store, owner=owner, now=NOW)
    store.complete(expired.id, RESULT, NOW, 60)
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{expired.id}", headers=owner_headers,
                                     params={"response_format": "bad"}).status_code == 404
    assert transcriptions_client.get(url, params={"response_format": "bad"}).status_code == 401


def test_get_vtt_keeps_unfinished_and_terminal_states(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    params = replace(RAW, mode=Mode.DIARIZE)
    queued = make(store, owner=None, params=params, now=now)
    failed = make(store, owner=None, params=params, now=now)
    cancelled = make(store, owner=None, params=params, now=now)
    store.fail(failed.id, "upstream_error", "test failure", now, 86_400)
    store.cancel(cancelled.id, now, 86_400)
    for task, expected in ((queued, "queued"), (failed, "failed"), (cancelled, "cancelled")):
        body = transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=service_headers,
                                        params={"response_format": "vtt"}).json()
        assert body["status"] == expected and body["output"] is None
        if expected == "failed":
            assert body["error"] == {"code": "upstream_error", "message": "test failure"}


def test_another_caller_gets_the_same_404_as_an_unknown_id(transcriptions_client, db):
    owner_headers, owner = personal_headers(db, "alice@example.test")
    other_headers, _ = personal_headers(db, "bob@example.test")
    task = make(app_store(transcriptions_client), owner=owner)
    hidden = transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=other_headers)
    unknown = transcriptions_client.get("/v1/audio/transcriptions/trn_unknown", headers=owner_headers)
    assert (hidden.status_code, unknown.status_code) == (404, 404)
    assert hidden.json()["error"]["code"] == unknown.json()["error"]["code"] == "not_found"


def test_the_default_account_reads_its_own_task_with_the_service_key(transcriptions_client, service_headers):
    task = make(app_store(transcriptions_client), owner=None)
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=service_headers).status_code == 200


def test_a_personal_key_does_not_read_the_task_of_the_default_account(transcriptions_client, db):
    headers, _ = personal_headers(db, "alice@example.test")
    task = make(app_store(transcriptions_client), owner=None)
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=headers).status_code == 404


def test_an_expired_task_is_404(transcriptions_client, service_headers):
    store = app_store(transcriptions_client)
    task = store.complete(make(store, owner=None).id, RESULT, NOW, 60)  # expirée depuis longtemps
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=service_headers).status_code == 404


# --- annulation ----------------------------------------------------------------------------------------------------


def test_cancel_gives_the_cancelled_task_and_can_be_repeated(transcriptions_client, service_headers):
    task = make(app_store(transcriptions_client), owner=None, now=int(time.time()))
    for _ in range(2):
        response = transcriptions_client.post(f"/v1/audio/transcriptions/{task.id}/cancel", headers=service_headers)
        assert (response.status_code, response.json()["status"]) == (200, "cancelled")


def test_cancel_leaves_a_finished_task_as_it_is(transcriptions_client, service_headers):
    store, now = app_store(transcriptions_client), int(time.time())
    done = store.complete(make(store, owner=None, now=now).id, RESULT, now, 86_400)
    response = transcriptions_client.post(f"/v1/audio/transcriptions/{done.id}/cancel", headers=service_headers)
    assert response.json() == task_object(done)


def test_cancel_of_another_callers_task_is_404_and_leaves_it_running(transcriptions_client, db):
    owner_headers, owner = personal_headers(db, "alice@example.test")
    other_headers, _ = personal_headers(db, "bob@example.test")
    store = app_store(transcriptions_client)
    task = make(store, owner=owner, now=int(time.time()))
    response = transcriptions_client.post(f"/v1/audio/transcriptions/{task.id}/cancel", headers=other_headers)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
    assert store.get(task.id).status is TaskStatus.QUEUED
    assert transcriptions_client.get(f"/v1/audio/transcriptions/{task.id}", headers=owner_headers).status_code == 200


def test_cancel_of_an_unknown_id_is_404(transcriptions_client, service_headers):
    response = transcriptions_client.post("/v1/audio/transcriptions/trn_unknown/cancel", headers=service_headers)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")


def test_cancel_of_a_task_that_vanishes_in_between_is_404(transcriptions_client, service_headers, db, monkeypatch):
    # Le ménage efface la tâche entre sa lecture et l'annulation : le runner ne la connaît plus.
    store = app_store(transcriptions_client)
    task = make(store, owner=None, now=int(time.time()))
    get_visible = store.get_visible

    def get_visible_then_clean_up(*args):
        found = get_visible(*args)
        with db.connect() as conn:
            conn.execute("delete from tasks where id = ?", (task.id,))
        return found

    monkeypatch.setattr(store, "get_visible", get_visible_then_clean_up)
    response = transcriptions_client.post(f"/v1/audio/transcriptions/{task.id}/cancel", headers=service_headers)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")


# --- clé -----------------------------------------------------------------------------------------------------------


def test_the_task_routes_need_a_key(transcriptions_client):
    assert transcriptions_client.get("/v1/audio/transcriptions/trn_x").status_code == 401
    assert transcriptions_client.post("/v1/audio/transcriptions/trn_x/cancel").status_code == 401
