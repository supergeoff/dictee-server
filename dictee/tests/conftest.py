import pytest
from starlette.testclient import TestClient

from dictee.app import create_app
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
def settings(tmp_path):
    return Settings.from_env({**REQUIRED_ENV, "DB_PATH": str(tmp_path / "dictee.db")})


@pytest.fixture
def client(settings):
    # Le bloc `with` déclenche le démarrage et l'arrêt de l'app (lifespan).
    with TestClient(create_app(settings), base_url="https://testserver") as test_client:
        yield test_client
