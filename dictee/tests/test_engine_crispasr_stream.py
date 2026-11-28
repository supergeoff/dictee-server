"""L'adaptateur `crispasr-stream` : un processus CrispASR par session de dictée en direct, joué par un faux binaire."""

import asyncio
import json
import os
import struct
from pathlib import Path

import pytest
from conftest import wait_until

from dictee.engines.crispasr_stream import CrispasrLive, CrispasrLiveSession
from dictee.engines.ports import EngineOverloaded, EngineUnavailable
from dictee.types import Cleared, Committed, Final, Partial

pytestmark = pytest.mark.anyio

FAKE = str(Path(__file__).parent / "fixtures" / "fake_crispasr_stream.py")
RATE = 24000
# Les fichiers des modèles, réels et vides, que la fixture `models` crée pour chaque test.
MODELS = {"model": "", "vad": ""}


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


@pytest.fixture(autouse=True)
def models(tmp_path, monkeypatch):
    """Crée les fichiers du modèle de transcription et du modèle de détection de voix, que l'adaptateur exige."""
    for name, file in (("model", "asr.gguf"), ("vad", "vad.bin")):
        (tmp_path / file).touch()
        monkeypatch.setitem(MODELS, name, str(tmp_path / file))


def engine(command: str = FAKE, max_sessions: int = 2, stream_args=(), model=None, vad=None) -> CrispasrLive:
    return CrispasrLive(
        command,
        model or MODELS["model"],
        vad_model_path=vad or MODELS["vad"],
        max_sessions=max_sessions,
        threads=3,
        stream_args=stream_args,
    )


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

    assert argv == [
        *["--stream", "--stream-json", "--vad", "-vm", MODELS["vad"], "-m", MODELS["model"], "-l", "fr", "-t", "3"],
        *["--stream-step", "1000", "--stream-final-on-silence-ms", "800"],
    ]


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


async def test_missing_vad_model_is_unavailable(scenario, tmp_path):
    scenario([])
    adapter = engine(max_sessions=1, vad=str(tmp_path / "absent.bin"))
    for _ in range(2):  # une place prise à tort ferait lever `EngineOverloaded` au second essai
        with pytest.raises(EngineUnavailable) as caught:
            async with session(adapter):
                pass

    assert "absent" not in str(caught.value)
    assert not (tmp_path / "argv.json").exists()
    assert children() == []


async def test_missing_model_is_unavailable(scenario, tmp_path):
    scenario([])
    adapter = engine(max_sessions=1, model=str(tmp_path / "absent.gguf"))
    for _ in range(2):
        with pytest.raises(EngineUnavailable) as caught:
            async with session(adapter):
                pass

    assert "absent" not in str(caught.value)
    assert not (tmp_path / "argv.json").exists()
    assert children() == []


async def test_missing_ffmpeg_is_unavailable_and_crispasr_never_starts(scenario, tmp_path, monkeypatch):
    scenario([])
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(EngineUnavailable):
        async with session(engine()):
            pass

    assert not (tmp_path / "argv.json").exists()
    assert children() == []


async def test_a_failed_launch_gives_its_slot_back():
    failing = engine(command="/nonexistent/crispasr", max_sessions=1)
    for _ in range(2):  # une place gardée après l'échec ferait lever `EngineOverloaded` au second essai
        with pytest.raises(EngineUnavailable):
            async with session(failing):
                pass


async def test_a_process_that_dies_while_a_commit_waits_is_unavailable(scenario):
    scenario([(["un"], "Un.")], exit_after_bytes=40000)
    async with session(engine()) as live_session:
        await live_session.send_audio(speech(1))
        await live_session.commit()
        with pytest.raises(EngineUnavailable):
            await asyncio.wait_for(until_committed(live_session), 5)

    assert children() == []


async def test_a_line_too_long_stops_the_reading_with_unavailable(scenario):
    scenario([], long_line=True)
    async with session(engine()) as live_session:
        with pytest.raises(EngineUnavailable):
            await asyncio.wait_for(anext(live_session.events()), 5)


async def test_fake_binary_gives_the_final_then_silence_on_the_next_step(scenario):
    """Ordre relevé sur le vrai binaire : le `final` au premier pas dont le silence atteint 800 ms, sans `silence` dans
    ce pas, puis un `silence` par pas suivant."""
    scenario([(["un"], "Un.")])
    process = await asyncio.create_subprocess_exec(
        *[FAKE, "--stream-step", "1000", "--stream-final-on-silence-ms", "800"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate(speech(1, 16000) + silence(3, 16000))
    events = [json.loads(line) for line in out.decode().splitlines()]

    assert [(event["type"], event.get("t", event.get("t1"))) for event in events] == [
        ("partial", 1.0),
        ("final", 2.0),
        ("silence", 3.0),
        ("silence", 4.0),
    ]


class _Stdin:
    """Entrée de ffmpeg factice : elle accepte tout ce que la session écrit."""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class _Process:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()


async def scripted_session() -> tuple[CrispasrLiveSession, asyncio.StreamReader]:
    """Une session sur des processus factices : le test écrit lui-même, dans l'ordre voulu, ce que CrispASR répond."""
    crispasr = _Process()
    live_session = CrispasrLiveSession(_Process(), crispasr, RATE)
    return live_session, crispasr.stdout


async def feed(stdout: asyncio.StreamReader, *events: dict) -> None:
    """Donne ces événements à la session et la laisse les traiter."""
    for event in events:
        stdout.feed_data((json.dumps(event) + "\n").encode())
    for _ in range(3):
        await asyncio.sleep(0)


async def stop(live_session: CrispasrLiveSession) -> None:
    live_session._reader.cancel()
    await asyncio.gather(live_session._reader, return_exceptions=True)


def queued(live_session: CrispasrLiveSession) -> list:
    events = []
    while not live_session._queue.empty():
        events.append(live_session._queue.get_nowait())
    return events


def partial(utterance: int, text: str, t0: float) -> dict:
    return {"type": "partial", "utterance_id": utterance, "text": text, "t0": t0, "t1": t0 + 1}


async def test_clear_after_an_unclosed_commit_keeps_its_final():
    live_session, stdout = await scripted_session()
    await live_session.send_audio(speech(1))
    await live_session.commit()  # validation au temps 1 s, close par un `silence` à 2,8 s au moins
    await live_session.send_audio(speech(1))
    await live_session.clear()  # vidage au temps 5 s, avant que la validation soit close
    await feed(
        stdout,
        {"type": "final", "utterance_id": 1, "text": "Gardé.", "t0": 0.0, "t1": 2.0},
        {"type": "silence", "t": 3.0},
        {"type": "final", "utterance_id": 2, "text": "Vidé.", "t0": 4.0, "t1": 6.0},
    )

    assert queued(live_session) == [Cleared(), Final("Gardé.", ()), Committed()]
    await stop(live_session)


async def test_clear_after_an_unclosed_commit_does_not_repeat_its_words():
    live_session, stdout = await scripted_session()
    await live_session.send_audio(speech(3))
    await feed(stdout, partial(1, "un", 0.0), partial(1, "un deux", 0.0))
    await live_session.commit()
    await live_session.send_audio(speech(1))
    await live_session.clear()
    await feed(stdout, partial(1, "un deux trois", 0.0), partial(1, "un deux trois", 0.0))

    assert [event for event in queued(live_session) if isinstance(event, Partial)] == [
        Partial("un"),
        Partial(" deux"),
        Partial(" trois"),
    ]
    await stop(live_session)
