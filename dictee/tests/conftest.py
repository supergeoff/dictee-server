import subprocess
from pathlib import Path

import anyio
import pytest
from fakes import FINALS, FakeLlm, FakeNemo
from openai import OpenAI
from starlette.testclient import TestClient

from dictee.app import create_app
from dictee.db import Database
from dictee.settings import Settings
from dictee.taskstore import TaskStore

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
def anyio_backend():
    # Les tests asynchrones tournent sur asyncio seulement : trio n'est pas installé.
    return "asyncio"


async def wait_until(condition, timeout: float = 2.0) -> None:
    """Attend, sans bloquer la boucle, que `condition()` devienne vraie ; échoue après `timeout` secondes."""
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(0.01)


def make_audio(
    directory: Path, name: str, *, seconds: float = 1.0, rate: int = 16000, channels: int = 1, codec: str | None = None
) -> Path:
    """Fabrique un fichier audio d'un sinus avec ffmpeg ; le format suit l'extension de `name`.

    Sans `codec`, ffmpeg prend celui que le format de sortie choisit par défaut.
    """
    path = directory / name
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
            "-ac",
            str(channels),
            *(["-c:a", codec] if codec else []),
            "-y",
            str(path),
        ],
        check=True,
    )
    return path


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
def store(db):
    """Registre des tâches sur la base du test."""
    return TaskStore(db)


@pytest.fixture
def service_headers(settings):
    return {"Authorization": f"Bearer {settings.openwebui_service_key}"}


@pytest.fixture
def client(settings):
    # Le bloc `with` déclenche le démarrage et l'arrêt de l'app (lifespan).
    with TestClient(create_app(settings), base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def fake_nemo():
    return FakeNemo(finals=FINALS)


@pytest.fixture
def fake_llm():
    llm = FakeLlm()
    yield llm
    anyio.run(llm.http.aclose)


@pytest.fixture
def transcriptions_client(settings, fake_nemo, fake_llm):
    """Client HTTP d'une app qui parle au faux NeMo et au faux LiteLLM."""
    app = create_app(settings, nemo=fake_nemo, http=fake_llm.http)
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def openai_client(settings, transcriptions_client):
    """Le SDK d'OpenAI face à l'app, avec la clé de service d'Open WebUI ; sans reprise, pour voir chaque erreur."""
    return OpenAI(
        api_key=settings.openwebui_service_key,
        base_url="https://testserver/v1",
        http_client=transcriptions_client,
        max_retries=0,
    )


@pytest.fixture(scope="session")
def tone_mp3(tmp_path_factory):
    """Trois secondes de sinus en MP3."""
    return make_audio(tmp_path_factory.mktemp("tone"), "tone.mp3", seconds=3.0).read_bytes()
