"""Faux NeMo : ses appels par morceaux et sa disponibilité font ce que sa docstring dit."""

import asyncio
import io
import wave
from dataclasses import replace

import anyio
import pytest
from fakes import FINALS, FakeNemo

from dictee.nemo import NemoUnavailable
from dictee.types import Final

pytestmark = pytest.mark.anyio


def wav_of(seconds: float) -> bytes:
    """WAV PCM16 mono 16 kHz de silence, de `seconds` secondes."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(2 * int(16000 * seconds)))
    return out.getvalue()


async def chunk(nemo: FakeNemo, seconds: float = 1.0) -> Final:
    return await nemo.transcribe_chunk(wav_of(seconds), language="fr-FR", phrases=[], boost=1.0)


async def test_a_chunk_is_noted_with_its_settings_and_its_seconds():
    nemo = FakeNemo()
    await nemo.transcribe_chunk(wav_of(2.5), language="fr-FR", phrases=["Nemotron"], boost=2.0)
    assert nemo.chunk_calls == [
        {"chunk": True, "language": "fr-FR", "phrases": ["Nemotron"], "boost": 2.0, "seconds": 2.5}
    ]
    assert nemo.calls == nemo.chunk_calls


async def test_a_chunk_gives_the_four_words_of_the_finals_without_a_speaker_by_default():
    final = await chunk(FakeNemo())
    assert final.text == "Bonjour à tous. Salut."
    assert [word.text for word in final.words] == ["Bonjour", "à", "tous.", "Salut."]
    assert final.words == tuple(replace(word, speaker=None) for each in FINALS for word in each.words)


async def test_the_chunks_come_in_turn_and_the_last_one_repeats():
    nemo = FakeNemo(chunks=[Final("un", ()), Final("deux", ())])
    assert [(await chunk(nemo)).text for _ in range(3)] == ["un", "deux", "deux"]


async def test_a_chunk_that_is_an_exception_is_raised_in_its_turn():
    nemo = FakeNemo(chunks=[Final("un", ()), NemoUnavailable("down"), Final("trois", ())])
    assert (await chunk(nemo)).text == "un"
    with pytest.raises(NemoUnavailable, match="down"):
        await chunk(nemo)
    assert (await chunk(nemo)).text == "trois"


async def test_release_holds_the_first_chunk_only():
    release = anyio.Event()
    nemo = FakeNemo(chunks=[Final("un", ()), Final("deux", ())], release=release)
    first = asyncio.create_task(chunk(nemo))
    await asyncio.sleep(0.05)
    assert len(nemo.chunk_calls) == 1 and not first.done()
    assert (await asyncio.wait_for(chunk(nemo), 1)).text == "deux"
    assert not first.done()
    release.set()
    assert (await asyncio.wait_for(first, 1)).text == "un"


async def test_release_stays_in_place_and_a_new_event_holds_the_next_transcription():
    release = anyio.Event()
    nemo = FakeNemo(release=release)
    held = asyncio.create_task(chunk(nemo))
    await asyncio.sleep(0.05)
    # La transcription retenue a pris l'événement : le test le retrouve dans `release` pour le poser.
    assert nemo.release is release and not held.done()
    nemo.release.set()
    await asyncio.wait_for(held, 1)
    nemo.release = anyio.Event()
    again = asyncio.create_task(chunk(nemo))
    await asyncio.sleep(0.05)
    assert not again.done()
    nemo.release.set()
    await asyncio.wait_for(again, 1)


async def test_release_goes_to_the_first_transcription_of_either_kind(tmp_path):
    release = anyio.Event()
    nemo = FakeNemo(finals=FINALS, release=release)
    path = tmp_path / "audio.wav"
    path.write_bytes(wav_of(1.0))

    async def transcribe() -> list:
        return [event async for event in nemo.transcribe(path, language="fr-FR", phrases=[], boost=1.0)]

    held = asyncio.create_task(transcribe())
    await asyncio.sleep(0.05)
    assert not held.done()
    await asyncio.wait_for(chunk(nemo), 1)
    release.set()
    assert await asyncio.wait_for(held, 1)


async def test_ready_is_noted_and_true_by_default():
    nemo = FakeNemo()
    assert await nemo.ready() is True
    assert nemo.ready_calls == [{"ready": True}]
    assert nemo.calls == nemo.ready_calls


async def test_the_ready_answers_come_in_turn_and_the_last_one_repeats():
    nemo = FakeNemo(ready_answers=[False, False, True])
    assert [await nemo.ready() for _ in range(4)] == [False, False, True, True]


async def session(nemo: FakeNemo, path) -> list:
    """Les événements d'une session de `transcribe` sur le WAV `path`."""
    return [event async for event in nemo.transcribe(path, language="fr-FR", phrases=[], boost=1.0)]


@pytest.fixture
def wav_path(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(wav_of(1.0))
    return path


async def test_the_transcribe_errors_come_one_per_call_and_none_is_a_call_without_error(wav_path):
    nemo = FakeNemo(finals=FINALS, transcribe_errors=[NemoUnavailable("lost"), None])
    with pytest.raises(NemoUnavailable, match="lost"):
        await session(nemo, wav_path)
    assert len(await session(nemo, wav_path)) == len(FINALS)
    assert len(await session(nemo, wav_path)) == len(FINALS)
    assert len(nemo.transcribe_calls) == 3
    assert nemo.sessions_closed == 3


async def test_the_error_applies_again_once_the_transcribe_errors_are_used_up(wav_path):
    nemo = FakeNemo(finals=FINALS, error=NemoUnavailable("down"), transcribe_errors=[None])
    assert len(await session(nemo, wav_path)) == len(FINALS)
    with pytest.raises(NemoUnavailable, match="down"):
        await session(nemo, wav_path)


async def test_the_error_alone_still_fails_every_session(wav_path):
    nemo = FakeNemo(finals=FINALS, error=NemoUnavailable("down"))
    for _ in range(2):
        with pytest.raises(NemoUnavailable, match="down"):
            await session(nemo, wav_path)


async def test_on_chunk_is_called_first_in_every_chunk():
    seen = []
    nemo = FakeNemo()
    nemo.on_chunk = lambda: seen.append(len(nemo.chunk_calls))
    await chunk(nemo)
    await chunk(nemo)
    assert seen == [0, 1]
