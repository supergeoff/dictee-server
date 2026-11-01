import re
import sqlite3
from dataclasses import replace

import pytest
from taskdata import NOW, RAW, RESULT, make

from dictee.db import Database
from dictee.modes import Mode
from dictee.profile import GlossaryEntry
from dictee.taskstore import (
    TERMINAL_STATUSES,
    ChunkRecord,
    TaskParams,
    TaskResult,
    TaskStatus,
    TaskStore,
    new_task_id,
)
from dictee.types import Transcript, Word


def stored_column(db_path, task_id, column):
    """Valeur brute d'une colonne de `tasks`, lue sans passer par le registre."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"select {column} from tasks where id = ?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def test_statuses_are_the_ones_openai_uses():
    assert [status.value for status in TaskStatus] == ["queued", "in_progress", "completed", "failed", "cancelled"]
    assert TERMINAL_STATUSES == {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def test_task_ids_carry_their_prefix_and_sort_by_creation_time():
    first, second = new_task_id(1_700_000_000_000), new_task_id(1_700_000_000_001)
    assert re.fullmatch(r"trn_[0-9A-HJKMNP-TV-Z]{26}", first)
    assert first < second


def test_the_millisecond_clock_fills_the_first_ten_characters_in_base_32():
    assert new_task_id(0)[4:14] == "0000000000"
    assert new_task_id(32)[4:14] == "0000000010"
    assert new_task_id(2**48 - 1)[4:14] == "7ZZZZZZZZZ"


def test_a_task_id_is_new_at_every_call_and_defaults_to_the_current_time():
    assert new_task_id() != new_task_id()
    assert new_task_id(5) != new_task_id(5)


@pytest.mark.parametrize("now_ms", [-1, 2**48])
def test_a_clock_that_does_not_fit_in_48_bits_is_refused(now_ms):
    with pytest.raises(ValueError):
        new_task_id(now_ms)


def test_the_digest_follows_the_glossary_the_boost_and_the_instructions():
    assert RAW.digest() == replace(RAW).digest()
    assert RAW.digest() != replace(RAW, glossary=(GlossaryEntry("Sortformer"),)).digest()
    assert RAW.digest() != replace(RAW, glossary=(GlossaryEntry("Nemotron", ("némo trône",)),)).digest()
    assert RAW.digest() != replace(RAW, boost=1.5).digest()
    assert RAW.digest() != replace(RAW, mode=Mode.CLEAN, instructions="Corrige.").digest()


def test_the_digest_follows_the_language():
    assert RAW.digest() != replace(RAW, language="en-US").digest()


def test_the_digest_is_a_hexadecimal_sha256_that_ignores_how_the_boost_is_written():
    assert re.fullmatch(r"[0-9a-f]{64}", RAW.digest())
    assert RAW.digest() == replace(RAW, boost=1).digest()


def test_a_new_task_is_queued_without_progress(store):
    task = make(store)
    assert (task.status, task.progress, task.attempts, task.result) == (TaskStatus.QUEUED, 0.0, 0, None)
    assert store.get(task.id) == task


def test_a_task_comes_back_from_the_database_as_it_was_created(store):
    params = TaskParams(
        "en-US",
        Mode.CLEAN,
        (GlossaryEntry("Nemotron", ("némo trône", "nemo tron")), GlossaryEntry("Sortformer")),
        0.5,
        "Corrige.",
    )
    task = make(
        store,
        owner=None,
        model="dictee-clean",
        params=params,
        response_format="verbose_json",
        granularities=frozenset({"word", "segment"}),
        upload_sha256="d" * 64,
        duration=7180.4,
    )
    assert (task.owner, task.model, task.params, task.response_format) == (None, "dictee-clean", params, "verbose_json")
    assert (task.granularities, task.upload_sha256, task.duration) == (frozenset({"word", "segment"}), "d" * 64, 7180.4)
    assert (task.created_at, task.started_at, task.completed_at, task.expires_at) == (NOW, None, None, None)
    assert (task.cuts, task.error_code, task.error_message) == (None, None, None)
    assert store.get(task.id) == task


def test_the_stored_digest_is_the_digest_of_the_params(store, db_path):
    task = make(store)
    assert stored_column(db_path, task.id, "params_sha256") == RAW.digest()


def test_an_unknown_task_is_not_found(store):
    assert store.get("trn_unknown") is None


def test_an_identical_upload_finds_a_queued_running_or_finished_task(store):
    task = make(store)
    assert store.find_identical(1, task.upload_sha256, RAW.digest(), NOW) == task
    store.start(task.id, NOW)
    assert store.find_identical(1, task.upload_sha256, RAW.digest(), NOW).status is TaskStatus.IN_PROGRESS
    store.complete(task.id, RESULT, NOW, 86_400)
    assert store.find_identical(1, task.upload_sha256, RAW.digest(), NOW + 60).status is TaskStatus.COMPLETED


def test_failed_cancelled_and_expired_tasks_are_never_joined(store):
    failed, cancelled, done = make(store), make(store, upload_sha256="b" * 64), make(store, upload_sha256="c" * 64)
    store.fail(failed.id, "overloaded", "busy", NOW, 86_400)
    store.cancel(cancelled.id, NOW, 86_400)
    store.complete(done.id, RESULT, NOW, 60)
    assert store.find_identical(1, "a" * 64, RAW.digest(), NOW) is None
    assert store.find_identical(1, "b" * 64, RAW.digest(), NOW) is None
    assert store.find_identical(1, "c" * 64, RAW.digest(), NOW + 60) is None


def test_the_most_recent_identical_task_is_the_one_found(store):
    make(store, now=NOW)
    newer = make(store, now=NOW + 5)
    assert store.find_identical(1, "a" * 64, RAW.digest(), NOW + 5) == newer


def test_another_upload_or_other_params_are_not_identical(store):
    make(store)
    assert store.find_identical(1, "b" * 64, RAW.digest(), NOW) is None
    assert store.find_identical(1, "a" * 64, replace(RAW, boost=1.5).digest(), NOW) is None


def test_the_default_account_finds_and_sees_its_own_tasks(store):
    task = make(store, owner=None)
    assert store.find_identical(None, task.upload_sha256, RAW.digest(), NOW) == task
    assert store.get_visible(task.id, None, NOW) == task
    assert store.get_visible(task.id, 7, NOW) is None


def test_an_account_never_finds_the_task_of_another_account_or_of_the_default_one(store):
    own, default = make(store), make(store, owner=None)
    assert store.find_identical(1, "a" * 64, RAW.digest(), NOW) == own
    assert store.find_identical(None, "a" * 64, RAW.digest(), NOW) == default
    assert store.find_identical(2, "a" * 64, RAW.digest(), NOW) is None


def test_another_owner_and_an_expired_task_are_not_visible(store):
    task = make(store)
    store.complete(task.id, RESULT, NOW, 60)
    assert store.get_visible(task.id, 2, NOW) is None
    assert store.get_visible(task.id, 1, NOW + 59) is not None
    assert store.get_visible(task.id, 1, NOW + 60) is None


def test_an_unfinished_task_never_expires(store):
    task = make(store)
    assert store.get_visible(task.id, 1, NOW + 10**9) == task


def test_start_counts_the_starts_and_keeps_the_first_start_date(store):
    task = make(store)
    store.start(task.id, NOW + 1)
    again = store.start(task.id, NOW + 9)
    assert (again.attempts, again.started_at, again.status) == (2, NOW + 1, TaskStatus.IN_PROGRESS)


def test_cuts_and_progress_are_kept(store):
    task = make(store)
    assert task.cuts is None
    store.set_cuts(task.id, (29.5, 61.0))
    store.set_progress(task.id, 0.25)
    saved = store.get(task.id)
    assert (saved.cuts, saved.progress) == ((29.5, 61.0), 0.25)


def test_an_audio_without_any_cut_is_told_apart_from_cuts_not_yet_found(store):
    task = make(store)
    store.set_cuts(task.id, ())
    assert store.get(task.id).cuts == ()


def test_chunks_are_kept_once_per_rank_and_read_back_in_order(store):
    task = make(store)
    store.save_chunk(task.id, ChunkRecord(1, 30.0, 61.0, "Salut.", (Word("Salut.", 31.0, 31.5),)))
    store.save_chunk(task.id, ChunkRecord(0, 0.0, 30.0, "Bonjour.", ()))
    store.save_chunk(task.id, ChunkRecord(1, 30.0, 61.0, "Salut !", ()))
    assert [(chunk.idx, chunk.text) for chunk in store.chunks(task.id)] == [(0, "Bonjour."), (1, "Salut !")]


def test_a_chunk_keeps_its_bounds_and_its_words_with_their_people(store):
    task = make(store)
    chunk = ChunkRecord(0, 0.0, 30.5, "Salut.", (Word("Salut.", 1.5, 2.0, "B"), Word("Oui", 2.5, 2.9)))
    store.save_chunk(task.id, chunk)
    assert store.chunks(task.id) == [chunk]


def test_chunks_belong_to_their_task(store):
    first, second = make(store), make(store)
    store.save_chunk(first.id, ChunkRecord(0, 0.0, 30.0, "Bonjour.", ()))
    assert store.chunks(second.id) == []


def test_completion_keeps_the_result_for_the_retention(store):
    done = store.complete(make(store).id, RESULT, NOW, 86_400)
    assert (done.status, done.progress, done.completed_at, done.expires_at) == (
        TaskStatus.COMPLETED,
        1.0,
        NOW,
        NOW + 86_400,
    )
    assert done.result == RESULT
    assert store.get(done.id) == done


def test_a_retention_in_fractional_seconds_gives_a_whole_expiry(store, db_path):
    done = store.complete(make(store).id, RESULT, NOW, 5_400.5)
    assert done.expires_at == NOW + 5_400
    assert isinstance(stored_column(db_path, done.id, "expires_at"), int)


def test_failure_and_cancellation_record_their_end(store):
    failed = store.fail(make(store).id, "upstream_error", "no answer", NOW, 86_400)
    assert (failed.status, failed.error_code, failed.expires_at) == (TaskStatus.FAILED, "upstream_error", NOW + 86_400)
    assert (failed.error_message, failed.completed_at) == ("no answer", NOW)
    assert store.cancel(failed.id, NOW + 5, 86_400) == failed
    assert store.cancel("trn_unknown", NOW, 86_400) is None


def test_a_cancelled_task_records_its_end(store):
    cancelled = store.cancel(make(store).id, NOW, 3_600)
    assert (cancelled.status, cancelled.completed_at, cancelled.expires_at) == (TaskStatus.CANCELLED, NOW, NOW + 3_600)
    assert cancelled.result is None


def test_a_finished_task_keeps_its_first_end(store):
    cancelled = store.cancel(make(store).id, NOW, 60)
    assert store.complete(cancelled.id, RESULT, NOW + 5, 120) == cancelled
    assert store.fail(cancelled.id, "server_error", "late", NOW + 5, 120) == cancelled
    assert store.start(cancelled.id, NOW + 5) == cancelled
    assert store.get(cancelled.id) == cancelled


def test_a_task_that_does_not_exist_cannot_be_started_completed_or_failed(store):
    with pytest.raises(KeyError):
        store.start("trn_unknown", NOW)
    with pytest.raises(KeyError):
        store.complete("trn_unknown", RESULT, NOW, 60)
    with pytest.raises(KeyError):
        store.fail("trn_unknown", "server_error", "gone", NOW, 60)


def test_unfinished_tasks_come_in_creation_order(store):
    later, earlier = make(store, now=NOW + 10), make(store, now=NOW)
    store.complete(make(store, now=NOW + 5).id, RESULT, NOW, 60)
    assert [task.id for task in store.unfinished()] == [earlier.id, later.id]


def test_running_tasks_are_unfinished_too(store):
    queued, running = make(store, now=NOW), make(store, now=NOW + 1)
    store.start(running.id, NOW + 2)
    assert [(task.id, task.status) for task in store.unfinished()] == [
        (queued.id, TaskStatus.QUEUED),
        (running.id, TaskStatus.IN_PROGRESS),
    ]


def test_expired_tasks_are_deleted_with_their_chunks(store):
    task = make(store)
    store.save_chunk(task.id, ChunkRecord(0, 0.0, 12.0, "Bonjour.", ()))
    store.complete(task.id, RESULT, NOW, 60)
    assert store.delete_expired(NOW + 60) == [task.id]
    assert store.get(task.id) is None and store.chunks(task.id) == []


def test_only_expired_tasks_are_deleted(store):
    running, expired, kept = make(store), make(store), make(store)
    store.complete(expired.id, RESULT, NOW, 60)
    store.complete(kept.id, RESULT, NOW, 120)
    assert store.delete_expired(NOW + 60) == [expired.id]
    assert store.known_ids() == {running.id, kept.id}
    assert store.delete_expired(NOW + 60) == []


def test_the_known_ids_cover_every_status(store):
    assert store.known_ids() == set()
    queued, cancelled = make(store), make(store)
    store.cancel(cancelled.id, NOW, 60)
    assert store.known_ids() == {queued.id, cancelled.id}


def test_tasks_and_chunks_survive_reopening_the_database(store, db_path):
    task = make(store)
    store.save_chunk(task.id, ChunkRecord(0, 0.0, 12.0, "Bonjour.", ()))
    reopened = TaskStore(Database(db_path))
    assert reopened.get(task.id) == task
    assert [chunk.text for chunk in reopened.chunks(task.id)] == ["Bonjour."]


def test_a_result_survives_its_json_form():
    assert TaskResult.from_json(RESULT.to_json()) == RESULT  # RESULT porte des mots avec personnes et des segments


def test_a_result_without_segments_and_a_cleaned_text_survive_their_json_form():
    cleaned = replace(RESULT, segments=None, cleaned="Bonjour à tous, salut.", decision="cleaned")
    assert TaskResult.from_json(cleaned.to_json()) == cleaned


def test_the_json_form_of_a_result_is_stable():
    once = RESULT.to_json()
    assert TaskResult.from_json(once).to_json() == once


def test_the_transcript_of_a_result_is_its_raw_text_until_it_is_cleaned():
    assert RESULT.transcript() == Transcript(RESULT.text, RESULT.words, 12.0, "fr-FR", RESULT.segments)
    transcript = replace(RESULT, cleaned="Bonjour à tous, salut.", decision="cleaned").transcript()
    assert (transcript.text, transcript.words, transcript.segments) == (
        "Bonjour à tous, salut.",
        RESULT.words,
        RESULT.segments,
    )
