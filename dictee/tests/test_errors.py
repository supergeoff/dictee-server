import pytest

from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure
from dictee.nemo import NemoError, NemoOverloaded, NemoUnavailable

FAILURES = [
    (NemoOverloaded("the queue is full"), 429, "rate_limit_error", "overloaded"),
    (NemoUnavailable("connection refused"), 502, "server_error", "upstream_error"),
    (NemoError("session failed"), 502, "server_error", "upstream_error"),
]


@pytest.mark.parametrize("error,status,error_type,code", FAILURES)
def test_nemo_failure_is_a_429_when_nemo_is_busy_and_a_502_otherwise(error, status, error_type, code):
    failure = nemo_failure(error)
    assert isinstance(failure, ApiError)
    assert (failure.status, failure.type, failure.code, failure.param) == (status, error_type, code, None)
    assert str(error) not in failure.message


def test_nemo_errors_catches_every_failure_of_nemo():
    assert set(NEMO_ERRORS) == {NemoOverloaded, NemoUnavailable, NemoError}
    assert all(isinstance(error, NEMO_ERRORS) for error, *_ in FAILURES)
    assert not isinstance(RuntimeError("other"), NEMO_ERRORS)
