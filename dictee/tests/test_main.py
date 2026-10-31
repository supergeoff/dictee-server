import logging

import pytest

from dictee.__main__ import configure_logging


@pytest.fixture
def httpx_level():
    logger = logging.getLogger("httpx")
    level = logger.level
    yield logger
    logger.setLevel(level)


def test_httpx_stays_quiet_at_info(httpx_level):
    # httpx écrit l'adresse de chaque appel à INFO : le journal de dictee suffit.
    httpx_level.setLevel(logging.INFO)
    configure_logging()
    assert httpx_level.level == logging.WARNING
