import asyncio
import io
import math
import os
import struct
import subprocess
import tracemalloc
import wave
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

import pytest
from conftest import make_audio

from dictee import audio
from dictee.audio import (
    OUTPUT_FORMATS,
    AudioDecodeError,
    AudioTooLong,
    atempo_chain,
    decode_wav16k,
    encode_stream,
    pcm_frames,
    read_wav_slice,
)

pytestmark = pytest.mark.anyio

MIB = 1024 * 1024


def sine_pcm(rate: int, seconds: float) -> bytes:
    """PCM16 mono petit-boutiste d'un sinus à 440 Hz."""
    count = int(rate * seconds)
    return struct.pack(f"<{count}h", *(int(12000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(count)))


def numbered_wav(directory: Path, seconds: float) -> Path:
    """WAV canonique mono 16 kHz dont l'échantillon `n` vaut `(n % 20000) - 10000`, pour reconnaître chaque position."""
    path = directory / "numbered.wav"
    count = round(seconds * 16000)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack(f"<{count}h", *((n % 20000) - 10000 for n in range(count))))
    return path


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


DECODED = "decoded.wav"


async def decode(src: Path, **options) -> float:
    """`decode_wav16k` vers `DECODED`, à côté de `src`, avec un plafond de deux heures que le test remplace au besoin.

    Rend la durée de l'audio décodé.
    """
    return await decode_wav16k(src, src.parent / DECODED, **{"max_seconds": 7200.0, **options})


async def test_the_duration_limit_has_no_default(tmp_path):
    with pytest.raises(TypeError, match="max_seconds"):
        await decode_wav16k(make_audio(tmp_path, "tone.wav", seconds=0.5), tmp_path / DECODED)


async def test_converts_any_input_to_mono_16k_wav(tmp_path):
    duration = await decode(make_audio(tmp_path, "tone.mp3", seconds=1.0, rate=48000, channels=2))
    with wave.open(str(tmp_path / DECODED)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
    assert abs(duration - 1.0) < 0.05


async def test_reads_an_m4a_whose_index_is_at_the_end(tmp_path):
    src = make_audio(tmp_path, "tone.m4a", seconds=2.0)  # mp4 sans +faststart
    content = src.read_bytes()
    assert content.find(b"moov") > content.find(b"mdat"), "the test file must keep its index after the data"
    assert abs(await decode(src) - 2.0) < 0.1


@pytest.mark.parametrize("content", [b"", b"not audio at all"])
async def test_unreadable_input_raises(tmp_path, content):
    (tmp_path / "bad").write_bytes(content)
    with pytest.raises(AudioDecodeError) as error:
        await decode(tmp_path / "bad")
    message = str(error.value)
    assert str(tmp_path) not in message
    if content:
        assert content.decode() not in message


async def test_input_without_any_sample_raises(tmp_path):
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
    with pytest.raises(AudioDecodeError):
        await decode(empty)


async def test_wav_header_is_the_one_the_wave_module_writes(tmp_path):
    await decode(make_audio(tmp_path, "tone.wav", seconds=0.5))
    wav = (tmp_path / DECODED).read_bytes()
    expected = io.BytesIO()
    with wave.open(expected, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(wav[44:])
    assert wav == expected.getvalue()


async def test_audio_at_the_limit_is_accepted_and_just_over_it_is_refused(tmp_path):
    src = make_audio(tmp_path, "two_seconds.wav", seconds=2.0)
    assert abs(await decode(src, max_seconds=2.0) - 2.0) < 0.01
    with pytest.raises(AudioTooLong):
        await decode(src, max_seconds=1.9)


async def test_audio_over_the_limit_is_refused_without_being_buffered(tmp_path, spawned):
    # Deux minutes de son, soit 3,84 Mo de PCM une fois décodé, contre un plafond de cinq secondes (160 Ko).
    src = make_audio(tmp_path, "long.flac", seconds=120.0)
    tracemalloc.start()
    try:
        with pytest.raises(AudioTooLong) as error:
            await decode(src, max_seconds=5.0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert issubclass(AudioTooLong, AudioDecodeError)
    assert peak < 1_000_000
    assert [process.returncode is not None for process in spawned] == [True]
    assert str(tmp_path) not in str(error.value)


async def test_on_long_is_called_once_when_the_decoded_audio_passes_the_threshold(tmp_path):
    src = make_audio(tmp_path, "two_seconds.wav", seconds=2.0)
    calls = []
    duration = await decode(src, long_seconds=1.0, on_long=lambda: calls.append(1))
    assert calls == [1]
    assert abs(duration - 2.0) < 0.01  # le rappel n'arrête pas le décodage


@pytest.mark.parametrize(("seconds", "threshold"), [(2.0, 2.5), (0.5, 1.0)])
async def test_on_long_is_not_called_for_audio_under_the_threshold(tmp_path, seconds, threshold):
    src = make_audio(tmp_path, "short.wav", seconds=seconds)
    calls = []
    await decode(src, long_seconds=threshold, on_long=lambda: calls.append(1))
    assert calls == []


async def test_on_long_is_called_right_after_the_read_that_passes_the_threshold(tmp_path, monkeypatch):
    # Lectures de 4 Kio : le rappel part après la première qui dépasse 32 000 octets de PCM, soit une seconde d'audio.
    monkeypatch.setattr(audio, "DECODE_READ_SIZE", 4096)
    decoded = []  # octets rendus par chaque lecture du décodage, relevés sur le tube de ffmpeg
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real(*args, **kwargs)
        read = process.stdout.read

        async def counting(size=-1):
            data = await read(size)
            decoded.append(len(data))
            return data

        process.stdout.read = counting
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    decoded_at_the_call = []
    src = make_audio(tmp_path, "two_seconds.wav", seconds=2.0)
    await decode(src, long_seconds=1.0, on_long=lambda: decoded_at_the_call.append(sum(decoded)))
    assert len(decoded_at_the_call) == 1
    assert 32000 < decoded_at_the_call[0] <= 32000 + 4096


async def test_on_long_that_raises_stops_the_decoding_without_buffering_the_rest(tmp_path, spawned):
    # Deux minutes de son (3,84 Mo de PCM décodé) contre un seuil de cinq secondes (160 Ko) : le rappel refuse.
    src = make_audio(tmp_path, "long.flac", seconds=120.0)

    def refuse():
        raise RuntimeError("no place left")

    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match="no place left"):
            await decode(src, long_seconds=5.0, on_long=refuse)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000
    assert [process.returncode is not None for process in spawned] == [True]


async def test_the_threshold_and_its_callback_go_together(tmp_path):
    src = make_audio(tmp_path, "tone.wav", seconds=0.5)
    with pytest.raises(ValueError, match="on_long"):
        await decode(src, long_seconds=1.0)
    with pytest.raises(ValueError, match="on_long"):
        await decode(src, on_long=lambda: None)


async def test_decoding_that_never_ends_times_out(tmp_path, spawned):
    stuck = tmp_path / "stuck"
    os.mkfifo(stuck)  # ffmpeg attend indéfiniment qu'un écrivain ouvre le tube
    async with asyncio.timeout(30):
        with pytest.raises(AudioDecodeError, match="longer than") as error:
            await decode(stuck, timeout_s=0.5)
    assert [process.returncode is not None for process in spawned] == [True]
    assert str(tmp_path) not in str(error.value)


async def test_cancelling_the_decoding_kills_ffmpeg(tmp_path, spawned):
    stuck = tmp_path / "stuck"
    os.mkfifo(stuck)
    decoding = asyncio.create_task(decode(stuck))
    async with asyncio.timeout(30):
        while not spawned:
            await asyncio.sleep(0.01)
        decoding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await decoding
    assert [process.returncode is not None for process in spawned] == [True]


def decoded_without_restrictions(src: Path) -> int:
    """Octets de PCM 16 kHz mono que ffmpeg tire du fichier sans aucune liste blanche.

    Quand `decode_wav16k` refuse un fichier, ce nombre prouve que la liste blanche est seule en cause : le fichier
    de test est bien formé et ffmpeg le décoderait.
    """
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src)]
    result = subprocess.run([*command, "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"], capture_output=True)
    return len(result.stdout)


@pytest.mark.parametrize("segment", ["segment.aac", "segment.mp3"])
async def test_refuses_an_hls_playlist_that_points_to_a_local_file(tmp_path, segment):
    # Le démuxeur HLS de ffmpeg refuse de lui-même un segment WAV, qui ne prouverait donc rien : les segments sont
    # en aac ou en mp3, qu'il lit.
    make_audio(tmp_path, segment, seconds=1.0)
    playlist = tmp_path / "list.m3u8"
    header = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
    playlist.write_text(f"{header}#EXTINF:1.0,\n{segment}\n#EXT-X-ENDLIST\n")
    assert decoded_without_restrictions(playlist) > 30000
    with pytest.raises(AudioDecodeError, match="not on whitelist"):
        await decode(playlist)


async def test_refuses_a_concat_list_that_points_to_a_local_file(tmp_path):
    make_audio(tmp_path, "segment.wav", seconds=1.0)
    listing = tmp_path / "upload"  # le démuxeur concat se reconnaît à son contenu, pas à l'extension
    listing.write_text("ffconcat version 1.0\nfile segment.wav\n")
    assert decoded_without_restrictions(listing) > 30000
    with pytest.raises(AudioDecodeError, match="not on whitelist"):
        await decode(listing)


@pytest.mark.parametrize(
    "name,codec",
    [
        ("tone.mp3", None),
        ("tone.m4a", None),
        ("tone.mp4", None),
        ("tone.3gp", "aac"),
        ("tone.ogg", "libopus"),
        ("tone.webm", "libopus"),
        ("tone.flac", None),
        ("tone.aac", None),
        ("tone.aiff", None),
        ("tone.caf", None),
        ("tone.w64", None),
        ("tone.wav", None),
    ],
)
async def test_decodes_every_container_the_service_accepts(tmp_path, name, codec):
    assert abs(await decode(make_audio(tmp_path, name, seconds=1.0, codec=codec)) - 1.0) < 0.15


async def test_decodes_an_amr_file(tmp_path):
    # Le conteneur AMR n'a pas d'encodeur dans l'image : le fichier est écrit à la main, une seconde de trames
    # à 4,75 kbit/s (cinquante trames de vingt millisecondes, un octet d'en-tête et douze octets de données).
    amr = tmp_path / "voice.amr"
    amr.write_bytes(b"#!AMR\n" + (b"\x04" + bytes(12)) * 50)
    assert abs(await decode(amr) - 1.0) < 0.15


async def test_decoding_to_a_file_writes_a_canonical_wav_and_gives_its_duration(tmp_path):
    dst = tmp_path / "audio.wav"
    duration = await decode_wav16k(make_audio(tmp_path, "tone.mp3", seconds=3.0), dst, max_seconds=60)
    assert duration == pytest.approx(3.0, abs=0.1)
    with wave.open(str(dst)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
    assert dst.stat().st_size == 44 + round(duration * 32000)


async def test_decoding_to_a_file_stops_at_the_limit_and_leaves_no_file(tmp_path):
    dst = tmp_path / "audio.wav"
    with pytest.raises(AudioTooLong):
        await decode_wav16k(make_audio(tmp_path, "long.mp3", seconds=5.0), dst, max_seconds=2)
    assert not dst.exists()


async def test_an_unreadable_file_leaves_no_decoded_file(tmp_path):
    src = tmp_path / "junk.mp3"
    src.write_bytes(b"not audio")
    with pytest.raises(AudioDecodeError):
        await decode_wav16k(src, tmp_path / "audio.wav", max_seconds=60)
    assert not (tmp_path / "audio.wav").exists()


async def test_a_file_without_any_sample_leaves_no_decoded_file(tmp_path):
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
    with pytest.raises(AudioDecodeError):
        await decode_wav16k(empty, tmp_path / "audio.wav", max_seconds=60)
    assert not (tmp_path / "audio.wav").exists()


async def test_a_refusal_from_on_long_stops_the_decoding_and_leaves_no_file(tmp_path):
    calls = []

    def refuse():
        calls.append(1)
        raise RuntimeError("busy")

    src = make_audio(tmp_path, "five.mp3", seconds=5.0)
    with pytest.raises(RuntimeError):
        await decode_wav16k(src, tmp_path / "audio.wav", max_seconds=60, long_seconds=1, on_long=refuse)
    assert calls == [1] and not (tmp_path / "audio.wav").exists()


async def test_cancelling_the_decoding_to_a_file_leaves_no_file(tmp_path):
    src = make_audio(tmp_path, "minute.flac", seconds=60.0)
    dst = tmp_path / "audio.wav"
    # La tâche s'annule elle-même quand le seuil est franchi : des échantillons sont alors déjà écrits dans `dst`.
    decoding = asyncio.create_task(
        decode_wav16k(src, dst, max_seconds=120, long_seconds=1, on_long=lambda: decoding.cancel())
    )
    with pytest.raises(asyncio.CancelledError):
        await decoding
    assert not dst.exists()


async def test_decoding_memory_does_not_grow_with_the_duration(tmp_path):
    src = make_audio(tmp_path, "minute.flac", seconds=60.0)
    tracemalloc.start()
    await decode_wav16k(src, tmp_path / "audio.wav", max_seconds=120)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 2 * MIB  # une minute décodée pèse 1,9 Mio


def test_a_slice_is_a_wav_of_the_samples_between_its_bounds(tmp_path):
    piece = read_wav_slice(numbered_wav(tmp_path, 3.0), 1.0, 2.5)
    with wave.open(io.BytesIO(piece)) as wav:
        assert (wav.getframerate(), wav.getnframes()) == (16000, 24000)
        first = struct.unpack("<h", wav.readframes(1))[0]
    assert first == 16000 % 20000 - 10000


def test_a_slice_past_the_end_stops_at_the_end(tmp_path):
    with wave.open(io.BytesIO(read_wav_slice(numbered_wav(tmp_path, 3.0), 2.0, 10.0))) as wav:
        assert wav.getnframes() == 16000


def test_pcm_frames_give_the_samples_after_the_header_in_order(tmp_path):
    path = numbered_wav(tmp_path, 2.5)
    frames = list(pcm_frames(path, 32000))
    assert [len(frame) for frame in frames] == [32000, 32000, 16000]
    assert b"".join(frames) == path.read_bytes()[44:]


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
