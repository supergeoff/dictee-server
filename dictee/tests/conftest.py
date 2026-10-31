import pytest
from starlette.testclient import TestClient

from dictee.app import create_app
from dictee.db import Database
from dictee.settings import Settings

REQUIRED_ENV = {
    "NEMO_API_KEY": "test-nemo-key",
    "LITELLM_URL": "http://litellm.test",
    "LITELLM_API_KEY": "test-litellm-key",
    "OPENWEBUI_SERVICE_KEY": "test-openwebui-key",
    "OIDC_ISSUER": "https://id.test",
    "OIDC_CLIENT_ID": "dictee",
    "OIDC_CLIENT_SECRET": "test-oidc-secret",
    "PUBLIC_URL": "https://dictee.test",
    "SESSION_SECRET": "test-session-secret",
}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "dictee.db")


@pytest.fixture
def settings(db_path):
    return Settings.from_env({**REQUIRED_ENV, "DB_PATH": db_path})


@pytest.fixture
def db(db_path):
    # Même fichier que celui de l'app : la base ouvre une connexion par opération.
    return Database(db_path)


@pytest.fixture
def service_headers(settings):
    return {"Authorization": f"Bearer {settings.openwebui_service_key}"}


@pytest.fixture
def client(settings):
    # Le bloc `with` déclenche le démarrage et l'arrêt de l'app (lifespan).
    with TestClient(create_app(settings), base_url="https://testserver") as test_client:
        yield test_client
