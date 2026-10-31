"""Identification de l'appelant d'une route `/v1`."""

import hmac
from dataclasses import dataclass

from starlette.requests import Request

from dictee.db import Database
from dictee.errors import ApiError
from dictee.settings import Settings


@dataclass(frozen=True)
class Caller:
    """Compte désigné par l'appel ; `None` signifie « valeurs par défaut »."""

    user_id: int | None


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def resolve_caller(
    db: Database, settings: Settings, authorization: str | None, openwebui_email: str | None
) -> Caller:
    """Désigne l'appelant d'après sa clé.

    Une clé personnelle désigne son propriétaire et l'email d'Open WebUI est ignoré. La clé de
    service d'Open WebUI désigne le compte dont l'email égale `openwebui_email`, sans tenir compte
    de la casse, ou les valeurs par défaut quand l'email manque ou est inconnu. Une clé absente,
    inconnue ou révoquée est refusée.
    """
    token = _bearer_token(authorization)
    if token is None:
        raise ApiError(
            401,
            "You didn't provide an API key. Send it in the Authorization header as 'Bearer <key>'.",
            code="invalid_api_key",
        )
    if hmac.compare_digest(token.encode(), settings.openwebui_service_key.encode()):
        return Caller(db.user_by_email(openwebui_email) if openwebui_email else None)
    user_id = db.key_owner(token)
    if user_id is None:
        raise ApiError(401, "Incorrect API key provided.", code="invalid_api_key")
    return Caller(user_id)


def require_caller(request: Request) -> Caller:
    """Identifie l'appelant d'une requête, ou lève une erreur 401.

    Chaque route `/v1` l'appelle avant de lire le corps de la requête.
    """
    return resolve_caller(
        request.app.state.db,
        request.app.state.settings,
        request.headers.get("authorization"),
        request.headers.get("x-openwebui-user-email"),
    )
