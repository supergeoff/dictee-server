import logging

import pytest
import uvicorn
from conftest import REQUIRED_ENV

from dictee import __main__ as main_module
from dictee.__main__ import configure_logging


@pytest.fixture(params=["httpx", "httpx2"])
def http_logger(request):
    # httpx2 est le client d'Authlib, httpx celui de dictee : chacun écrit l'adresse de chaque appel à INFO.
    logger = logging.getLogger(request.param)
    level = logger.level
    yield logger
    logger.setLevel(level)


def test_http_clients_stay_quiet_at_info(http_logger):
    # Le journal de dictee suffit : les adresses d'appel n'ont pas à y ajouter les leurs.
    http_logger.setLevel(logging.INFO)
    configure_logging()
    assert http_logger.level == logging.WARNING


def main_with_test_env(monkeypatch) -> None:
    """Lance `main` avec la configuration des tests ; l'app n'est pas construite, uvicorn reçoit un objet vide."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main_module, "create_app", lambda settings: object())
    main_module.main()


def test_uvicorn_stops_before_docker_kills_the_container(monkeypatch):
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
    main_with_test_env(monkeypatch)
    assert seen["timeout_graceful_shutdown"] == 20
