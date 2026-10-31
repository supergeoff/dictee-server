"""Erreurs de l'API, rendues dans l'enveloppe d'erreur d'OpenAI."""

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse


class ApiError(Exception):
    """Erreur à renvoyer au client avec le statut HTTP donné."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type
        self.code = code
        self.param = param


def envelope(
    message: str, *, type: str, param: str | None = None, code: str | None = None
) -> dict[str, dict[str, str | None]]:
    return {"error": {"message": message, "type": type, "param": param, "code": code}}


async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    body = envelope(exc.message, type=exc.type, param=exc.param, code=exc.code)
    return JSONResponse(body, status_code=exc.status)


async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Rend les erreurs de Starlette (route inconnue, méthode refusée) comme celles d'OpenAI."""
    error_type = "invalid_request_error" if exc.status_code < 500 else "server_error"
    return JSONResponse(
        envelope(exc.detail, type=error_type),
        status_code=exc.status_code,
        headers=exc.headers,
    )
