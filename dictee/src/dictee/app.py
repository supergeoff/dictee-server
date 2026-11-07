"""Fabrique de l'application Starlette de dictee."""

import contextlib
from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from dictee.auth import require_caller
from dictee.db import Database
from dictee.errors import ApiError, handle_api_error, handle_http_exception, handle_unexpected_error
from dictee.live import realtime
from dictee.modes import MODELS
from dictee.nemo import NemoClient
from dictee.processing import Services
from dictee.runner import LongLane, TaskRunner
from dictee.settings import Settings
from dictee.speech import speech
from dictee.tasks_api import cancel_task, get_task
from dictee.taskstore import TaskStore
from dictee.transcribe import transcriptions
from dictee.web import (
    PreviewCache,
    WebError,
    build_oauth,
    callback,
    create_key,
    handle_web_error,
    icon,
    index,
    login,
    logout,
    reset_instructions,
    revoke_key,
    touch_icon,
    update_settings,
    voice_preview,
)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def list_models(request: Request) -> JSONResponse:
    require_caller(request)
    data = [{"id": model_id, "object": "model", "created": 0, "owned_by": "dictee"} for model_id in MODELS]
    return JSONResponse({"object": "list", "data": data})


def create_app(settings: Settings, *, nemo=None, http: httpx.AsyncClient | None = None) -> Starlette:
    """Construit l'application.

    `settings`, `nemo`, `http`, la base de données, le registre des tâches (`store`), la file des tâches longues
    (`lane`, que les flux longs partagent avec elles) et les voix de la synthèse (`voices`, `None` jusqu'à la première
    lecture) sont rangés dans `app.state`. La base s'ouvre à `settings.db_path`, dossier parent créé au besoin. Sans
    `http`, l'app crée son client à son démarrage et le ferme à son arrêt ; un client fourni reste à la charge de
    l'appelant. Sans `nemo`, elle crée aussi à son démarrage le client de NeMo, sur ce client HTTP ; `app.state.nemo`
    reste `None` jusque-là.

    Le démarrage crée `settings.tasks_dir`, range dans `app.state.runner` le `TaskRunner` qui travaille sur ces deux
    clients, et le démarre : il reprend les tâches restées en file ou en cours. L'arrêt de l'app arrête le runner, avant
    que le client HTTP se ferme.

    Chaque route `/v1` appelle `require_caller` en premier ; `/health` reste ouverte. La dictée en direct
    (`/v1/realtime`, une route WebSocket, voir `dictee.live`) identifie l'appelant par les mêmes en-têtes. La page de
    configuration (`dictee.web`) ouvre ses sessions par PocketID : `app.state.oauth` porte le client `pocketid`, et le
    cookie de session, signé par `settings.session_secret`, n'est envoyé que sur une connexion sécurisée.
    `app.state.previews` garde les aperçus de voix déjà synthétisés.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            if http is None:
                # Pas de délai de lecture par défaut : une transcription longue dépasse 5 s,
                # chaque appel fixe son propre délai quand il en a besoin.
                client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
                stack.push_async_callback(client.aclose)
                app.state.http = client
            if nemo is None:
                app.state.nemo = NemoClient(settings.nemo_url, settings.nemo_api_key, app.state.http)
            settings.tasks_dir.mkdir(parents=True, exist_ok=True)
            services = Services(app.state.store, app.state.nemo, settings, app.state.http)
            app.state.runner = TaskRunner(services, app.state.lane)
            await app.state.runner.start()
            stack.push_async_callback(app.state.runner.stop)
            yield

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/login", login),
            Route("/auth/callback", callback),
            Route("/logout", logout, methods=["POST"]),
            Route("/keys", create_key, methods=["POST"]),
            Route("/keys/{key_id:int}/revoke", revoke_key, methods=["POST"]),
            Route("/settings", update_settings, methods=["POST"]),
            Route("/settings/reset-instructions", reset_instructions, methods=["POST"]),
            Route("/voice/preview", voice_preview),
            Route("/icon.svg", icon),
            Route("/apple-touch-icon.png", touch_icon),
            Route("/health", health),
            Route("/v1/models", list_models),
            Route("/v1/audio/transcriptions", transcriptions, methods=["POST"]),
            Route("/v1/audio/transcriptions/{task_id}", get_task),
            Route("/v1/audio/transcriptions/{task_id}/cancel", cancel_task, methods=["POST"]),
            Route("/v1/audio/speech", speech, methods=["POST"]),
            WebSocketRoute("/v1/realtime", realtime),
        ],
        middleware=[
            Middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=True, same_site="lax")
        ],
        exception_handlers={
            ApiError: handle_api_error,
            WebError: handle_web_error,
            HTTPException: handle_http_exception,
            Exception: handle_unexpected_error,
        },
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.oauth = build_oauth(settings)
    app.state.db = Database(settings.db_path)
    app.state.nemo = nemo
    app.state.http = http
    app.state.store = TaskStore(app.state.db)
    app.state.lane = LongLane(settings.max_queued_long)
    app.state.voices = None
    app.state.previews = PreviewCache()
    return app
