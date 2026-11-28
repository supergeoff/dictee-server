import pytest

from dictee.engines.ports import ENGINE_ERRORS, EngineError, EngineOverloaded, EngineUnavailable
from dictee.errors import ApiError, engine_failure

FAILURES = [
    (EngineOverloaded("the queue is full"), 429, "rate_limit_error", "overloaded"),
    (EngineUnavailable("connection refused"), 502, "server_error", "upstream_error"),
    (EngineError("session failed"), 502, "server_error", "upstream_error"),
]


@pytest.mark.parametrize("error,status,error_type,code", FAILURES)
def test_nemo_failure_is_a_429_when_nemo_is_busy_and_a_502_otherwise(error, status, error_type, code):
    failure = engine_failure(error)
    assert isinstance(failure, ApiError)
    assert (failure.status, failure.type, failure.code, failure.param) == (status, error_type, code, None)
    assert str(error) not in failure.message


def test_nemo_errors_catches_every_failure_of_nemo():
    assert set(ENGINE_ERRORS) == {EngineOverloaded, EngineUnavailable, EngineError}
    assert all(isinstance(error, ENGINE_ERRORS) for error, *_ in FAILURES)
    assert not isinstance(RuntimeError("other"), ENGINE_ERRORS)
