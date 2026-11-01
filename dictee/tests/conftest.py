import math
import struct
import subprocess
import wave
from pathlib import Path

import anyio
import pytest
from fakes import FINALS, FakeLlm, FakeNemo
from openai import OpenAI
from starlette.testclient import TestClient
from taskdata import make

from dictee import processing
from dictee.app import create_app
from dictee.db import Database
from dictee.modes import Mode
from dictee.processing import Services, task_audio
from dictee.profile import GlossaryEntry
from dictee.settings import Settings
from dictee.taskstore import TaskParams, TaskStore

RATE = 16000

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
def services(store, fake_nemo, settings, fake_llm):
    """Ce que le traitement d'une tâche utilise : le registre, le faux NeMo, les réglages et le client du faux LiteLLM."""
    return Services(store, fake_nemo, settings, fake_llm.http)


def write_wav(path, seconds: float, frequency: int = 400) -> None:
    """WAV canonique mono 16 kHz d'un sinus à `frequency` Hz : sans silence, ffmpeg n'y repère aucune coupe."""
    # Une fréquence entière fait un nombre entier de périodes par seconde : les secondes se mettent bout à bout sans
    # raccord.
    samples = (round(8000 * math.sin(2 * math.pi * frequency * n / RATE)) for n in range(RATE))
    one_second = struct.pack(f"<{RATE}h", *samples)
    pcm = (one_second * math.ceil(seconds))[: round(seconds * RATE) * 2]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)


def audio_task(services, seconds: float, mode: Mode = Mode.RAW):
    """Une tâche en file de `seconds` secondes, dont le WAV est déjà à sa place."""
    params = TaskParams("fr-FR", mode, (GlossaryEntry("Nemotron"),), 1.0, "Corrige." if mode is Mode.CLEAN else None)
    task = make(services.store, params=params, duration=seconds)
    audio = task_audio(services.settings, task.id)
    audio.parent.mkdir(parents=True)
    write_wav(audio, seconds)
    return task


@pytest.fixture
def no_retry_delays(monkeypatch):
    """Retire les pauses des reprises auprès de NeMo : ni attente entre deux essais, ni attente de son retour.

    Sans `autouse` : un fichier de tests l'applique à tous ses tests par `pytestmark`, un test seul par son argument.
    """
    monkeypatch.setattr(processing, "RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(processing, "READY_POLL_S", 0)


@pytest.fixture
def service_headers(settings):
    return {"Authorization": f"Bearer {settings.openwebui_service_key}"}


def personal_headers(db: Database, email: str) -> tuple[dict[str, str], int]:
    """Crée le compte de `email` avec une clé personnelle ; rend les en-têtes d'un appel à son nom et son numéro."""
    user_id = db.upsert_user(f"sub-{email}", email, email.partition("@")[0])
    return {"Authorization": f"Bearer {db.create_key(user_id, 'tests')}"}, user_id


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
