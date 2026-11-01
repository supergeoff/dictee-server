"""Vie d'une requête : sa ligne de journal, les tâches qui lui survivent, le client qui raccroche.

Rien n'est journalisé ici qui vienne du contenu : la ligne d'une requête dit qui, quoi, combien de temps et comment
elle s'est terminée, jamais le texte ni l'audio.
"""

import asyncio
import logging
import re
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

logger = logging.getLogger("dictee")

# Statut que la ligne de journal donne à une requête dont le client a raccroché (celui de nginx).
CLIENT_CLOSED = 499

# Les tâches lancées par `spawn`, gardées ici : asyncio ne retient pas ses tâches.
_running: set[asyncio.Task] = set()


@dataclass
class Trace:
    """Ce que la ligne de journal d'une requête dit d'elle ; un champ resté inconnu s'écrit `-`.

    `route` nomme la route (`transcriptions`, par exemple). Le statut d'une requête qui s'arrête sur une erreur
    imprévue reste 500. `log` n'écrit la ligne qu'une fois : la première qui l'appelle la fixe.
    """

    route: str
    user: str = "-"
    model: str = "-"
    mode: str = "-"
    audio_s: float | None = None
    nemo_ms: int | None = None
    llm_ms: int | None = None
    decision: str = "-"
    status: int = 500
    logged: bool = False

    def log(self) -> None:
        if self.logged:
            return
        self.logged = True

        def show(value: float | int | None, template: str = "{}") -> str:
            return "-" if value is None else template.format(value)

        logger.info(
            "user=%s route=%s model=%s mode=%s audio_s=%s nemo_ms=%s llm_ms=%s decision=%s status=%d",
            self.user,
            self.route,
            self.model,
            self.mode,
            show(self.audio_s, "{:.1f}"),
            show(self.nemo_ms),
            show(self.llm_ms),
            self.decision,
            self.status,
        )


def loggable(value: str | None) -> str:
    """Valeur venue du client (un nom de modèle), sûre pour une ligne de journal.

    Seuls les lettres, chiffres et `_.:/-` restent, sur 64 caractères au plus : un client ne peut pas y forger une
    ligne avec un retour à la ligne.
    """
    return re.sub(r"[^\w.:/-]", "?", value or "")[:64] or "-"


def elapsed_ms(started: float) -> int:
    """Millisecondes écoulées depuis `started`, relevé avec `time.monotonic()`."""
    return round((time.monotonic() - started) * 1000)


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Lance `coro` dans sa propre tâche, hors de la portée d'annulation de la requête.

    Starlette annule une réponse en continu avec une portée d'annulation d'anyio, qui renouvelle l'annulation à
    chaque attente : le nettoyage d'un générateur annulé de cette façon est interrompu à sa première attente, et la
    session de NeMo resterait ouverte. Une tâche annulée par `Task.cancel` n'est annulée qu'une fois : son nettoyage
    va jusqu'au bout, dans sa propre tâche. Le code qui attend le résultat n'a plus qu'à appeler `cancel_once`.
    """
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_forget)
    return task


def cancel_once(task: asyncio.Task) -> None:
    """Annule `task` si elle tourne et si personne ne l'a déjà annulée.

    Un second `Task.cancel` pendant le nettoyage d'une tâche déjà annulée l'interrompt à sa prochaine attente : la
    session de NeMo resterait ouverte. Plusieurs chemins d'une réponse peuvent vouloir arrêter la même tâche (le
    générateur qui finit, le filet de la réponse) ; tous passent par ici.
    """
    if not task.done() and not task.cancelling():
        task.cancel()


def _forget(task: asyncio.Task) -> None:
    _running.discard(task)
    if not task.cancelled():
        task.exception()  # lue ici, pour qu'asyncio n'en journalise pas le message si personne ne la lit


async def until_disconnect(receive) -> None:
    """Rend la main quand le client raccroche ; `receive` est le `receive` ASGI, corps déjà lu."""
    while (await receive())["type"] != "http.disconnect":
        pass


async def run_unless_disconnected(request: Request, coro: Coroutine[Any, Any, Any]) -> Any | None:
    """Rend le résultat de `coro`, ou `None` si le client raccroche avant : `coro` est alors annulée.

    Starlette ne prévient pas une route d'un client qui raccroche : sans cette garde, une transcription irait à son
    terme et garderait sa place pour rien. Le corps de la requête doit être lu en entier avant l'appel. Une
    exception de `coro` est relevée ; `coro` ne doit donc pas rendre `None` elle-même.
    """
    job = spawn(coro)
    watcher = asyncio.create_task(until_disconnect(request.receive))
    try:
        await asyncio.wait({job, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        cancel_once(job)
        watcher.cancel()
        await asyncio.gather(job, watcher, return_exceptions=True)
    return None if job.cancelled() else job.result()
