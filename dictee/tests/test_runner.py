"""File des tâches longues, boucle de travail, reprise au démarrage, annulation et ménage des tâches."""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from conftest import wait_until
from taskdata import NOW, RAW, RESULT, make

from dictee import runner as runner_module
from dictee.errors import ApiError
from dictee.modes import Mode
from dictee.nemo import NemoError, NemoOverloaded, NemoUnavailable
from dictee.processing import RunStats, task_audio, task_dir
from dictee.runner import LongLane, TaskRunner, failure_of, log_task
from dictee.taskstore import TaskRecord, TaskStatus

pytestmark = pytest.mark.anyio


class HeldProcess:
    """Remplace `process` : retient chaque tâche jusqu'à `release(task_id)`, puis rend `RESULT` ou lève `error`.

    `started` note, dans l'ordre, les tâches reçues. Avec `auto`, rien n'est retenu. `during`, s'il est donné, est
    appelé avec la tâche juste avant la fin du traitement. `runners` garde les runners que `runner_for` construit : la
    fixture `held` les arrête à la fin du test.
    """

    def __init__(self) -> None:
        self.started: list[str] = []
        self.auto = False
        self.error: Exception | None = None
        self.during: Callable[[TaskRecord], None] | None = None
        self.runners: list[TaskRunner] = []
        self._released: dict[str, asyncio.Event] = {}

    def _event(self, task_id: str) -> asyncio.Event:
        return self._released.setdefault(task_id, asyncio.Event())

    def release(self, task_id: str) -> None:
        self._event(task_id).set()

    async def __call__(self, services, task, stats):
        self.started.append(task.id)
        if not self.auto:
            await self._event(task.id).wait()
        if self.during is not None:
            self.during(task)
        if self.error is not None:
            raise self.error
        return RESULT


@pytest.fixture
async def held():
    process = HeldProcess()
    yield process
    for runner in process.runners:
        await runner.stop()


async def runner_for(services, lane, held, start=True) -> TaskRunner:
    """Un runner qui passe par `held`, démarré si `start` ; la fixture `held` l'arrête à la fin du test.

    L'horloge donne `NOW` plus une demi-seconde, pour vérifier que le runner passe des secondes entières au registre.
    """
    runner = TaskRunner(services, lane, process=held, clock=lambda: NOW + 0.5)
    held.runners.append(runner)
    if start:
        await runner.start()
    return runner


def make_task(services, seconds: float, **changes) -> TaskRecord:
    """Une tâche en file de `seconds` secondes, avec son dossier et un WAV de remplissage : le runner n'y lit rien."""
    task = make(services.store, duration=seconds, **changes)
    audio = task_audio(services.settings, task.id)
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFF")
    return task


def long_task(services, now: int = NOW) -> TaskRecord:
    return make_task(services, 900.0, now=now)


def short_task(services) -> TaskRecord:
    return make_task(services, 3.0)


async def wait_for_end(runner: TaskRunner, task_id: str) -> TaskRecord:
    """`runner.wait` avec un délai : une régression fait échouer le test au lieu de le suspendre."""
    with anyio.fail_after(2):
        return await runner.wait(task_id)


async def enter(ticket) -> None:
    """Entre dans le tour du ticket et y attend sans fin."""
    async with ticket.turn():
        await asyncio.Event().wait()


def age_folder(path: Path, age_s: float) -> None:
    """Recule la date de modification de `path` à `age_s` secondes avant `NOW`, l'instant que lit l'horloge du runner."""
    os.utime(path, (NOW - age_s, NOW - age_s))


def make_folder(services, name: str, age_s: float) -> Path:
    """Un dossier de `settings.tasks_dir` qui n'est celui d'aucune tâche, modifié il y a `age_s` secondes."""
    folder = services.settings.tasks_dir / name
    folder.mkdir(parents=True)
    (folder / "audio.wav").write_bytes(b"RIFF")
    age_folder(folder, age_s)
    return folder


def task_lines(caplog) -> list[str]:
    return [line for record in caplog.records if (line := record.getMessage()).startswith("task=")]


# La file des tâches longues.


def test_the_lane_takes_one_running_and_three_waiting_long_tasks():
    lane = LongLane(3)
    tickets = [lane.reserve() for _ in range(4)]
    with pytest.raises(ApiError) as refused:
        lane.reserve()
    assert (refused.value.status, refused.value.code) == (429, "long_audio_busy")
    tickets[0].release()
    tickets[0].release()
    lane.reserve()
    assert lane.full


def test_a_forced_reservation_goes_past_the_limit():
    lane = LongLane(0)
    lane.reserve()
    with pytest.raises(ApiError):
        lane.reserve()
    lane.reserve(force=True)
    assert lane.full


async def test_a_turn_gives_its_place_back_on_an_error_or_a_cancellation():
    lane = LongLane(1)
    with pytest.raises(RuntimeError):
        async with lane.reserve().turn():
            raise RuntimeError("boom")
    holder = lane.reserve()
    async with holder.turn():
        waiter = asyncio.create_task(enter(lane.reserve()))
        await wait_until(lambda: lane.full)
        await asyncio.sleep(0)  # le waiter entre dans `turn` et attend le verrou du tour en cours
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert not lane.full


# Le lancement des tâches.


async def test_a_short_task_runs_while_a_long_one_runs(services, held):
    runner = await runner_for(services, LongLane(3), held)
    runner.submit(long_task(services), None)
    short = short_task(services)
    runner.submit(short, None)
    await wait_until(lambda: short.id in held.started)


async def test_long_tasks_run_one_at_a_time_in_order_of_arrival(services, held):
    runner = await runner_for(services, LongLane(3), held)
    first, second = long_task(services), long_task(services)
    runner.submit(first, None)
    runner.submit(second, None)
    await wait_until(lambda: held.started == [first.id])
    held.release(first.id)
    await wait_until(lambda: held.started == [first.id, second.id])


async def test_a_task_is_long_above_the_threshold_only(services, held):
    runner = await runner_for(services, LongLane(3), held, start=False)
    threshold = services.settings.long_audio_seconds
    assert (runner.is_long(threshold), runner.is_long(threshold + 0.1)) == (False, True)


async def test_a_long_task_given_its_ticket_waits_for_its_turn_behind_the_running_one(services, held):
    lane = LongLane(3)
    runner = await runner_for(services, lane, held)
    first, second = long_task(services), long_task(services)
    runner.submit(first, lane.reserve())
    runner.submit(second, lane.reserve())
    await wait_until(lambda: held.started == [first.id])
    held.release(first.id)
    await wait_until(lambda: held.started == [first.id, second.id])
    held.release(second.id)
    await wait_for_end(runner, second.id)
    await wait_until(lambda: not lane.full)
    assert len([lane.reserve() for _ in range(4)]) == 4


async def test_a_short_task_hands_its_ticket_back(services, held):
    lane = LongLane(0)
    runner = await runner_for(services, lane, held)
    held.auto = True
    ticket = lane.reserve()
    assert lane.full
    runner.submit(short_task(services), ticket)
    assert not lane.full


async def test_submitting_a_task_twice_runs_it_once(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    await asyncio.sleep(0.01)  # le temps qu'un second passage, s'il existait, démarre aussi
    assert held.started == [task.id]


# L'attente de la fin d'une tâche.


async def test_wait_gives_the_finished_task_even_when_it_finished_before(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    held.release(task.id)
    assert (await wait_for_end(runner, task.id)).status is TaskStatus.COMPLETED
    assert (await wait_for_end(runner, task.id)).status is TaskStatus.COMPLETED


async def test_wait_and_cancel_know_nothing_of_an_unknown_task(services, held):
    runner = await runner_for(services, LongLane(3), held)
    with pytest.raises(KeyError):
        await wait_for_end(runner, "trn_unknown")
    assert await runner.cancel("trn_unknown") is None


async def test_a_finished_task_is_kept_for_the_retention_time_from_its_end(services, held):
    runner = await runner_for(services, LongLane(3), held)
    held.auto = True
    task = short_task(services)
    runner.submit(task, None)
    ended = await wait_for_end(runner, task.id)
    assert (ended.completed_at, ended.expires_at) == (NOW, NOW + 86_400)
    assert ended.result == RESULT


# L'annulation.


async def test_cancel_stops_a_running_task_removes_its_audio_and_can_be_repeated(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    cancelled = await runner.cancel(task.id)
    assert cancelled.status is TaskStatus.CANCELLED
    await wait_until(lambda: not task_dir(services.settings, task.id).exists())
    assert await runner.cancel(task.id) == services.store.get(task.id)


async def test_cancelling_a_queued_long_task_frees_its_place_and_it_never_runs(services, held):
    lane = LongLane(1)
    runner = await runner_for(services, lane, held)
    running, queued = long_task(services), long_task(services)
    runner.submit(running, lane.reserve())
    runner.submit(queued, lane.reserve())
    assert lane.full
    await runner.cancel(queued.id)
    await wait_until(lambda: not lane.full)
    held.release(running.id)
    await wait_for_end(runner, running.id)
    assert held.started == [running.id]


async def test_cancelling_a_long_task_waiting_for_its_turn_frees_its_place_and_removes_its_audio(services, held):
    lane = LongLane(1)
    runner = await runner_for(services, lane, held)
    running, queued = long_task(services), long_task(services)
    runner.submit(running, lane.reserve())
    runner.submit(queued, lane.reserve())
    await wait_until(lambda: held.started == [running.id])
    await asyncio.sleep(0)  # `queued` attend son tour : elle n'est plus une tâche qui n'a jamais démarré
    await runner.cancel(queued.id)
    assert not lane.full
    assert not task_dir(services.settings, queued.id).exists()
    assert services.store.get(queued.id).status is TaskStatus.CANCELLED
    assert held.started == [running.id]


async def test_cancel_leaves_a_finished_task_as_it_is(services, held):
    runner = await runner_for(services, LongLane(3), held)
    done = services.store.complete(short_task(services).id, RESULT, NOW, 86_400)
    assert await runner.cancel(done.id) == done


async def test_cancel_wakes_the_waiters_with_the_cancelled_task(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    waiter = asyncio.create_task(wait_for_end(runner, task.id))
    await asyncio.sleep(0)
    await runner.cancel(task.id)
    assert (await asyncio.wait_for(waiter, 2)).status is TaskStatus.CANCELLED


async def test_cancelling_a_task_the_runner_does_not_run_ends_it_and_wakes_its_waiters(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    waiter = asyncio.create_task(wait_for_end(runner, task.id))
    await asyncio.sleep(0)
    cancelled = await runner.cancel(task.id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert (await asyncio.wait_for(waiter, 2)) == cancelled
    assert not task_dir(services.settings, task.id).exists()


async def test_a_task_found_finished_at_its_turn_is_not_run_and_loses_its_audio(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    services.store.cancel(task.id, NOW, 86_400)
    runner.submit(task, None)  # `task` est celle de l'envoi, encore en file : le registre dit déjà le contraire
    await wait_until(lambda: not task_dir(services.settings, task.id).exists())
    assert held.started == [] and services.store.get(task.id).attempts == 0


async def test_a_task_cancelled_while_it_ends_stays_cancelled(services, held, caplog):
    caplog.set_level(logging.INFO)
    held.auto = True
    held.during = lambda task: services.store.cancel(task.id, NOW, 86_400)
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    ended = await wait_for_end(runner, task.id)
    assert (ended.status, ended.result) == (TaskStatus.CANCELLED, None)
    assert not task_dir(services.settings, task.id).exists()
    assert [line.split()[-2:] for line in task_lines(caplog)] == [["status=cancelled", "error=-"]]


# La reprise au démarrage, l'arrêt et le ménage.


async def test_unfinished_tasks_resume_at_startup_in_creation_order(services, held):
    first, _second = long_task(services, now=NOW), long_task(services, now=NOW + 1)
    services.store.start(first.id, NOW)
    await runner_for(services, LongLane(3), held)
    await wait_until(lambda: held.started == [first.id])


async def test_resumed_long_tasks_are_never_refused_by_the_lane(services, held):
    lane = LongLane(1)
    tasks = [long_task(services, now=NOW + rank) for rank in range(3)]
    held.auto = True
    await runner_for(services, lane, held)
    await wait_until(lambda: len(held.started) == 3)
    assert held.started == [task.id for task in tasks]
    await wait_until(lambda: not lane.full)
    assert [services.store.get(task.id).status for task in tasks] == [TaskStatus.COMPLETED] * 3


async def test_a_task_started_three_times_fails_as_interrupted_at_startup(services, held):
    task = long_task(services)
    for _ in range(3):
        services.store.start(task.id, NOW)
    await runner_for(services, LongLane(3), held)
    failed = services.store.get(task.id)
    assert (failed.status, failed.error_code) == (TaskStatus.FAILED, "interrupted")
    assert failed.error_message == "The transcription was interrupted several times."
    assert (failed.completed_at, failed.expires_at) == (NOW, NOW + 86_400)
    assert held.started == [] and not task_dir(services.settings, task.id).exists()


async def test_a_task_started_twice_resumes_at_startup(services, held):
    task = long_task(services)
    for _ in range(2):
        services.store.start(task.id, NOW)
    await runner_for(services, LongLane(3), held)
    await wait_until(lambda: held.started == [task.id])


async def test_stopping_leaves_a_running_task_in_progress_with_its_audio(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    await runner.stop()
    assert services.store.get(task.id).status is TaskStatus.IN_PROGRESS
    assert task_audio(services.settings, task.id).exists()


async def test_stopping_leaves_a_waiting_long_task_queued_and_frees_the_lane(services, held):
    lane = LongLane(3)
    runner = await runner_for(services, lane, held)
    running, queued = long_task(services), long_task(services)
    runner.submit(running, lane.reserve())
    runner.submit(queued, lane.reserve())
    await wait_until(lambda: held.started == [running.id])
    await runner.stop()
    assert [services.store.get(task.id).status for task in (running, queued)] == [
        TaskStatus.IN_PROGRESS,
        TaskStatus.QUEUED,
    ]
    assert task_audio(services.settings, running.id).exists() and task_audio(services.settings, queued.id).exists()
    assert not lane.full


async def test_a_stop_that_finds_the_task_cancelled_in_the_registry_ends_the_cancellation(services, held):
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    await wait_until(lambda: task.id in held.started)
    services.store.cancel(task.id, NOW, 86_400)
    await runner.stop()
    assert not task_dir(services.settings, task.id).exists()


async def test_a_cancelled_stop_does_not_cancel_the_passages_a_second_time(services, held):
    started, cleaning, finish_cleaning, cleaned = (asyncio.Event() for _ in range(4))

    async def slow_to_close(services, task, stats):
        """Un traitement dont le nettoyage attend, comme la fermeture de la session de NeMo."""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish_cleaning.wait()
            cleaned.set()

    runner = TaskRunner(services, LongLane(3), process=slow_to_close, clock=lambda: NOW)
    held.runners.append(runner)
    runner.submit(short_task(services), None)
    await asyncio.wait_for(started.wait(), 2)
    stopping = asyncio.create_task(runner.stop())
    await asyncio.wait_for(cleaning.wait(), 2)
    stopping.cancel()
    await asyncio.gather(stopping, return_exceptions=True)
    finish_cleaning.set()
    await asyncio.wait_for(cleaned.wait(), 2)


async def test_stopping_twice_does_no_harm(services, held):
    runner = await runner_for(services, LongLane(3), held)
    await runner.stop()
    await runner.stop()


async def test_a_task_submitted_after_the_stop_stays_queued_and_gives_its_ticket_back(services, held):
    lane = LongLane(0)
    runner = await runner_for(services, lane, held)
    await runner.stop()
    task = long_task(services)
    runner.submit(task, lane.reserve())
    await asyncio.sleep(0)
    assert not lane.full and held.started == []
    assert services.store.get(task.id).status is TaskStatus.QUEUED
    assert task_audio(services.settings, task.id).exists()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (None, None),
        (NemoOverloaded("full"), "overloaded"),
        (NemoError("bad"), "upstream_error"),
        (ValueError("secret text"), "server_error"),
    ],
)
async def test_each_end_records_its_code_and_removes_the_audio(services, held, error, code):
    held.auto, held.error = True, error
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    ended = await wait_for_end(runner, task.id)
    assert ended.status is (TaskStatus.COMPLETED if error is None else TaskStatus.FAILED)
    assert ended.error_code == code and not task_dir(services.settings, task.id).exists()


async def test_an_ended_task_writes_its_line_and_never_the_text_of_its_error(services, held, caplog):
    caplog.set_level(logging.INFO)
    held.auto, held.error = True, ValueError("secret text")
    runner = await runner_for(services, LongLane(3), held)
    task = short_task(services)
    runner.submit(task, None)
    ended = await wait_for_end(runner, task.id)
    assert task_lines(caplog) == [
        (
            f"task={task.id} user=1 mode=raw audio_s=3.0 chunks=- starts=1 retries=0 nemo_ms=0 llm_ms=- "
            "status=failed error=server_error"
        )
    ]
    assert ended.error_message == "The server failed to process the request."
    assert "ValueError" in caplog.text and "secret text" not in caplog.text


async def test_cleanup_removes_expired_tasks_and_old_orphan_folders_only(services, held):
    expired, queued = short_task(services), long_task(services)
    services.store.complete(expired.id, RESULT, NOW - 100_000, 86_400)
    old_orphan = make_folder(services, "trn_orphan_old", age_s=7200)
    decoding = make_folder(services, "trn_being_decoded", age_s=60)
    age_folder(task_dir(services.settings, queued.id), 7200)
    runner = await runner_for(services, LongLane(3), held, start=False)
    await runner.cleanup()
    assert services.store.get(expired.id) is None
    assert not old_orphan.exists() and decoding.exists() and task_dir(services.settings, queued.id).exists()


async def test_cleanup_without_a_task_folder_does_nothing(services, held):
    runner = await runner_for(services, LongLane(3), held, start=False)
    await runner.cleanup()
    assert not services.settings.tasks_dir.exists()


async def test_the_cleanup_loop_runs_at_startup_and_then_again(services, held, monkeypatch):
    monkeypatch.setattr(runner_module, "CLEANUP_EVERY_S", 0.01)
    first, second = short_task(services), short_task(services)
    services.store.complete(first.id, RESULT, NOW - 100_000, 86_400)
    await runner_for(services, LongLane(3), held)
    await wait_until(lambda: services.store.get(first.id) is None)
    services.store.complete(second.id, RESULT, NOW - 100_000, 86_400)
    await wait_until(lambda: services.store.get(second.id) is None)


async def test_the_cleanup_loop_survives_a_failure_and_stops_with_the_runner(services, held, monkeypatch, caplog):
    monkeypatch.setattr(runner_module, "CLEANUP_EVERY_S", 0.01)
    calls: list[int] = []
    delete_expired = services.store.delete_expired

    def flaky(now: int) -> list[str]:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("secret text")
        return delete_expired(now)

    monkeypatch.setattr(services.store, "delete_expired", flaky)
    runner = await runner_for(services, LongLane(3), held)
    await wait_until(lambda: len(calls) >= 3)
    assert "RuntimeError" in caplog.text and "secret text" not in caplog.text
    await runner.stop()
    seen = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == seen


# Les échecs et la ligne de journal.


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (NemoOverloaded("full"), ("overloaded", "The speech engine is busy, try again shortly.")),
        (NemoUnavailable("down"), ("upstream_error", "The speech engine did not answer correctly.")),
        (NemoError("bad glossary term"), ("upstream_error", "The speech engine did not answer correctly.")),
        (ValueError("secret text"), ("server_error", "The server failed to process the request.")),
    ],
)
def test_each_failure_has_a_fixed_code_and_message(error, failure):
    assert failure_of(error) == failure


def test_an_unexpected_failure_is_logged_by_its_class_only(caplog):
    failure_of(ValueError("secret text"))
    assert "ValueError" in caplog.text and "secret text" not in caplog.text


def test_the_task_line_says_who_what_and_how_it_ended_without_any_text(store, caplog):
    caplog.set_level(logging.INFO)
    task = make(store, params=replace(RAW, mode=Mode.CLEAN), duration=65.0)
    task = replace(task, status=TaskStatus.COMPLETED, attempts=1)
    log_task(task, RunStats(chunks=2, retries=1, nemo_ms=1200, llm_ms=300))
    line = caplog.records[-1].getMessage()
    assert line == (
        f"task={task.id} user=1 mode=clean audio_s=65.0 chunks=2 starts=1 retries=1 nemo_ms=1200 llm_ms=300 "
        "status=completed error=-"
    )


def test_the_task_line_names_the_default_account_and_dashes_what_is_missing(store, caplog):
    caplog.set_level(logging.INFO)
    task = make(store, owner=None, duration=3.0)
    task = replace(task, status=TaskStatus.FAILED, attempts=2, error_code="upstream_error")
    log_task(task, RunStats())
    assert caplog.records[-1].getMessage() == (
        f"task={task.id} user=default mode=raw audio_s=3.0 chunks=- starts=2 retries=0 nemo_ms=0 llm_ms=- "
        "status=failed error=upstream_error"
    )
