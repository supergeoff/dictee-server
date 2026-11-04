"""Traitement d'une tâche : par morceaux ou en réunion, avec les reprises auprès de NeMo."""

import asyncio
import logging
import time
from dataclasses import replace

import pytest
from conftest import audio_task
from fakes import FakeNemo

from dictee import processing
from dictee.cuts import Silence
from dictee.modes import Mode
from dictee.nemo import NemoError, NemoOverloaded, NemoUnavailable
from dictee.processing import RunStats, process, task_audio, wait_ready
from dictee.taskstore import ChunkRecord
from dictee.types import Final, Word

# Aucune pause entre deux essais ni entre deux relevés de `/ready` : les tests ne dorment pas.
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("no_retry_delays")]


def progress_writes(services, monkeypatch) -> list[float]:
    """Les avancements que le traitement écrit en base, dans l'ordre ; la liste se remplit pendant le test."""
    writes: list[float] = []
    write = services.store.set_progress

    def record(task_id: str, progress: float) -> None:
        writes.append(progress)
        write(task_id, progress)

    monkeypatch.setattr(services.store, "set_progress", record)
    return writes


class FramedNemo(FakeNemo):
    """Un faux NeMo qui annonce 1, 2 puis 3 s envoyées, comme le vrai client trame par trame, avant l'audio entier."""

    async def transcribe(self, wav, **kwargs):
        for sent in (1.0, 2.0, 3.0):
            kwargs["on_progress"](sent)
        async for event in super().transcribe(wav, **kwargs):
            yield event


# Les morceaux d'un fichier entier.


async def test_a_chunked_task_sends_each_chunk_once_and_joins_the_texts(services, fake_nemo):
    task = audio_task(services, 65.0)
    fake_nemo.chunks = [
        Final("Bonjour.", (Word("Bonjour.", 0.2, 0.8),)),
        Final(" Salut. ", (Word("Salut.", 1.0, 1.5),)),
    ]
    result = await process(services, task, RunStats())
    assert result.text == "Bonjour. Salut."
    assert [(word.text, word.start) for word in result.words] == [("Bonjour.", 0.2), ("Salut.", 31.0)]
    assert [call["seconds"] for call in fake_nemo.calls if "chunk" in call] == pytest.approx([30.0, 35.0])
    assert services.store.get(task.id).cuts == (30.0,)


async def test_empty_chunks_leave_no_extra_space(services, fake_nemo):
    fake_nemo.chunks = [Final("", ()), Final("Salut.", ())]
    assert (await process(services, audio_task(services, 65.0), RunStats())).text == "Salut."


async def test_a_resumed_task_sends_only_the_chunks_not_done(services, fake_nemo):
    task = audio_task(services, 65.0)
    services.store.set_cuts(task.id, [30.0])
    services.store.save_chunk(task.id, ChunkRecord(0, 0.0, 30.0, "Bonjour.", (Word("Bonjour.", 0.2, 0.8),)))
    fake_nemo.chunks = [Final("Salut.", (Word("Salut.", 1.0, 1.5),))]
    result = await process(services, services.store.get(task.id), RunStats())
    assert result.text == "Bonjour. Salut."
    assert [call["seconds"] for call in fake_nemo.calls if "chunk" in call] == pytest.approx([35.0])


async def test_progress_follows_the_chunks_done(services, fake_nemo):
    task, seen = audio_task(services, 65.0), []
    fake_nemo.on_chunk = lambda: seen.append(services.store.get(task.id).progress)
    await process(services, task, RunStats())
    assert seen == [0.0, 0.5] and services.store.get(task.id).progress == 1.0


async def test_the_chunks_carry_the_glossary_and_the_boost_of_the_task(services, fake_nemo):
    await process(services, audio_task(services, 3.0), RunStats())
    [call] = [call for call in fake_nemo.calls if "chunk" in call]
    assert (call["phrases"], call["boost"], call["language"]) == (["Nemotron"], 1.0, "fr-FR")


async def test_each_chunk_is_saved_with_its_text_and_its_words_shifted(services, fake_nemo):
    task = audio_task(services, 65.0)
    fake_nemo.chunks = [
        Final("Bonjour.", (Word("Bonjour.", 0.2, 0.8),)),
        Final(" Salut. ", (Word("Salut.", 1.0, 1.5),)),
    ]
    await process(services, task, RunStats())
    assert services.store.chunks(task.id) == [
        ChunkRecord(0, 0.0, 30.0, "Bonjour.", (Word("Bonjour.", 0.2, 0.8),)),
        ChunkRecord(1, 30.0, 65.0, "Salut.", (Word("Salut.", 31.0, 31.5),)),
    ]


async def test_a_task_whose_chunks_are_all_saved_sends_nothing(services, fake_nemo):
    task = audio_task(services, 65.0)
    services.store.set_cuts(task.id, [30.0])
    services.store.save_chunk(task.id, ChunkRecord(0, 0.0, 30.0, "Bonjour.", ()))
    services.store.save_chunk(task.id, ChunkRecord(1, 30.0, 65.0, "Salut.", ()))
    result = await process(services, services.store.get(task.id), RunStats())
    assert result.text == "Bonjour. Salut." and fake_nemo.calls == []


async def test_a_task_that_failed_midway_resumes_with_the_chunks_it_had_saved(services, fake_nemo):
    task = audio_task(services, 65.0)
    fake_nemo.chunks = [Final("Bonjour.", ()), NemoError("bad audio")]
    with pytest.raises(NemoError):
        await process(services, task, RunStats())
    assert [chunk.text for chunk in services.store.chunks(task.id)] == ["Bonjour."]
    assert services.store.get(task.id).progress == 0.5
    fake_nemo.chunks = [Final("Salut.", ())]
    result = await process(services, services.store.get(task.id), RunStats())
    assert result.text == "Bonjour. Salut."
    assert len(fake_nemo.chunk_calls) == 3


async def test_a_short_audio_is_one_chunk_without_a_scan_for_silences(services, fake_nemo, monkeypatch):
    async def scan(wav, duration):
        raise AssertionError("a short audio needs no scan")

    monkeypatch.setattr(processing, "detect_silences", scan)
    task = audio_task(services, 40.0)
    stats = RunStats()
    await process(services, task, stats)
    assert services.store.get(task.id).cuts == ()
    assert [call["seconds"] for call in fake_nemo.chunk_calls] == pytest.approx([40.0])
    assert stats.chunks == 1


async def test_a_longer_audio_is_cut_inside_the_silences_found(services, fake_nemo, monkeypatch):
    scanned = []

    async def scan(wav, duration):
        scanned.append((wav, duration))
        return [Silence(34.5, 35.5)]

    monkeypatch.setattr(processing, "detect_silences", scan)
    task = audio_task(services, 70.0)
    stats = RunStats()
    await process(services, task, stats)
    assert scanned == [(task_audio(services.settings, task.id), 70.0)]
    assert services.store.get(task.id).cuts == (35.0,)
    assert [call["seconds"] for call in fake_nemo.chunk_calls] == pytest.approx([35.0, 35.0])
    assert stats.chunks == 2


async def test_the_result_carries_the_duration_of_the_task_and_its_iso_language(services):
    result = await process(services, audio_task(services, 3.0), RunStats())
    assert (result.duration, result.language) == (3.0, "fr")
    assert (result.segments, result.cleaned, result.decision) == (None, None, None)


# La reformulation.


async def test_a_clean_task_cleans_the_assembled_text(services, fake_llm):
    result = await process(services, audio_task(services, 3.0, mode=Mode.CLEAN), RunStats())
    assert (result.text, result.cleaned) == ("Bonjour à tous. Salut.", fake_llm.reply)
    assert result.transcript().text == fake_llm.reply


async def test_the_cleaning_uses_the_instructions_and_the_glossary_of_the_task(services, fake_llm):
    stats = RunStats()
    result = await process(services, audio_task(services, 3.0, mode=Mode.CLEAN), stats)
    [call] = fake_llm.calls
    system = call["messages"][0]["content"]
    assert system.startswith("Corrige.") and "- Nemotron" in system
    assert result.decision == "cleaned" and stats.llm_ms is not None


async def test_a_task_that_is_not_clean_leaves_the_llm_alone(services, fake_llm):
    stats = RunStats()
    await process(services, audio_task(services, 3.0), stats)
    assert fake_llm.calls == [] and stats.llm_ms is None


# La réunion.


async def test_a_meeting_labels_its_speakers_and_builds_its_segments(services, fake_nemo):
    result = await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), RunStats())
    assert [segment.speaker for segment in result.segments] == ["A", "B"]
    assert fake_nemo.calls[0]["speakers"] is True and fake_nemo.calls[0]["seconds"] == pytest.approx(3.0)


async def test_a_meeting_joins_its_non_empty_sentences_and_keeps_every_word(services, fake_nemo):
    fake_nemo.finals = (Final("", ()), Final(" Bonjour. ", (Word("Bonjour.", 0.0, 0.5, "0"),)), Final("Salut.", ()))
    task = audio_task(services, 3.0, mode=Mode.DIARIZE)
    stats = RunStats()
    result = await process(services, task, stats)
    assert result.text == "Bonjour. Salut."
    assert [(word.text, word.speaker) for word in result.words] == [("Bonjour.", "A")]
    assert (result.duration, result.language, stats.chunks) == (3.0, "fr", None)
    [call] = fake_nemo.calls
    assert (call["phrases"], call["boost"], call["language"]) == (["Nemotron"], 1.0, "fr-FR")


async def test_a_meeting_without_any_word_has_no_segment(services, fake_nemo):
    fake_nemo.finals = ()
    result = await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), RunStats())
    assert (result.text, result.words, result.segments) == ("", (), ())


async def test_a_meeting_starts_its_whole_session_again_after_a_lost_connection(services, fake_nemo):
    fake_nemo.transcribe_errors = [NemoUnavailable("lost"), None]
    stats = RunStats()
    await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), stats)
    assert len([call for call in fake_nemo.calls if "speakers" in call]) == 2 and stats.retries == 1


async def test_a_meeting_keeps_only_the_words_of_its_last_session(services, fake_nemo):
    # La première session rend sa première phrase avant d'être perdue : ses mots ne doivent pas rester dans le résultat.
    fake_nemo.after = 1
    fake_nemo.transcribe_errors = [NemoUnavailable("lost"), None]
    result = await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), RunStats())
    assert result.text == "Bonjour à tous. Salut."
    assert len(result.words) == 4


async def test_a_meeting_gives_up_after_three_retries(services, fake_nemo):
    fake_nemo.transcribe_errors = [NemoOverloaded("full")] * 4
    stats = RunStats()
    with pytest.raises(NemoOverloaded):
        await process(services, audio_task(services, 3.0, mode=Mode.DIARIZE), stats)
    assert len(fake_nemo.transcribe_calls) == 4 and stats.retries == 3


async def test_the_progress_of_a_meeting_is_written_at_most_every_few_seconds_of_real_time(services, monkeypatch):
    # Quatre relevés (1 à 4 s envoyées) en quelques millisecondes : un seul passe, quelle que soit la durée d'audio.
    monkeypatch.setattr(processing, "PROGRESS_EVERY_S", 2.5)
    writes = progress_writes(services, monkeypatch)
    services = replace(services, nemo=FramedNemo(finals=services.nemo.finals))
    await process(services, audio_task(services, 4.0, mode=Mode.DIARIZE), RunStats())
    assert writes == [0.25]


async def test_the_progress_of_a_meeting_is_the_seconds_sent_over_its_duration(services, monkeypatch):
    monkeypatch.setattr(processing, "PROGRESS_EVERY_S", 0.0)
    writes = progress_writes(services, monkeypatch)
    services = replace(services, nemo=FramedNemo(finals=services.nemo.finals))
    task = audio_task(services, 4.0, mode=Mode.DIARIZE)
    await process(services, task, RunStats())
    assert writes == [0.25, 0.5, 0.75, 1.0]
    assert services.store.get(task.id).progress == 1.0


async def test_the_progress_of_a_meeting_never_goes_past_one(services, monkeypatch):
    monkeypatch.setattr(processing, "PROGRESS_EVERY_S", 0.0)
    writes = progress_writes(services, monkeypatch)
    services = replace(services, nemo=FramedNemo(finals=services.nemo.finals))
    task = audio_task(services, 4.0, mode=Mode.DIARIZE)
    await process(services, replace(task, duration=2.0), RunStats())
    assert writes == [0.5, 1.0, 1.0, 1.0]


async def test_the_progress_of_a_meeting_never_goes_back_after_a_retry(services, monkeypatch):
    # Le premier essai annonce 1 à 4 s envoyées puis perd sa session ; le second repart de 1 s.
    monkeypatch.setattr(processing, "PROGRESS_EVERY_S", 0.0)
    writes = progress_writes(services, monkeypatch)
    nemo = FramedNemo(finals=services.nemo.finals, transcribe_errors=[NemoUnavailable("lost"), None])
    services = replace(services, nemo=nemo)
    await process(services, audio_task(services, 4.0, mode=Mode.DIARIZE), RunStats())
    assert writes == [0.25, 0.5, 0.75, 1.0, 1.0, 1.0, 1.0, 1.0]


async def test_a_resumed_meeting_keeps_the_progress_it_had_reached(services, monkeypatch):
    # Après un redémarrage, la tâche avait atteint 0,6 : la nouvelle session ne fait pas reculer la barre.
    monkeypatch.setattr(processing, "PROGRESS_EVERY_S", 0.0)
    services = replace(services, nemo=FramedNemo(finals=services.nemo.finals))
    task = audio_task(services, 4.0, mode=Mode.DIARIZE)
    services.store.set_progress(task.id, 0.6)
    writes = progress_writes(services, monkeypatch)
    await process(services, services.store.get(task.id), RunStats())
    assert writes == [0.6, 0.6, 0.75, 1.0]


# Les reprises auprès de NeMo.


async def test_an_unavailable_nemo_is_tried_again_once_ready(services, fake_nemo):
    fake_nemo.chunks = [NemoUnavailable("down"), Final("Bonjour.", ())]
    fake_nemo.ready_answers = [False, True]
    stats = RunStats()
    assert (await process(services, audio_task(services, 3.0), stats)).text == "Bonjour."
    assert stats.retries == 1 and len([call for call in fake_nemo.calls if "ready" in call]) == 2


async def test_after_three_retries_the_failure_goes_up(services, fake_nemo):
    fake_nemo.chunks = [NemoOverloaded("full")]
    with pytest.raises(NemoOverloaded):
        await process(services, audio_task(services, 3.0), RunStats())
    assert len([call for call in fake_nemo.calls if "chunk" in call]) == 4


async def test_an_error_on_the_request_itself_is_not_tried_again(services, fake_nemo):
    fake_nemo.chunks = [NemoError("bad audio")]
    with pytest.raises(NemoError):
        await process(services, audio_task(services, 3.0), RunStats())
    assert len([call for call in fake_nemo.calls if "chunk" in call]) == 1


async def test_waiting_for_nemo_gives_up_after_its_delay(services, fake_nemo, monkeypatch):
    monkeypatch.setattr(processing, "READY_WAIT_S", 0.05)
    fake_nemo.chunks, fake_nemo.ready_answers = [NemoUnavailable("down")], [False]
    with pytest.raises(NemoUnavailable):
        await process(services, audio_task(services, 3.0), RunStats())


async def test_giving_up_on_a_nemo_that_stays_down_is_not_one_more_retry(services, fake_nemo, monkeypatch):
    monkeypatch.setattr(processing, "READY_WAIT_S", 0.05)
    fake_nemo.chunks, fake_nemo.ready_answers = [NemoUnavailable("down")], [False]
    stats = RunStats()
    with pytest.raises(NemoUnavailable, match="did not become ready"):
        await process(services, audio_task(services, 3.0), stats)
    assert stats.retries == 0 and len(fake_nemo.chunk_calls) == 1


async def test_each_retry_waits_its_own_pause(services, fake_nemo, monkeypatch):
    pauses = []
    sleep = asyncio.sleep

    async def record(delay):
        pauses.append(delay)
        await sleep(0)

    monkeypatch.setattr(processing, "RETRY_DELAYS_S", (0.5, 0.6, 0.7))
    monkeypatch.setattr(processing.asyncio, "sleep", record)
    fake_nemo.chunks = [NemoUnavailable("down")] * 3 + [Final("Bonjour.", ())]
    stats = RunStats()
    assert (await process(services, audio_task(services, 3.0), stats)).text == "Bonjour."
    assert pauses == [0.5, 0.6, 0.7] and stats.retries == 3


async def test_the_retries_of_each_chunk_are_counted_apart(services, fake_nemo):
    # Chaque morceau a droit à ses trois reprises : deux pannes pour le premier, trois pour le second.
    fake_nemo.chunks = [
        NemoUnavailable("a"),
        NemoUnavailable("b"),
        Final("Bonjour.", ()),
        NemoOverloaded("c"),
        NemoOverloaded("d"),
        NemoOverloaded("e"),
        Final("Salut.", ()),
    ]
    stats = RunStats()
    result = await process(services, audio_task(services, 65.0), stats)
    assert result.text == "Bonjour. Salut." and stats.retries == 5


async def test_wait_ready_returns_as_soon_as_nemo_is_ready(fake_nemo):
    fake_nemo.ready_answers = [False, False, True]
    await wait_ready(fake_nemo)
    assert len(fake_nemo.ready_calls) == 3


async def test_wait_ready_lets_the_loop_run_between_two_readings(fake_nemo, monkeypatch):
    monkeypatch.setattr(processing, "READY_WAIT_S", 0.05)
    fake_nemo.ready_answers = [False]
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    with pytest.raises(NemoUnavailable):
        await wait_ready(fake_nemo)
    ticker.cancel()
    assert ticks > 1


# Les mesures et les journaux.


async def test_the_stats_count_the_chunks_and_the_time_spent_in_nemo(services, fake_nemo):
    fake_nemo.on_chunk = lambda: time.sleep(0.02)
    stats = RunStats()
    await process(services, audio_task(services, 65.0), stats)
    assert stats.chunks == 2 and stats.retries == 0 and stats.llm_ms is None
    assert stats.nemo_ms >= 40


async def test_the_time_of_a_failed_attempt_counts_as_time_spent_in_nemo(services, fake_nemo):
    fake_nemo.on_chunk = lambda: time.sleep(0.02)
    fake_nemo.chunks = [NemoUnavailable("down"), Final("Bonjour.", ())]
    stats = RunStats()
    await process(services, audio_task(services, 3.0), stats)
    assert stats.retries == 1 and stats.nemo_ms >= 40


async def test_a_failure_of_nemo_leaves_no_trace_in_the_logs(services, fake_nemo, caplog):
    # NemoError recopie le message de NeMo, qui peut citer la requête et donc le glossaire.
    fake_nemo.chunks = [NemoError("rejected the glossary term Nemotron")]
    with caplog.at_level(logging.DEBUG), pytest.raises(NemoError):
        await process(services, audio_task(services, 3.0), RunStats())
    assert [record for record in caplog.records if record.name.startswith("dictee")] == []
    assert "Nemotron" not in caplog.text
