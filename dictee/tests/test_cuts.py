import asyncio
import math
import os
import random
import struct
import wave
from collections.abc import Sequence
from pathlib import Path

import pytest

from dictee import cuts
from dictee.audio import AudioDecodeError
from dictee.cuts import MAX_CHUNK_S, Silence, chunk_bounds, detect_silences, plan_cuts

pytestmark = pytest.mark.anyio

RATE = 16000


def wav_with_silences(directory: Path, seconds: float, silences: Sequence[tuple[float, float]]) -> Path:
    """WAV canonique mono 16 kHz : un sinus d'amplitude 8000, remplacé par des zéros dans chaque intervalle."""
    count = round(seconds * RATE)
    # 400 Hz fait un nombre entier de périodes par seconde : les secondes se mettent bout à bout sans raccord.
    one_second = struct.pack(f"<{RATE}h", *(round(8000 * math.sin(2 * math.pi * 400 * n / RATE)) for n in range(RATE)))
    pcm = bytearray((one_second * math.ceil(seconds))[: count * 2])
    for start, end in silences:
        first, last = round(start * RATE), round(end * RATE)
        pcm[first * 2 : last * 2] = bytes((last - first) * 2)
    path = directory / "silences.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return path


def random_silences(rng: random.Random, duration: float) -> list[Silence]:
    """Silences triés et disjoints de 0,3 à 2 s, séparés de 0 à 50 s : certaines fenêtres de coupe n'en ont aucun."""
    silences = []
    position = rng.uniform(0.0, 20.0)
    while True:
        length = rng.uniform(0.3, 2.0)
        if position + length > duration:
            return silences
        silences.append(Silence(position, position + length))
        position += length + rng.uniform(0.0, 50.0)


@pytest.fixture
def spawned(monkeypatch):
    """Les processus que le code lance, pour vérifier qu'aucun ne survit à la fonction."""
    processes = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return processes


def finished(processes) -> list[bool]:
    """Pour chaque processus : ffmpeg est terminé et sa sortie d'erreur a été lue jusqu'au bout, donc son tube fermé."""
    return [process.returncode is not None and process.stderr.at_eof() for process in processes]


def fake_ffmpeg(directory: Path, monkeypatch, stderr: str) -> None:
    """Place devant le vrai ffmpeg un script qui écrit `stderr` sur sa sortie d'erreur et s'arrête sans erreur."""
    (directory / "ffmpeg").write_text(f"#!/bin/sh\ncat >&2 <<'END'\n{stderr}\nEND\n")
    (directory / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def test_the_cut_falls_in_the_silence_closest_to_30_seconds():
    silences = [Silence(21.0, 21.6), Silence(29.0, 29.4), Silence(33.0, 34.0)]
    assert plan_cuts(65.0, silences) == pytest.approx([29.2])


def test_without_silence_between_20_and_40_seconds_the_cut_is_at_30():
    assert plan_cuts(65.0, [Silence(10.0, 11.0), Silence(45.0, 46.0)]) == [30.0]


def test_a_tie_goes_to_the_earlier_silence():
    # Les milieux, 28 et 32 s, sont à 2 s de la cible. L'audio dure 65 s et non 70 : après une coupe à 28 s, il en
    # resterait 42, plus que le maximum d'un morceau, et une seconde coupe s'ajouterait.
    assert plan_cuts(65.0, [Silence(27.9, 28.1), Silence(31.9, 32.1)]) == pytest.approx([28.0])


def test_each_chunk_looks_from_its_own_start():
    assert plan_cuts(95.0, [Silence(31.0, 31.2), Silence(58.0, 59.0)]) == pytest.approx([31.1, 58.5])


@pytest.mark.parametrize(("middle", "cut"), [(19.9, 30.0), (20.0, 20.0), (40.0, 40.0), (40.1, 30.0)])
def test_a_cut_falls_between_20_and_40_seconds_after_the_chunk_start(middle, cut):
    # Un silence réduit à un point : son milieu est exactement `middle`.
    assert plan_cuts(90.0, [Silence(middle, middle)])[0] == cut


def test_the_order_of_the_silences_does_not_matter():
    silences = [Silence(31.9, 32.1), Silence(27.9, 28.1), Silence(21.0, 21.6)]
    assert plan_cuts(65.0, silences) == pytest.approx([28.0])


def test_audio_of_40_seconds_or_less_is_one_chunk():
    assert plan_cuts(40.0, []) == []
    assert chunk_bounds(12.0, []) == [(0.0, 12.0)]


def test_chunks_cover_the_audio_without_gap_or_overlap():
    rng = random.Random(7)
    for _ in range(200):
        duration = rng.uniform(1.0, 7200.0)
        bounds = chunk_bounds(duration, plan_cuts(duration, random_silences(rng, duration)))
        assert bounds[0][0] == 0.0 and bounds[-1][1] == duration
        assert all(left[1] == right[0] for left, right in zip(bounds, bounds[1:]))
        assert all(0 < end - start <= MAX_CHUNK_S for start, end in bounds)


async def test_detect_silences_finds_the_silent_stretches(tmp_path):
    path = wav_with_silences(tmp_path, 30.0, [(10.0, 11.0), (20.0, 20.2)])
    [silence] = await detect_silences(path, 30.0)
    assert silence.start == pytest.approx(10.0, abs=0.05)
    assert silence.end == pytest.approx(11.0, abs=0.05)


async def test_a_silence_that_lasts_until_the_end_closes_at_the_duration(tmp_path):
    path = wav_with_silences(tmp_path, 12.0, [(10.0, 12.0)])
    assert (await detect_silences(path, 12.0))[-1].end == 12.0


async def test_a_silence_still_open_when_ffmpeg_ends_closes_at_the_duration(tmp_path, monkeypatch):
    # ffmpeg 7 écrit lui-même la fin d'un silence qui dure jusqu'au bout du fichier ; les versions plus anciennes non.
    fake_ffmpeg(tmp_path, monkeypatch, "[silencedetect @ 0x1] silence_start: 10.5")
    assert await detect_silences(tmp_path / "unused.wav", 12.0) == [Silence(10.5, 12.0)]


async def test_only_the_lines_of_the_filter_count(tmp_path, monkeypatch):
    fake_ffmpeg(
        tmp_path,
        monkeypatch,
        "Input #0, wav, from 'unused.wav':\n"
        "  title : [silencedetect @ 0x1] silence_start: 99\n"  # une métadonnée du fichier, indentée
        "[silencedetect @ 0x1] silence_end: 3 | silence_duration: 1\n"  # une fin sans début
        "[silencedetect @ 0x1] silence_start: 5\n"
        "[silencedetect @ 0x1] silence_end: 6.000063 | silence_duration: 1.000063\n"
        "size=N/A time=00:00:12.00 bitrate=N/A speed=5e+03x",
    )
    assert await detect_silences(tmp_path / "unused.wav", 12.0) == [Silence(5.0, 6.000063)]


async def test_detect_silences_of_an_unreadable_file_raises(tmp_path):
    path = tmp_path / "junk.wav"
    path.write_bytes(b"not audio")
    with pytest.raises(AudioDecodeError):
        await detect_silences(path, 1.0)


async def test_the_error_for_an_unreadable_file_shows_neither_its_path_nor_its_content(tmp_path):
    path = tmp_path / "junk.wav"
    path.write_bytes(b"not audio at all")
    with pytest.raises(AudioDecodeError) as error:
        await detect_silences(path, 1.0)
    assert str(tmp_path) not in str(error.value)
    assert "not audio" not in str(error.value)


async def test_silences_are_found_whatever_the_size_of_the_reads(tmp_path, monkeypatch):
    # Des lectures de 7 octets coupent chacune des lignes de ffmpeg en plusieurs morceaux.
    monkeypatch.setattr(cuts, "READ_SIZE", 7)
    path = wav_with_silences(tmp_path, 30.0, [(5.0, 6.0), (20.0, 22.0)])
    found = await detect_silences(path, 30.0)
    assert [silence.start for silence in found] == pytest.approx([5.0, 20.0], abs=0.05)
    assert [silence.end for silence in found] == pytest.approx([6.0, 22.0], abs=0.05)


async def test_every_silence_of_a_long_run_is_found_in_order(tmp_path):
    # 200 silences de 0,4 s, une par seconde : la sortie de ffmpeg dépasse de loin une lecture.
    path = wav_with_silences(tmp_path, 200.0, [(n + 0.3, n + 0.7) for n in range(200)])
    found = await detect_silences(path, 200.0)
    assert [silence.start for silence in found] == pytest.approx([n + 0.3 for n in range(200)], abs=0.05)
    assert [silence.end for silence in found] == pytest.approx([n + 0.7 for n in range(200)], abs=0.05)


async def test_cuts_planned_on_detected_silences_fall_in_the_silences(tmp_path):
    path = wav_with_silences(tmp_path, 100.0, [(28.0, 29.0), (58.0, 59.0)])
    cut_points = plan_cuts(100.0, await detect_silences(path, 100.0))
    assert cut_points == pytest.approx([28.5, 58.5, 88.5], abs=0.05)  # la troisième : coupe nette, sans silence


async def test_a_scan_that_never_ends_times_out(tmp_path, spawned):
    stuck = tmp_path / "stuck.wav"
    os.mkfifo(stuck)  # ffmpeg attend indéfiniment qu'un écrivain ouvre le tube
    async with asyncio.timeout(30):
        with pytest.raises(AudioDecodeError, match="longer than") as error:
            await detect_silences(stuck, 1.0, timeout_s=0.5)
    assert finished(spawned) == [True]
    assert str(tmp_path) not in str(error.value)


async def test_cancelling_the_scan_kills_ffmpeg(tmp_path, spawned):
    stuck = tmp_path / "stuck.wav"
    os.mkfifo(stuck)
    scan = asyncio.create_task(detect_silences(stuck, 1.0))
    async with asyncio.timeout(30):
        while not spawned:
            await asyncio.sleep(0.01)
        scan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scan
    assert finished(spawned) == [True]


async def test_a_timeout_while_nobody_reads_ffmpeg_still_closes_its_pipes(tmp_path, spawned, monkeypatch):
    # 3000 silences font près de 400 Ko de sortie d'erreur : assez pour remplir le tube et mettre sa lecture en pause.
    # Ici personne ne lit tant que ffmpeg tourne, comme à l'instant d'une annulation sous une sortie abondante.
    path = wav_with_silences(tmp_path, 1200.0, [(n * 0.4, n * 0.4 + 0.35) for n in range(3000)])
    spawn = asyncio.create_subprocess_exec  # l'espion de `spawned`, qui lance le vrai ffmpeg

    async def spawn_without_reader(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        read = process.stderr.read

        async def read_once_dead(size=-1):
            while process.returncode is None:
                await asyncio.sleep(0.01)
            return await read(size)

        process.stderr.read = read_once_dead
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_without_reader)
    async with asyncio.timeout(30):
        with pytest.raises(AudioDecodeError, match="longer than"):
            await detect_silences(path, 1200.0, timeout_s=1.0)
    assert finished(spawned) == [True]
