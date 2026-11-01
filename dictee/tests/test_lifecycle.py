import gc
import logging
import time

import anyio
import pytest
from conftest import wait_until
from starlette.requests import Request

from dictee.lifecycle import Trace, cancel_once, elapsed_ms, loggable, run_unless_disconnected, spawn


def make_request(receive) -> Request:
    return Request({"type": "http", "method": "POST", "headers": []}, receive)


def receive_after(hung_up: anyio.Event, *early: dict):
    """`receive` ASGI : les messages `early`, puis `http.disconnect` une fois `hung_up` posé."""
    messages = list(early)

    async def receive():
        if messages:
            return messages.pop(0)
        await hung_up.wait()
        return {"type": "http.disconnect"}

    return receive


def lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "dictee"]


def test_trace_line_names_the_route_and_leaves_unknown_fields_as_dashes(caplog):
    caplog.set_level(logging.INFO, logger="dictee")
    Trace(route="speech").log()
    assert lines(caplog) == [
        "user=- route=speech model=- mode=- audio_s=- nemo_ms=- llm_ms=- decision=- mismatch=- status=500"
    ]


def test_trace_line_writes_each_field(caplog):
    caplog.set_level(logging.INFO, logger="dictee")
    trace = Trace(route="transcriptions", user="7", model="dictee", mode="clean", audio_s=3.04, nemo_ms=120)
    trace.llm_ms, trace.decision, trace.mismatch, trace.status = 45, "cleaned", 2, 200
    trace.log()
    assert lines(caplog) == [
        "user=7 route=transcriptions model=dictee mode=clean audio_s=3.0 nemo_ms=120 llm_ms=45"
        " decision=cleaned mismatch=2 status=200"
    ]


def test_trace_line_writes_a_mismatch_count_of_zero(caplog):
    caplog.set_level(logging.INFO, logger="dictee")
    trace = Trace(route="transcriptions", mismatch=0)
    trace.log()
    assert "mismatch=0 status=500" in lines(caplog)[0]


def test_trace_writes_its_line_once(caplog):
    caplog.set_level(logging.INFO, logger="dictee")
    trace = Trace(route="speech")
    trace.log()
    trace.status = 200
    trace.log()
    (line,) = lines(caplog)
    assert line.endswith("status=500") and trace.logged


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("whisper-1", "whisper-1"),
        ("org/model:7b_q4.1", "org/model:7b_q4.1"),
        ("a b\nuser=1", "a?b?user?1"),
        ("x" * 100, "x" * 64),
        ("", "-"),
        (None, "-"),
    ],
)
def test_loggable_keeps_a_client_value_on_one_line(raw, expected):
    assert loggable(raw) == expected


def test_elapsed_ms_counts_milliseconds_from_a_monotonic_reading():
    assert 1500 <= elapsed_ms(time.monotonic() - 1.5) < 1600


@pytest.mark.anyio
async def test_spawned_task_finishes_its_cleanup_after_a_single_cancel():
    started, cleaned = anyio.Event(), []

    async def work():
        try:
            started.set()
            await anyio.sleep_forever()
        finally:
            await anyio.sleep(0)  # un nettoyage qui attend, comme la fermeture de la session de NeMo
            await anyio.sleep(0)
            cleaned.append(True)

    task = spawn(work())
    await started.wait()
    task.cancel()
    await wait_until(lambda: cleaned)


@pytest.mark.anyio
async def test_cancel_once_leaves_the_cleanup_of_a_cancelled_task_alone():
    started, cleaned = anyio.Event(), []

    async def work():
        try:
            started.set()
            await anyio.sleep_forever()
        finally:
            await anyio.sleep(0)  # un second `Task.cancel` interromprait le nettoyage ici
            cleaned.append(True)

    task = spawn(work())
    await started.wait()
    cancel_once(task)
    await anyio.sleep(0)  # la tâche entame son nettoyage
    cancel_once(task)
    await wait_until(task.done)
    assert cleaned


@pytest.mark.anyio
async def test_cancel_once_ignores_a_finished_task():
    async def work():
        return 1

    task = spawn(work())
    await wait_until(task.done)
    cancel_once(task)
    assert task.result() == 1


@pytest.mark.anyio
async def test_spawned_task_that_fails_logs_nothing(caplog):
    # Une tâche en échec que personne ne lit fait journaliser son exception par asyncio, message compris.
    marker = "Sortformer-marker-5c1e07"

    async def fail():
        raise RuntimeError(marker)

    caplog.set_level(logging.DEBUG)
    task = spawn(fail())
    await wait_until(task.done)
    del task
    gc.collect()
    assert marker not in caplog.text


@pytest.mark.anyio
async def test_run_unless_disconnected_returns_the_result_of_the_coroutine():
    async def job():
        return 42

    receive = receive_after(anyio.Event(), {"type": "http.request", "body": b"", "more_body": False})
    assert await run_unless_disconnected(make_request(receive), job()) == 42


@pytest.mark.anyio
async def test_run_unless_disconnected_cancels_the_coroutine_when_the_client_hangs_up():
    hung_up, cleaned = anyio.Event(), anyio.Event()
    results = []

    async def job():
        try:
            await anyio.sleep_forever()
        finally:
            await anyio.sleep(0)
            cleaned.set()

    receive = receive_after(hung_up, {"type": "http.request", "body": b"", "more_body": False})

    async def run():
        results.append(await run_unless_disconnected(make_request(receive), job()))

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await anyio.sleep(0.05)
        assert results == []
        hung_up.set()
    assert results == [None] and cleaned.is_set()


@pytest.mark.anyio
async def test_run_unless_disconnected_raises_the_exception_of_the_coroutine():
    async def job():
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        await run_unless_disconnected(make_request(receive_after(anyio.Event())), job())
