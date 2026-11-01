"""File des tâches longues et boucle de travail des tâches de transcription.

Chaque tâche soumise à `TaskRunner` tourne dans son propre passage, une tâche asyncio détachée de la requête qui l'a
créée. Une tâche courte part tout de suite. Une tâche longue attend son tour dans `LongLane` : une seule tourne à la
fois, pour borner la mémoire de NeMo et le disque, et les autres attendent dans l'ordre d'arrivée.

Un passage ne fait que le travail : `store.start`, `process`, puis `complete` ou `fail`. Tout ce qui suit sa fin, quelle
qu'elle soit (effacer l'audio, écrire la ligne de la tâche, réveiller ceux qui l'attendent), se fait dans `_ended`,
rappelé par asyncio à la fin du passage. Un passage annulé avant son premier pas ne s'exécute jamais, et ce rappel lui
reste pourtant dû : c'est pourquoi la fin ne se règle pas dans le passage lui-même.

Un passage annulé l'est pour l'une de deux raisons, que le registre distingue : `cancel` écrit `cancelled` avant
d'annuler, alors que l'arrêt de dictee (`stop`) laisse la tâche en file ou en cours, audio en place, pour que
`start` la reprenne au prochain démarrage.

Rien n'est journalisé ici qui vienne du contenu : le message d'une exception de NeMo peut citer le glossaire, donc ni
le runner ni `log_task` n'en journalisent ou n'en enregistrent le texte.
"""

import asyncio
import contextlib
import functools
import logging
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from dictee import processing
from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure, unexpected_error
from dictee.lifecycle import cancel_once, spawn
from dictee.processing import RunStats, Services, task_dir
from dictee.taskstore import TERMINAL_STATUSES, TaskRecord, TaskResult, TaskStatus

logger = logging.getLogger("dictee")

# Une tâche en cours trouvée au démarrage après `MAX_TASK_STARTS` démarrages passe en échec `interrupted` : elle est déjà
# partie trois fois sans arriver au bout, et dictee ne la reprend plus.
MAX_TASK_STARTS = 3
# La boucle de ménage tourne toutes les `CLEANUP_EVERY_S` secondes. Ces deux valeurs se lisent à chaque tour : les tests
# les remplacent.
CLEANUP_EVERY_S = 600.0
# Un dossier de `settings.tasks_dir` qui n'est celui d'aucune tâche n'est effacé qu'après `ORPHAN_AGE_S` secondes : il
# peut être celui d'un audio en cours de décodage, dont la tâche n'est pas encore enregistrée.
ORPHAN_AGE_S = 3600.0

INTERRUPTED_MESSAGE = "The transcription was interrupted several times."


def long_audio_busy() -> ApiError:
    """Erreur 429 d'un audio long refusé parce que la place et la file d'attente des audios longs sont prises."""
    message = "Another long transcription is already waiting, try again later."
    return ApiError(429, message, type="rate_limit_error", code="long_audio_busy")


class LongLane:
    """La file des tâches longues : une qui tourne et `max_waiting` qui attendent, servies dans l'ordre d'arrivée.

    Elle tient sur un verrou asyncio, servi dans l'ordre d'arrivée, et un compte des places pris sans attente entre le
    test et l'incrément, donc sans course entre deux requêtes. Une place se réserve (`reserve`) avant que la tâche
    demande son tour (`LongTicket.turn`) : une tâche longue refusée l'est donc tout de suite, avant que son audio soit
    décodé en entier. Un flux long (`stream=true`) y prend sa place comme une tâche longue.
    """

    def __init__(self, max_waiting: int) -> None:
        self._capacity = 1 + max_waiting
        self._lock = asyncio.Lock()
        # Les places prises : celle qui tourne, celles qui attendent et celles réservées dont le tour n'est pas demandé.
        self._taken = 0

    @property
    def full(self) -> bool:
        """Vrai quand une tâche longue de plus serait refusée."""
        return self._taken >= self._capacity

    def reserve(self, *, force: bool = False) -> "LongTicket":
        """Réserve une place ; lève `long_audio_busy` quand la file est pleine, sauf avec `force`.

        `force` sert à la reprise d'une tâche déjà acceptée : elle ne se refuse pas, même si la file est déjà pleine.
        La place se rend avec `LongTicket.release`, ou en quittant le bloc `LongTicket.turn`.
        """
        if self.full and not force:
            raise long_audio_busy()
        self._taken += 1
        return LongTicket(self)


class LongTicket:
    """Une place de `LongLane` : à son tour, celle qui tourne, avant lui, celle qui attend."""

    def __init__(self, lane: LongLane) -> None:
        self._lane = lane
        self._held = True

    def release(self) -> None:
        """Rend la place à la file ; sans effet quand elle est déjà rendue."""
        if self._held:
            self._held = False
            self._lane._taken -= 1

    @contextlib.asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        """Attend derrière la tâche longue qui tourne, puis tourne jusqu'à la sortie du bloc.

        La place est rendue à la sortie, quelle qu'elle soit : fin normale, erreur ou annulation, y compris pendant
        l'attente. Une tâche longue annulée pendant qu'elle attend libère donc sa place.
        """
        try:
            async with self._lane._lock:
                yield
        finally:
            self.release()


def failure_of(error: BaseException) -> tuple[str, str]:
    """Le code et le message d'échec d'une tâche : fixes, sans jamais reprendre le texte de l'exception.

    NeMo saturé donne `overloaded`, NeMo indisponible ou en erreur `upstream_error`, toute autre exception
    `server_error`. Une exception imprévue passe par `unexpected_error`, qui n'en journalise que le nom de la classe.
    """
    if isinstance(error, NEMO_ERRORS):
        failure = nemo_failure(error)
        return failure.code, failure.message
    failure = unexpected_error(error)
    return failure.type, failure.message


def log_task(task: TaskRecord, stats: RunStats) -> None:
    """Écrit la ligne de la fin d'une tâche : qui, quoi, combien de temps et comment elle s'est terminée.

    Ni texte, ni audio, ni glossaire, ni consignes : le code d'échec seul dit pourquoi. Un champ resté inconnu s'écrit
    `-`, et le compte par défaut `default`.
    """
    logger.info(
        "task=%s user=%s mode=%s audio_s=%.1f chunks=%s starts=%d retries=%d nemo_ms=%d llm_ms=%s status=%s error=%s",
        task.id,
        "default" if task.owner is None else task.owner,
        task.params.mode.value,
        task.duration,
        "-" if stats.chunks is None else stats.chunks,
        task.attempts,
        stats.retries,
        stats.nemo_ms,
        "-" if stats.llm_ms is None else stats.llm_ms,
        task.status.value,
        task.error_code or "-",
    )


class TaskRunner:
    """Fait tourner les tâches : leur file, leur reprise au démarrage, leur annulation et le ménage de leurs restes.

    `process` et `clock` se remplacent dans les tests : `process` fait le travail d'une tâche (voir
    `processing.process`), `clock` rend l'heure en secondes, dont le runner ne garde que la partie entière pour le
    registre. Le runner ne garde en mémoire que les passages en cours et ceux qui attendent la fin d'une tâche : l'état
    d'une tâche se lit toujours dans le registre.
    """

    def __init__(
        self,
        services: Services,
        lane: LongLane,
        *,
        process: Callable[[Services, TaskRecord, RunStats], Awaitable[TaskResult]] = processing.process,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._services = services
        self._lane = lane
        self._process = process
        self._clock = clock
        self._passages: dict[str, asyncio.Task] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._cleaner: asyncio.Task | None = None
        self._stopping = False

    def _now(self) -> int:
        return int(self._clock())

    @property
    def _retention_s(self) -> float:
        return self._services.settings.task_retention_hours * 3600

    def is_long(self, duration: float) -> bool:
        """Vrai pour un audio de plus de `settings.long_audio_seconds` ; un audio court n'attend jamais son tour."""
        return duration > self._services.settings.long_audio_seconds

    async def start(self) -> None:
        """Reprend les tâches en file ou en cours, de la plus ancienne à la plus récente, puis lance le ménage.

        Une tâche en cours dont `attempts` atteint `MAX_TASK_STARTS` passe en échec `interrupted` et son audio est
        effacé. Les autres repartent par `submit`, sans ticket : une tâche longue prend sa place dans la file même
        pleine, car elle est déjà acceptée. La boucle de ménage commence tout de suite.
        """
        self._stopping = False
        store = self._services.store
        for task in store.unfinished():
            if task.status is TaskStatus.IN_PROGRESS and task.attempts >= MAX_TASK_STARTS:
                failed = store.fail(task.id, "interrupted", INTERRUPTED_MESSAGE, self._now(), self._retention_s)
                self._conclude(task.id, failed, RunStats())
            else:
                self.submit(task, None)
        self._cleaner = spawn(self._clean_forever())

    async def stop(self) -> None:
        """Arrête les passages et la boucle de ménage, et attend qu'ils aient fini.

        Un passage arrêté ici laisse sa tâche telle qu'elle est en base, avec son audio, pour la reprise. Une tâche
        soumise après l'arrêt reste en file de la même façon. Ceux qui attendent la fin d'une tâche ainsi laissée ne
        sont pas réveillés : elle n'est pas finie.
        """
        self._stopping = True
        jobs = list(self._passages.values())
        if self._cleaner is not None:
            jobs.append(self._cleaner)
            self._cleaner = None
        for job in jobs:
            cancel_once(job)
        if jobs:
            # `wait` plutôt que `gather` : une annulation de `stop` ne doit pas annuler une seconde fois les passages,
            # dont le nettoyage serait interrompu.
            await asyncio.wait(jobs)

    def submit(self, task: TaskRecord, ticket: LongTicket | None) -> None:
        """Lance la tâche : une tâche courte tout de suite, une tâche longue à son tour dans la file.

        `ticket` est la place que l'appelant a réservée pour une tâche longue ; sans lui (reprise), le runner réserve
        la place de force. Une tâche courte n'en a pas besoin, et le ticket qu'on lui donne est rendu. Le runner
        reprend la charge du ticket : il le rend à la fin du passage. Une tâche déjà lancée ne l'est pas deux fois, et
        après `stop` rien ne part : la tâche reste en file pour le prochain démarrage.
        """
        long = self.is_long(task.duration)
        refused = self._stopping or task.id in self._passages
        if refused or not long:
            if ticket is not None:
                ticket.release()
            ticket = None
        if refused:
            return
        if long and ticket is None:
            ticket = self._lane.reserve(force=True)
        stats = RunStats()
        passage = spawn(self._run(task, ticket, stats))
        self._passages[task.id] = passage
        passage.add_done_callback(functools.partial(self._ended, task.id, ticket, stats))

    async def wait(self, task_id: str) -> TaskRecord:
        """Attend la fin de la tâche et la rend, telle que le registre la garde.

        Une tâche déjà finie est rendue tout de suite. Lève `KeyError` pour une tâche inconnue, ou effacée entre-temps.
        """
        store = self._services.store
        task = store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status in TERMINAL_STATUSES:
            return task
        await self._waiters.setdefault(task_id, asyncio.Event()).wait()
        task = store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    async def cancel(self, task_id: str) -> TaskRecord | None:
        """Annule la tâche et rend son état, ou `None` pour une tâche inconnue.

        Une tâche finie reste telle quelle, et l'annuler de nouveau ne change rien. Son passage, s'il y en a un, est
        annulé et attendu : à la sortie, sa place dans la file est rendue, son audio effacé et ceux qui l'attendent
        réveillés. Une tâche que le runner ne fait pas tourner est terminée ici.
        """
        store = self._services.store
        before = store.get(task_id)
        if before is None:
            return None
        record = store.cancel(task_id, self._now(), self._retention_s)
        if before.status in TERMINAL_STATUSES:
            return record
        passage = self._passages.get(task_id)
        if passage is None:
            self._conclude(task_id, record, RunStats())
        else:
            cancel_once(passage)
            await asyncio.wait([passage])
        return record

    async def cleanup(self) -> None:
        """Efface les tâches expirées, puis les dossiers de `settings.tasks_dir` qui n'ont plus de tâche.

        Un dossier dont le nom n'est pas celui d'une tâche en base n'est effacé que s'il date de plus de
        `ORPHAN_AGE_S` secondes : il peut être celui d'un audio en cours de décodage.
        """
        store = self._services.store
        now = self._clock()
        store.delete_expired(int(now))
        known = store.known_ids()
        tasks_dir = self._services.settings.tasks_dir
        if not tasks_dir.is_dir():
            return
        for folder in tasks_dir.iterdir():
            if folder.name in known:
                continue
            with contextlib.suppress(FileNotFoundError):  # le dossier a pu disparaître depuis le parcours
                if folder.is_dir() and now - folder.stat().st_mtime > ORPHAN_AGE_S:
                    shutil.rmtree(folder, ignore_errors=True)

    async def _clean_forever(self) -> None:
        """Le ménage, tout de suite puis toutes les `CLEANUP_EVERY_S` secondes ; un échec ne l'arrête pas."""
        while True:
            try:
                await self.cleanup()
            except Exception as error:
                unexpected_error(error)  # seul le nom de la classe est journalisé
            await asyncio.sleep(CLEANUP_EVERY_S)

    async def _run(self, task: TaskRecord, ticket: LongTicket | None, stats: RunStats) -> None:
        """Le passage d'une tâche : son tour, `start`, `process`, puis `complete` ou `fail`.

        La tâche rendue par `start` est celle que `process` reçoit : elle porte les points de coupe d'un passage
        précédent. Une tâche déjà finie (annulée en attendant son tour) n'est pas traitée. Le statut que `complete` ou
        `fail` rendent n'est pas lu ici : si une annulation est arrivée pendant la fin du traitement, la tâche reste
        annulée, et `_ended` le voit dans le registre. Une `CancelledError` n'est jamais attrapée.
        """
        store = self._services.store
        async with contextlib.nullcontext() if ticket is None else ticket.turn():
            try:
                current = store.start(task.id, self._now())
                if current.status in TERMINAL_STATUSES:
                    return
                result = await self._process(self._services, current, stats)
                store.complete(task.id, result, self._now(), self._retention_s)
            except Exception as error:
                code, message = failure_of(error)
                store.fail(task.id, code, message, self._now(), self._retention_s)

    def _ended(self, task_id: str, ticket: LongTicket | None, stats: RunStats, passage: asyncio.Task) -> None:
        """Rappelé à la fin d'un passage, annulé ou non : règle ce que sa fin laisse à faire.

        Le ticket est rendu ici aussi, pour un passage annulé avant son premier pas, qui n'est jamais entré dans son
        tour. L'état de la tâche dans le registre dit comment le passage s'est terminé : finie (réussie, en échec ou
        annulée), il reste à effacer l'audio, écrire la ligne et réveiller `wait`. Encore en file ou en cours, c'est que
        dictee s'arrête (ou que le registre est tombé en panne) : la tâche et son audio restent pour la reprise.
        """
        if self._passages.get(task_id) is passage:
            del self._passages[task_id]
        if ticket is not None:
            ticket.release()
        try:
            if not passage.cancelled() and (error := passage.exception()) is not None:
                unexpected_error(error)  # une panne du registre, hors du traitement : seul le nom de la classe sort
            task = self._services.store.get(task_id)
            if task is None or task.status in TERMINAL_STATUSES:
                self._conclude(task_id, task, stats)
        except Exception as error:
            unexpected_error(error)

    def _conclude(self, task_id: str, task: TaskRecord | None, stats: RunStats) -> None:
        """Ce que la fin d'une tâche laisse à faire : effacer son audio, écrire sa ligne, réveiller `wait`."""
        try:
            shutil.rmtree(task_dir(self._services.settings, task_id), ignore_errors=True)
            if task is not None:
                log_task(task, stats)
        finally:
            if (waiter := self._waiters.pop(task_id, None)) is not None:
                waiter.set()
