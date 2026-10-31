import logging
from pathlib import Path

import anyio
import httpx
from starlette.testclient import TestClient

from dictee.app import create_app
from dictee.db import Database
from dictee.errors import ApiError
from dictee.modes import MODELS
from dictee.transcribe import LongAudioGate


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_models_lists_the_three_transcription_models(client, service_headers):
    body = client.get("/v1/models", headers=service_headers).json()
    assert body["object"] == "list"
    assert {m["id"] for m in body["data"]} == {"dictee", "dictee-clean", "dictee-diarize"}


def test_models_follow_the_openai_model_shape(client, service_headers):
    for model in client.get("/v1/models", headers=service_headers).json()["data"]:
        assert model["object"] == "model"
        assert model["created"] == 0
        assert model["owned_by"] == "dictee"


def test_v1_routes_require_a_key(client, service_headers):
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers=service_headers).status_code == 200


def test_every_v1_route_refuses_a_call_without_a_key(client):
    # Garde pour les routes à venir : toute route /v1 ajoutée à l'app doit exiger une clé
    # avant de lire le corps de la requête.
    v1_routes = [route for route in client.app.routes if route.path.startswith("/v1/")]
    assert v1_routes
    for route in v1_routes:
        for method in route.methods - {"HEAD"}:
            r = client.request(method, route.path)
            assert r.status_code == 401, f"{method} {route.path} answered {r.status_code} without a key"


def test_refused_key_uses_the_openai_envelope(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer dk_unknown"})
    assert r.status_code == 401
    error = r.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_api_key"
    assert error["param"] is None
    assert "dk_unknown" not in error["message"]


def test_personal_key_opens_the_v1_routes_until_it_is_revoked(client, db):
    uid = db.upsert_user("sub-1", "a@example.org", "A")
    key = db.create_key(uid, "Android")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/models", headers=headers).status_code == 200
    db.revoke_key(uid, db.list_keys(uid)[0].id)
    assert client.get("/v1/models", headers=headers).status_code == 401


def test_unknown_route_uses_the_openai_envelope(client):
    r = client.get("/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_envelope_always_carries_the_four_openai_fields(client):
    error = client.get("/v1/nope").json()["error"]
    assert error == {
        "message": "Not Found",
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    }


def test_method_not_allowed_keeps_its_allow_header(client):
    r = client.post("/health")
    assert r.status_code == 405
    assert r.json()["error"]["message"] == "Method Not Allowed"
    assert "GET" in r.headers["allow"]


async def _raise_api_error(request):
    raise ApiError(400, "input must not be empty", param="input", code="empty_input")


def test_api_error_is_rendered_in_the_openai_envelope(settings):
    app = create_app(settings)
    app.add_route("/boom", _raise_api_error)
    with TestClient(app) as test_client:
        r = test_client.get("/boom")
    assert r.status_code == 400
    assert r.json() == {
        "error": {
            "message": "input must not be empty",
            "type": "invalid_request_error",
            "param": "input",
            "code": "empty_input",
        }
    }


def test_app_keeps_settings_and_creates_its_own_http_client(settings):
    app = create_app(settings)
    assert app.state.settings is settings
    assert app.state.nemo is None
    with TestClient(app):
        http = app.state.http
        assert isinstance(http, httpx.AsyncClient)
        assert not http.is_closed
    assert http.is_closed


def test_models_route_lists_the_models_of_the_modes_module(client, service_headers):
    body = client.get("/v1/models", headers=service_headers).json()
    assert tuple(model["id"] for model in body["data"]) == MODELS


def test_app_builds_the_nemo_client_at_startup_over_its_own_http_client(settings, monkeypatch):
    built = []
    monkeypatch.setattr("dictee.app.NemoClient", lambda *args: built.append(args) or "nemo client")
    app = create_app(settings)
    assert app.state.nemo is None
    with TestClient(app):
        assert app.state.nemo == "nemo client"
        assert built == [(settings.nemo_url, settings.nemo_api_key, app.state.http)]


def test_app_builds_the_nemo_client_over_an_injected_http_client(settings, monkeypatch):
    built = []
    monkeypatch.setattr("dictee.app.NemoClient", lambda *args: built.append(args) or "nemo client")
    http = httpx.AsyncClient()
    with TestClient(create_app(settings, http=http)):
        assert built == [(settings.nemo_url, settings.nemo_api_key, http)]
    anyio.run(http.aclose)


def test_app_has_one_long_audio_gate_set_from_the_settings(settings):
    app = create_app(settings)
    assert isinstance(app.state.gate, LongAudioGate)
    assert app.state.gate.threshold_s == settings.long_audio_seconds


async def _fail(request):
    raise RuntimeError("secret transcript text")


def test_unexpected_error_is_a_generic_500_in_the_openai_envelope(settings, caplog):
    app = create_app(settings)
    app.add_route("/boom", _fail)
    caplog.set_level(logging.INFO)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        r = test_client.get("/boom")
    assert r.status_code == 500
    assert r.json() == {
        "error": {
            "message": "The server failed to process the request.",
            "type": "server_error",
            "param": None,
            "code": None,
        }
    }
    assert "RuntimeError" in caplog.text
    assert "secret transcript text" not in caplog.text
    assert "secret transcript text" not in r.text


def test_app_opens_the_database_at_the_configured_path(settings):
    app = create_app(settings)
    assert isinstance(app.state.db, Database)
    assert Path(settings.db_path).exists()


def test_injected_clients_are_used_and_left_open(settings):
    nemo = object()
    http = httpx.AsyncClient()
    app = create_app(settings, nemo=nemo, http=http)
    with TestClient(app):
        assert app.state.nemo is nemo
        assert app.state.http is http
    assert not http.is_closed
    anyio.run(http.aclose)
