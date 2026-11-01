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
