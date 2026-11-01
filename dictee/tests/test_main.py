import logging

import pytest

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
