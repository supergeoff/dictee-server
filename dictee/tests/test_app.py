import anyio
import httpx
from starlette.testclient import TestClient

from dictee.app import create_app
from dictee.errors import ApiError


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_models_lists_the_three_transcription_models(client):
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert {m["id"] for m in body["data"]} == {"dictee", "dictee-clean", "dictee-diarize"}


def test_models_follow_the_openai_model_shape(client):
    for model in client.get("/v1/models").json()["data"]:
        assert model["object"] == "model"
        assert model["created"] == 0
        assert model["owned_by"] == "dictee"


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


def test_injected_clients_are_used_and_left_open(settings):
    nemo = object()
    http = httpx.AsyncClient()
    app = create_app(settings, nemo=nemo, http=http)
    with TestClient(app):
        assert app.state.nemo is nemo
        assert app.state.http is http
    assert not http.is_closed
    anyio.run(http.aclose)
