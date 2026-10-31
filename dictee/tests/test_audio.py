import asyncio
import io
import math
import struct
import wave
from collections.abc import AsyncIterator
from contextlib import aclosing

import pytest
from conftest import make_audio

from dictee.audio import (
    OUTPUT_FORMATS,
    AudioDecodeError,
    atempo_chain,
    encode_stream,
    to_wav16k,
    wav_duration,
    wav_pcm,
)

pytestmark = pytest.mark.anyio


def sine_pcm(rate: int, seconds: float) -> bytes:
    """PCM16 mono petit-boutiste d'un sinus à 440 Hz."""
    count = int(rate * seconds)
    return struct.pack(f"<{count}h", *(int(12000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(count)))


async def chunks(data: bytes, size: int = 4096) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def collect(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([part async for part in stream])


async def endless_sine() -> AsyncIterator[bytes]:
    """Entrée sans fin, pour les tests où le consommateur s'arrête avant elle."""
    while True:
        yield sine_pcm(22050, 0.1)
        await asyncio.sleep(0.01)


@pytest.fixture
def spawned(monkeypatch):
    """Les processus que le code lance, pour vérifier qu'aucun ne survit au consommateur."""
    processes = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return processes


async def test_converts_any_input_to_mono_16k_wav(tmp_path):
    wav = await to_wav16k(make_audio(tmp_path, "tone.mp3", seconds=1.0, rate=48000, channels=2))
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
    assert abs(wav_duration(wav) - 1.0) < 0.05


async def test_reads_an_m4a_whose_index_is_at_the_end(tmp_path):
    src = make_audio(tmp_path, "tone.m4a", seconds=2.0)  # mp4 sans +faststart
    content = src.read_bytes()
    assert content.find(b"moov") > content.find(b"mdat"), "the test file must keep its index after the data"
    wav = await to_wav16k(src)
    assert abs(wav_duration(wav) - 2.0) < 0.1


async def test_wav_pcm_returns_the_samples_without_the_header(tmp_path):
    wav = await to_wav16k(make_audio(tmp_path, "tone.wav", seconds=1.0))
    assert len(wav_pcm(wav)) == round(wav_duration(wav) * 16000) * 2
    assert len(wav) == len(wav_pcm(wav)) + 44


@pytest.mark.parametrize("content", [b"", b"not audio at all"])
async def test_unreadable_input_raises(tmp_path, content):
    (tmp_path / "bad").write_bytes(content)
    with pytest.raises(AudioDecodeError):
        await to_wav16k(tmp_path / "bad")


async def test_input_without_any_sample_raises(tmp_path):
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
    with pytest.raises(AudioDecodeError):
        await to_wav16k(empty)


def test_atempo_chain():
    assert atempo_chain(1.0) == ""
    assert atempo_chain(2.0) == "atempo=2.0"
    assert atempo_chain(0.25) == "atempo=0.5,atempo=0.5"
    assert atempo_chain(4.0) == "atempo=2.0,atempo=2.0"
    with pytest.raises(ValueError):
        atempo_chain(5.0)


@pytest.mark.parametrize("speed", [0.24, 4.01, 0.0, -1.0, float("nan")])
def test_atempo_chain_rejects_speeds_out_of_range(speed):
    with pytest.raises(ValueError):
        atempo_chain(speed)


@pytest.mark.parametrize(
    "speed,expected",
    [(0.5, "atempo=0.5"), (1.5, "atempo=1.5"), (3.0, "atempo=2.0,atempo=1.5"), (0.3, "atempo=0.5,atempo=0.6")],
)
def test_atempo_chain_keeps_each_stage_within_ffmpeg_limits(speed, expected):
    assert atempo_chain(speed) == expected


def test_output_formats_content_types():
    assert OUTPUT_FORMATS == {
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/pcm",
    }


@pytest.mark.parametrize(
    "fmt,magics",
    [
        ("mp3", (b"ID3", b"\xff\xfb", b"\xff\xf3")),
        ("opus", (b"OggS",)),
        ("aac", (b"\xff\xf1", b"\xff\xf9")),
        ("flac", (b"fLaC",)),
        ("wav", (b"RIFF",)),
    ],
)
async def test_encodes_each_format(fmt, magics):
    out = await collect(encode_stream(chunks(sine_pcm(22050, 1.0)), fmt=fmt, speed=1.0, input_rate=22050))
    assert out.startswith(magics)


async def test_unknown_format_raises():
    with pytest.raises(ValueError):
        await collect(encode_stream(chunks(sine_pcm(22050, 0.1)), fmt="ogg", speed=1.0, input_rate=22050))


async def test_pcm_is_resampled_to_24k():
    out = await collect(encode_stream(chunks(sine_pcm(22050, 1.0)), fmt="pcm", speed=1.0, input_rate=22050))
    assert abs(len(out) - 24000 * 2 * 1.0) < 24000 * 2 * 0.02


async def test_speed_two_halves_the_duration():
    out = await collect(encode_stream(chunks(sine_pcm(22050, 1.0)), fmt="pcm", speed=2.0, input_rate=22050))
    assert abs(len(out) - 24000 * 2 * 0.5) < 24000 * 2 * 0.5 * 0.05


async def test_first_output_arrives_before_the_input_ends():
    first_output = asyncio.Event()
    pcm = sine_pcm(22050, 1.0)
    half = len(pcm) // 2

    async def source() -> AsyncIterator[bytes]:
        yield pcm[:half]
        await first_output.wait()  # le second morceau n'existe qu'une fois le premier son sorti
        yield pcm[half:]

    received = b""
    async with asyncio.timeout(15):
        async for part in encode_stream(source(), fmt="mp3", speed=1.0, input_rate=22050):
            received += part
            first_output.set()
    assert received.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3"))


async def test_closing_the_stream_early_kills_ffmpeg(spawned):
    silence = bytes(22050 * 2 * 20)  # vingt secondes : bien plus que ce que les tubes retiennent
    tasks_before = asyncio.all_tasks()
    stream = encode_stream(chunks(silence), fmt="pcm", speed=1.0, input_rate=22050)
    async with asyncio.timeout(15), aclosing(stream):
        async for _ in stream:
            break
        await asyncio.sleep(0.5)  # personne ne lit plus : ffmpeg se bloque sur l'écriture de sa sortie
    assert [process.returncode is not None for process in spawned] == [True]
    assert asyncio.all_tasks() == tasks_before


async def test_cancelling_the_consumer_kills_ffmpeg(spawned):
    first_output = asyncio.Event()

    async def consume():
        async for _ in encode_stream(endless_sine(), fmt="pcm", speed=1.0, input_rate=22050):
            first_output.set()

    tasks_before = asyncio.all_tasks()
    consumer = asyncio.create_task(consume())
    async with asyncio.timeout(15):
        await first_output.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert [process.returncode is not None for process in spawned] == [True]
    assert asyncio.all_tasks() == tasks_before


async def test_input_failure_reaches_the_consumer():
    async def failing() -> AsyncIterator[bytes]:
        yield sine_pcm(22050, 0.1)
        raise ConnectionError("synthesis dropped")

    with pytest.raises(ConnectionError, match="synthesis dropped"):
        await collect(encode_stream(failing(), fmt="wav", speed=1.0, input_rate=22050))


async def test_ffmpeg_failure_reaches_the_consumer_without_waiting_for_the_input():
    async def stalled() -> AsyncIterator[bytes]:
        yield sine_pcm(22050, 0.1)
        await asyncio.Event().wait()  # la synthèse ne rend plus rien

    async with asyncio.timeout(15):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            await collect(encode_stream(stalled(), fmt="mp3", speed=1.0, input_rate=-1))  # ffmpeg refuse de démarrer
