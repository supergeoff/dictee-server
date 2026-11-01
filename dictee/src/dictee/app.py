"""Fabrique de l'application Starlette de dictee."""

import contextlib
from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dictee.auth import require_caller
from dictee.db import Database
from dictee.errors import ApiError, handle_api_error, handle_http_exception, handle_unexpected_error
from dictee.modes import MODELS
from dictee.nemo import NemoClient
from dictee.settings import Settings
from dictee.speech import speech
from dictee.transcribe import LongAudioGate, transcriptions


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def list_models(request: Request) -> JSONResponse:
    require_caller(request)
    data = [{"id": model_id, "object": "model", "created": 0, "owned_by": "dictee"} for model_id in MODELS]
    return JSONResponse({"object": "list", "data": data})


def create_app(settings: Settings, *, nemo=None, http: httpx.AsyncClient | None = None) -> Starlette:
    """Construit l'application.

    `settings`, `nemo`, `http`, la base de données, la place des transcriptions longues (`gate`) et les voix de la
    synthèse (`voices`, `None` jusqu'à la première lecture) sont rangés dans `app.state`. La base s'ouvre à
    `settings.db_path`, dossier parent créé au besoin. Sans `http`, l'app crée son client à son démarrage et le ferme
    à son arrêt ; un client fourni reste à la charge de l'appelant. Sans `nemo`, elle crée aussi à son démarrage le
    client de NeMo, sur ce client HTTP ; `app.state.nemo` reste `None` jusque-là.

    Chaque route `/v1` appelle `require_caller` en premier ; `/health` reste ouverte.
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
            yield

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/v1/models", list_models),
            Route("/v1/audio/transcriptions", transcriptions, methods=["POST"]),
            Route("/v1/audio/speech", speech, methods=["POST"]),
        ],
        exception_handlers={
            ApiError: handle_api_error,
            HTTPException: handle_http_exception,
            Exception: handle_unexpected_error,
        },
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = Database(settings.db_path)
    app.state.nemo = nemo
    app.state.http = http
    app.state.gate = LongAudioGate(settings.long_audio_seconds)
    app.state.voices = None
    return app
