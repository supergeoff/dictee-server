"""Fabrique de l'application Starlette de dictee."""

import contextlib
from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dictee.errors import ApiError, handle_api_error, handle_http_exception
from dictee.settings import Settings

MODEL_IDS = ("dictee", "dictee-clean", "dictee-diarize")


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def list_models(request: Request) -> JSONResponse:
    data = [{"id": model_id, "object": "model", "created": 0, "owned_by": "dictee"} for model_id in MODEL_IDS]
    return JSONResponse({"object": "list", "data": data})


def create_app(settings: Settings, *, nemo=None, http: httpx.AsyncClient | None = None) -> Starlette:
    """Construit l'application.

    `settings`, `nemo` et `http` sont rangés dans `app.state`. Sans `http`, l'app crée
    son client à son démarrage et le ferme à son arrêt ; un client fourni reste à la
    charge de l'appelant. Le client de NeMo n'existe pas encore : `nemo` reste à `None`.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if http is not None:
            yield
            return
        # Pas de délai de lecture par défaut : une transcription longue dépasse 5 s,
        # chaque appel fixe son propre délai quand il en a besoin.
        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        app.state.http = client
        try:
            yield
        finally:
            await client.aclose()

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/v1/models", list_models),
        ],
        exception_handlers={
            ApiError: handle_api_error,
            HTTPException: handle_http_exception,
        },
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.nemo = nemo
    app.state.http = http
    return app
