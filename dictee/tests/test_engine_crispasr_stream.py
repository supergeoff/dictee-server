"""L'adaptateur `crispasr-stream` : un processus CrispASR par session de dictée en direct, joué par un faux binaire."""

import asyncio
import json
import os
import struct
from pathlib import Path

import pytest
from conftest import wait_until

from dictee.engines.crispasr_stream import CrispasrLive
from dictee.engines.ports import EngineOverloaded, EngineUnavailable
from dictee.types import Cleared, Committed, Final, Partial

pytestmark = pytest.mark.anyio

FAKE = str(Path(__file__).parent / "fixtures" / "fake_crispasr_stream.py")
RATE = 24000


def speech(seconds: float, rate: int = RATE) -> bytes:
    """`seconds` secondes d'un signal constant, que le faux binaire prend pour de la parole."""
    return struct.pack("<h", 8000) * round(seconds * rate)


def silence(seconds: float, rate: int = RATE) -> bytes:
    return bytes(2 * round(seconds * rate))


def children() -> list[int]:
    """Les processus fils du processus de test, zombies compris : un fils tué mais pas attendu compte encore."""
    pids = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path(f"/proc/{entry}/stat").read_text()
            except OSError:
                continue
            if int(stat.rpartition(")")[2].split()[1]) == os.getpid():
                pids.append(int(entry))
    return pids


@pytest.fixture
def scenario(monkeypatch, tmp_path):
    """Règle le faux binaire : `scenario(utterances)` pose son scénario ; ses fichiers d'audio et d'arguments."""
    monkeypatch.setenv("FAKE_CRISPASR_AUDIO", str(tmp_path / "audio.raw"))
    monkeypatch.setenv("FAKE_CRISPASR_ARGV", str(tmp_path / "argv.json"))

    def set_scenario(utterances=(), **extra) -> None:
        body = {"utterances": [{"partials": list(p), "final": f} for p, f in utterances], **extra}
        monkeypatch.setenv("FAKE_CRISPASR_SCENARIO", json.dumps(body))

    return set_scenario


def engine(command: str = FAKE, max_sessions: int = 2, stream_args=()) -> CrispasrLive:
    return CrispasrLive(command, "/models/asr.gguf", max_sessions=max_sessions, threads=3, stream_args=stream_args)


def session(adapter: CrispasrLive, language: str = "fr-FR"):
    return adapter.live(sample_rate=RATE, language=language, phrases=["Nemotron"], boost=1.0)


async def until_committed(live_session) -> list:
    """Les événements de la session jusqu'au premier `Committed` compris."""
    events = []
    async for event in live_session.events():
        events.append(event)
        if isinstance(event, Committed):
            return events
    raise AssertionError(f"no Committed: {events}")


async def dictate(scenario, utterances, audio: bytes) -> list:
    scenario(utterances)
    async with session(engine()) as live_session:
        await live_session.send_audio(audio)
        await live_session.commit()
        return await asyncio.wait_for(until_committed(live_session), 5)


async def test_command_line_follows_the_settings(scenario, tmp_path):
    await dictate(scenario, [], silence(0.5))
    argv = json.loads((tmp_path / "argv.json").read_text())

    expected = "--stream --stream-json --vad -m /models/asr.gguf -l fr -t 3"
    assert argv == [*expected.split(), "--stream-step", "1000", "--stream-final-on-silence-ms", "800"]


async def test_stream_args_come_last_and_auto_stays_auto(scenario, tmp_path):
    scenario([])
    async with engine(stream_args=["--stream-length", "8000"]).live(
        sample_rate=RATE, language="auto", phrases=[], boost=1.0
    ) as live_session:
        await live_session.commit()
        await asyncio.wait_for(until_committed(live_session), 5)
    argv = json.loads((tmp_path / "argv.json").read_text())

    assert argv[argv.index("-l") + 1] == "auto"
    assert argv[-2:] == ["--stream-length", "8000"]


async def test_partial_emits_only_stable_new_words(scenario):
    texts = ["bonjour", "bonjour à", "bonjour à tous", "bonjour à tous"]
    events = await dictate(scenario, [(texts, "Bonjour à tous.")], speech(4))

    assert [event for event in events if isinstance(event, Partial)] == [
        Partial("bonjour"),
        Partial(" à"),
        Partial(" tous"),
    ]


async def test_partials_ignore_punctuation_and_case_and_take_the_latest_form(scenario):
    texts = ["Bonjour.", "Bonjour, je", "bonjour je teste"]
    events = await dictate(scenario, [(texts, "Bonjour, je teste.")], speech(3))

    assert [event for event in events if isinstance(event, Partial)] == [Partial("Bonjour,"), Partial(" je")]


async def test_revised_partial_never_retracts(scenario):
    texts = ["bonjour", "bonjour à", "bonjour à tous", "bonsoir à tous"]
    events = await dictate(scenario, [(texts, "Bonsoir à tous.")], speech(4))

    assert [event for event in events if isinstance(event, Partial)] == [Partial("bonjour"), Partial(" à")]


async def test_final_event_becomes_final(scenario):
    events = await dictate(scenario, [(["bonjour"], "Bonjour à tous.")], speech(1))

    assert events[-2:] == [Final("Bonjour à tous.", ()), Committed()]


async def test_commit_sends_silence_then_committed(scenario, tmp_path):
    events = await dictate(scenario, [(["bonjour"], "Bonjour.")], speech(1))
    audio = (tmp_path / "audio.raw").read_bytes()
    samples = struct.unpack(f"<{len(audio) // 2}h", audio)
    last_loud = max(index for index, sample in enumerate(samples) if abs(sample) > 100)

    assert events == [Final("Bonjour.", ()), Committed()]
    assert len(samples) - last_loud >= 2.8 * 16000


async def test_commit_without_speech_emits_committed_only(scenario):
    events = await dictate(scenario, [(["bonjour"], "Bonjour.")], silence(1))

    assert events == [Committed()]


async def test_finals_before_commit_wait_for_the_commit(scenario):
    scenario([(["un", "un"], "Un."), (["deux", "deux"], "Deux.")])
    async with session(engine()) as live_session:
        await live_session.send_audio(speech(2) + silence(1.5) + speech(2) + silence(1.5))
        events = live_session.events()
        before = [await asyncio.wait_for(anext(events), 5) for _ in range(2)]
        await live_session.commit()
        after = await asyncio.wait_for(until_committed(live_session), 5)

    assert before == [Partial("un"), Partial(" deux")]
    assert after == [Final("Un.", ()), Final("Deux.", ()), Committed()]


async def test_turns_after_a_commit_start_without_a_space(scenario):
    scenario([(["un", "un"], "Un."), (["deux", "deux"], "Deux.")])
    async with session(engine()) as live_session:
        for _ in range(2):
            await live_session.send_audio(speech(2))
            await live_session.commit()
        first = await asyncio.wait_for(until_committed(live_session), 5)
        second = await asyncio.wait_for(until_committed(live_session), 5)

    assert first == [Partial("un"), Final("Un.", ()), Committed()]
    assert second == [Partial("deux"), Final("Deux.", ()), Committed()]


async def test_clear_drops_the_turn_and_emits_cleared(scenario):
    scenario([(["oublié", "oublié"], "Oublié."), (["gardé"], "Gardé.")])
    async with session(engine()) as live_session:
        await live_session.send_audio(speech(1))
        await live_session.clear()
        await live_session.send_audio(speech(1))
        await live_session.commit()
        events = await asyncio.wait_for(until_committed(live_session), 5)

    assert events == [Cleared(), Final("Gardé.", ()), Committed()]


async def test_audio_reaches_the_process_at_16khz(scenario, tmp_path):
    await dictate(scenario, [], speech(1))
    audio = (tmp_path / "audio.raw").read_bytes()
    samples = struct.unpack(f"<{len(audio) // 2}h", audio)
    loud = sum(1 for sample in samples if abs(sample) > 100)

    assert abs(2 * loud - 32000) <= 320


async def test_third_session_is_overloaded_then_slot_frees(scenario):
    scenario([])
    adapter = engine(max_sessions=2)
    async with session(adapter), session(adapter):
        with pytest.raises(EngineOverloaded):
            async with session(adapter):
                pass
    async with session(adapter) as live_session:
        await live_session.commit()
        assert await asyncio.wait_for(until_committed(live_session), 5) == [Committed()]


async def test_process_is_killed_when_session_closes(scenario):
    scenario([])
    async with session(engine()) as live_session:
        await live_session.send_audio(speech(1))
        assert len(children()) == 2  # ffmpeg et le faux binaire

    assert children() == []


async def test_process_is_killed_when_cancelled(scenario):
    scenario([])
    opened = asyncio.Event()

    async def run() -> None:
        async with session(engine()) as live_session:
            await live_session.send_audio(speech(1))
            opened.set()
            async for _ in live_session.events():
                pass

    task = asyncio.create_task(run())
    await asyncio.wait_for(opened.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert children() == []


async def test_process_is_killed_when_the_block_raises(scenario):
    scenario([])
    with pytest.raises(RuntimeError):
        async with session(engine()):
            raise RuntimeError("boom")

    assert children() == []


async def test_missing_binary_is_unavailable(scenario):
    with pytest.raises(EngineUnavailable):
        async with session(engine(command="/nonexistent/crispasr")):
            pass

    assert children() == []


async def test_a_process_that_stops_is_unavailable(scenario):
    scenario(exit=1)
    async with session(engine()) as live_session:
        with pytest.raises(EngineUnavailable):
            await asyncio.wait_for(anext(live_session.events()), 5)

    assert children() == []


async def test_a_closed_session_frees_its_slot_even_after_a_failure(scenario):
    scenario(exit=1)
    adapter = engine(max_sessions=1)
    for _ in range(2):
        async with session(adapter) as live_session:
            with pytest.raises(EngineUnavailable):
                await asyncio.wait_for(anext(live_session.events()), 5)


async def test_capabilities():
    adapter = engine()

    assert adapter.server_vad is False
    assert adapter.many_commits is True


async def test_children_helper_sees_a_running_process():
    process = await asyncio.create_subprocess_exec("sleep", "5")
    try:
        await wait_until(lambda: process.pid in children())
    finally:
        process.kill()
        await process.wait()
