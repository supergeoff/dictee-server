"""Traitement d'une tâche de transcription : par morceaux de 30 s, ou en une session avec les personnes (réunion).

Un fichier entier passe par la route REST de NeMo, un morceau à la fois, et chaque morceau fini est gardé en base :
une reprise ne renvoie que ceux qui manquent. Une réunion passe par la session temps réel avec les personnes, qui ne
se reprend pas à mi-chemin : une session perdue recommence au début du fichier.

Un morceau ou une session que NeMo ne traite pas (indisponible ou saturé) est renvoyé, après le retour de NeMo, trois
fois au plus. Rien n'est journalisé ici : le message d'une `NemoError` recopie celui de NeMo, qui peut citer la requête,
donc le glossaire : le lanceur de tâches qui reçoit l'exception n'en journalise que le nom de la classe.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from dictee.audio import read_wav_slice
from dictee.cleanup import clean
from dictee.cuts import MAX_CHUNK_S, chunk_bounds, detect_silences, plan_cuts
from dictee.lifecycle import elapsed_ms
from dictee.modes import Mode, to_iso_language
from dictee.nemo import NemoClient, NemoOverloaded, NemoUnavailable
from dictee.profile import Profile, glossary_terms
from dictee.settings import Settings
from dictee.speakers import build_segments, label_speakers
from dictee.taskstore import ChunkRecord, TaskRecord, TaskResult, TaskStore
from dictee.types import Final, Segment, Word

# Reprises auprès de NeMo : un essai, puis `MAX_RETRIES` reprises au plus par morceau ou par session. Chaque reprise
# attend une réponse positive de `/ready`, puis la pause de son rang. Ces valeurs se lisent à chaque appel : les tests
# les remplacent.
MAX_RETRIES = 3
RETRY_DELAYS_S = (2.0, 5.0, 10.0)
# `wait_ready` interroge `/ready` toutes les `READY_POLL_S` secondes et abandonne après `READY_WAIT_S`.
READY_POLL_S = 2.0
READY_WAIT_S = 300.0
# L'avancement d'une réunion s'écrit en base au plus toutes les `PROGRESS_EVERY_S` secondes de temps réel : une réunion
# de 2 heures envoie une trame par seconde d'audio, et chaque écriture est une transaction.
PROGRESS_EVERY_S = 5.0


@dataclass(frozen=True)
class Services:
    """Ce que le traitement d'une tâche utilise : le registre, NeMo, les réglages et le client HTTP de `clean`."""

    store: TaskStore
    nemo: NemoClient
    settings: Settings
    http: httpx.AsyncClient


@dataclass
class RunStats:
    """Ce que le traitement d'une tâche mesure, pour la ligne de journal de son lanceur.

    `chunks` est le nombre de morceaux (`None` pour une réunion), `retries` le nombre de reprises auprès de NeMo,
    `nemo_ms` le temps passé dans les appels de transcription à NeMo, essais manqués compris, pauses et attentes de
    `/ready` exclues, et `llm_ms` la durée de la reformulation (`None` sans elle).
    """

    chunks: int | None = None
    retries: int = 0
    nemo_ms: int = 0
    llm_ms: int | None = None


def task_dir(settings: Settings, task_id: str) -> Path:
    """Dossier d'une tâche, où son audio attend le traitement."""
    return settings.tasks_dir / task_id


def task_audio(settings: Settings, task_id: str) -> Path:
    """Le WAV 16 kHz mono d'une tâche."""
    return task_dir(settings, task_id) / "audio.wav"


async def wait_ready(nemo: NemoClient) -> None:
    """Attend que NeMo réponde prêt, en l'interrogeant toutes les `READY_POLL_S` secondes.

    Chaque réponse négative est suivie d'une pause de `READY_POLL_S` secondes, qui rend la main à la boucle même
    quand elle est nulle. Lève `NemoUnavailable` quand NeMo n'est toujours pas prêt `READY_WAIT_S` secondes après le
    premier appel.
    """
    deadline = time.monotonic() + READY_WAIT_S
    while not await nemo.ready():
        if time.monotonic() >= deadline:
            raise NemoUnavailable(f"NeMo did not become ready within {READY_WAIT_S:g} seconds")
        await asyncio.sleep(READY_POLL_S)


async def _with_retries[T](nemo: NemoClient, stats: RunStats, attempt: Callable[[], Awaitable[T]]) -> T:
    """Rend le résultat de `attempt`, que NeMo indisponible ou saturé fait relancer après son retour.

    Chaque reprise attend `wait_ready`, puis la pause `RETRY_DELAYS_S` de son rang, et s'ajoute à `stats.retries`.
    Une `NemoError` remonte tout de suite : renvoyer la même requête n'y changerait rien. Après `MAX_RETRIES`
    reprises, la dernière panne remonte. Une `NemoUnavailable` de `wait_ready` remonte aussi tout de suite : NeMo
    est resté absent, et ce n'est pas une reprise de plus.
    """
    retry = 0
    while True:
        try:
            return await attempt()
        except (NemoUnavailable, NemoOverloaded):
            if retry == MAX_RETRIES:
                raise
        # Hors du `except` : la panne de `wait_ready` ne repart pas dans une reprise.
        await wait_ready(nemo)
        await asyncio.sleep(RETRY_DELAYS_S[retry])
        retry += 1
        stats.retries += 1


async def _plan_cuts(wav: Path, duration: float) -> tuple[float, ...]:
    """Les coupes d'un audio : aucune pour un audio d'un seul morceau, sinon celles que ses silences donnent."""
    if duration <= MAX_CHUNK_S:
        return ()
    return tuple(plan_cuts(duration, await detect_silences(wav, duration)))


async def _transcribe_chunk(
    services: Services, task: TaskRecord, stats: RunStats, index: int, start: float, end: float
) -> ChunkRecord:
    """Le morceau `index` de l'audio, de `start` à `end` secondes, passé par NeMo sous les reprises.

    Les mots du morceau sont décalés de `start` : ils portent leurs instants dans le fichier entier.
    """
    wav = read_wav_slice(task_audio(services.settings, task.id), start, end)
    params = task.params

    async def attempt() -> Final:
        started = time.monotonic()
        try:
            return await services.nemo.transcribe_chunk(
                wav, language=params.language, phrases=glossary_terms(params.glossary), boost=params.boost
            )
        finally:
            stats.nemo_ms += elapsed_ms(started)

    final = await _with_retries(services.nemo, stats, attempt)
    words = tuple(replace(word, start=word.start + start, end=word.end + start) for word in final.words)
    return ChunkRecord(index, start, end, final.text.strip(), words)


async def _transcribe_chunks(services: Services, task: TaskRecord, stats: RunStats) -> tuple[str, tuple[Word, ...]]:
    """Le texte et les mots du fichier entier, morceau par morceau ; ceux déjà en base ne repartent pas.

    Les coupes sont celles de la tâche, ou celles qu'elle n'avait pas encore et que `set_cuts` garde pour une reprise.
    Chaque morceau fini est gardé, puis l'avancement vaut la part des morceaux faits.
    """
    store = services.store
    cuts = task.cuts
    if cuts is None:
        cuts = await _plan_cuts(task_audio(services.settings, task.id), task.duration)
        store.set_cuts(task.id, cuts)
    bounds = chunk_bounds(task.duration, cuts)
    stats.chunks = len(bounds)
    chunks = {chunk.idx: chunk for chunk in store.chunks(task.id)}
    for index, (start, end) in enumerate(bounds):
        if index in chunks:
            continue
        chunks[index] = await _transcribe_chunk(services, task, stats, index, start, end)
        store.save_chunk(task.id, chunks[index])
        store.set_progress(task.id, len(chunks) / len(bounds))
    ordered = [chunks[index] for index in range(len(bounds))]
    text = " ".join(text for chunk in ordered if (text := chunk.text.strip()))
    return text, tuple(word for chunk in ordered for word in chunk.words)


async def _transcribe_meeting(
    services: Services, task: TaskRecord, stats: RunStats
) -> tuple[str, tuple[Word, ...], tuple[Segment, ...]]:
    """Le texte, les mots avec leur personne et les segments d'une réunion, par une session de NeMo avec les personnes.

    Une session perdue recommence au début du fichier, avec des mots et des phrases neufs. L'avancement vaut les
    secondes envoyées sur la durée de la tâche ; il s'écrit au plus toutes les `PROGRESS_EVERY_S` secondes de temps
    réel, tous essais confondus. Il ne redescend jamais : son plancher part de l'avancement de la tâche au départ,
    monte avec chaque écriture et vaut pour tous les essais, si bien qu'une reprise ou un redémarrage ne fait pas
    reculer la barre qu'un client lit.
    """
    wav = task_audio(services.settings, task.id)
    params = task.params
    last_write: float | None = None
    floor = task.progress

    def on_progress(sent: float) -> None:
        nonlocal last_write, floor
        now = time.monotonic()
        if last_write is None or now - last_write >= PROGRESS_EVERY_S:
            last_write = now
            floor = max(floor, min(1.0, sent / task.duration))
            services.store.set_progress(task.id, floor)

    async def attempt() -> tuple[list[str], list[Word]]:
        texts: list[str] = []
        words: list[Word] = []
        started = time.monotonic()
        try:
            async with contextlib.aclosing(
                services.nemo.transcribe(
                    wav,
                    language=params.language,
                    phrases=glossary_terms(params.glossary),
                    boost=params.boost,
                    speakers=True,
                    on_progress=on_progress,
                )
            ) as events:
                async for event in events:
                    if isinstance(event, Final):
                        words.extend(event.words)
                        if text := event.text.strip():
                            texts.append(text)
        finally:
            stats.nemo_ms += elapsed_ms(started)
        return texts, words

    texts, words = await _with_retries(services.nemo, stats, attempt)
    labelled = tuple(label_speakers(words))
    return " ".join(texts), labelled, tuple(build_segments(labelled))


async def process(services: Services, task: TaskRecord, stats: RunStats) -> TaskResult:
    """Traite la tâche et rend son résultat, sans l'enregistrer : c'est à l'appelant de le faire.

    Une réunion (`Mode.DIARIZE`) passe par une session avec les personnes. Tout autre mode passe par les morceaux du
    fichier, et `Mode.CLEAN` fait ensuite reformuler le texte entier avec le glossaire et les consignes de la tâche :
    `cleaned` et `decision` viennent de `clean`, qui ne lève jamais d'exception à cause du LLM. Une exception de NeMo
    qui remonte ici (`NemoUnavailable` ou `NemoOverloaded` après les reprises, `NemoError`) laisse en base les
    morceaux déjà faits.
    """
    params = task.params
    segments: tuple[Segment, ...] | None = None
    if params.mode is Mode.DIARIZE:
        text, words, segments = await _transcribe_meeting(services, task, stats)
    else:
        text, words = await _transcribe_chunks(services, task, stats)

    cleaned = decision = None
    if params.mode is Mode.CLEAN:
        started = time.monotonic()
        result = await clean(
            text,
            profile=Profile(params.glossary, params.instructions or "", ""),
            settings=services.settings,
            http=services.http,
        )
        stats.llm_ms = elapsed_ms(started)
        cleaned, decision = result.text, result.decision
    return TaskResult(text, words, segments, task.duration, to_iso_language(params.language), cleaned, decision)
