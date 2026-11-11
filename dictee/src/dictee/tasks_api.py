"""Objet tâche, lecture et annulation d'une tâche de transcription.

Une tâche n'est visible que de son propriétaire : pour tout autre appelant, comme pour une tâche inconnue ou effacée,
l'identifiant répond la même 404.
"""

import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from dictee.auth import require_caller
from dictee.errors import ApiError
from dictee.formats import render_body
from dictee.modes import RESPONSE_FORMATS, Mode, unsupported_format
from dictee.taskstore import TaskRecord, TaskStatus

# Statut HTTP et type d'erreur de l'API selon le code d'échec d'une tâche. Tout autre code (`server_error`,
# `interrupted`) donne une erreur 500 du serveur.
_FAILURES = {
    "overloaded": (429, "rate_limit_error"),
    "upstream_error": (502, "server_error"),
}


def task_not_found(task_id: str) -> ApiError:
    """L'erreur 404 d'un identifiant de tâche inconnu, effacé ou d'un autre compte."""
    return ApiError(404, f"No transcription task found with id '{task_id}'.", code="not_found")


def task_error(task: TaskRecord) -> ApiError:
    """L'erreur de l'API que rend une tâche en échec ou annulée, pour qui l'attend en direct.

    Un échec rend son code et son message enregistrés, avec le statut qu'avait la requête synchrone : `overloaded`
    donne 429, `upstream_error` 502, `server_error` et `interrupted` 500. Une tâche annulée donne 409. Une tâche qui
    n'est ni en échec ni annulée n'a pas d'erreur : l'appeler pour elle lève `ValueError`.
    """
    if task.status is TaskStatus.CANCELLED:
        return ApiError(409, "The transcription task was cancelled.", code="task_cancelled")
    if task.status is not TaskStatus.FAILED:
        raise ValueError(f"task {task.id} is {task.status.value}, it has no error")
    status, error_type = _FAILURES.get(task.error_code, (500, "server_error"))
    return ApiError(status, task.error_message, type=error_type, code=task.error_code)


def task_object(
    task: TaskRecord, *, response_format: str | None = None, granularities: frozenset[str] | None = None
) -> dict:
    """L'objet tâche, rendu selon la requête ou, par défaut, les réglages enregistrés à sa création."""
    output = None
    if task.status is TaskStatus.COMPLETED and task.result is not None:
        output = render_body(
            task.result.transcript(),
            task.response_format if response_format is None else response_format,
            granularities=task.granularities if granularities is None else granularities,
        )
    error = None
    if task.status is TaskStatus.FAILED:
        error = {"code": task.error_code, "message": task.error_message}
    return {
        "id": task.id,
        "object": "audio.transcription.task",
        "status": task.status.value,
        "model": task.model,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
        "expires_at": task.expires_at,
        "duration": round(task.duration, 3),
        "progress": round(task.progress, 3),
        "error": error,
        "output": output,
    }


def _visible_task(request: Request) -> TaskRecord:
    """La tâche de l'URL si l'appelant la voit, sinon la 404 de `task_not_found` ; l'appelant est identifié d'abord."""
    caller = require_caller(request)
    task_id = request.path_params["task_id"]
    task = request.app.state.store.get_visible(task_id, caller.user_id, int(time.time()))
    if task is None:
        raise task_not_found(task_id)
    return task


async def get_task(request: Request) -> JSONResponse:
    """`GET /v1/audio/transcriptions/{task_id}` : l'objet de la tâche, au format demandé ou à son format initial."""
    task = _visible_task(request)
    query = request.query_params
    response_format = query.get("response_format")
    if response_format is not None:
        if response_format not in RESPONSE_FORMATS:
            raise unsupported_format(response_format)
        if (task.params.mode is Mode.RAW and response_format == "diarized_json") or (
            task.params.mode is Mode.CLEAN and response_format not in ("json", "text")
        ):
            raise ApiError(
                400,
                "The requested format is incompatible with the task's processing mode.",
                param="response_format",
                code="incompatible_response_format",
            )
    values = [*query.getlist("timestamp_granularities[]"), *query.getlist("timestamp_granularities")]
    effective_format = task.response_format if response_format is None else response_format
    if values and (effective_format != "verbose_json" or set(values) - {"word", "segment"}):
        raise ApiError(400, "Unsupported timestamp granularities.", param="timestamp_granularities")
    granularities = frozenset(values) if values else (frozenset({"segment"}) if response_format is not None else None)
    return JSONResponse(task_object(task, response_format=response_format, granularities=granularities))


async def cancel_task(request: Request) -> JSONResponse:
    """`POST /v1/audio/transcriptions/{task_id}/cancel` : arrête la tâche et rend son objet.

    L'appel est idempotent et sans effet sur une tâche déjà finie, que le runner rend telle qu'elle est.
    """
    task = _visible_task(request)
    cancelled = await request.app.state.runner.cancel(task.id)
    if cancelled is None:  # le ménage l'a effacée entre la lecture et l'annulation
        raise task_not_found(task.id)
    return JSONResponse(task_object(cancelled))
