import pytest
from conftest import REQUIRED_ENV

from dictee.settings import Settings


def test_defaults_apply_when_optional_variables_are_missing():
    s = Settings.from_env(REQUIRED_ENV)
    assert s.cleanup_model == "simple-low"
    assert s.long_audio_seconds == 600.0
    assert s.glossary_boost == 2.0
    assert s.max_upload_mb == 300
    assert s.nemo_url == "http://nemo:8000"
    assert s.db_path == "/data/dictee.db"
    assert s.default_voice == "Leo"


def test_required_variables_are_read_from_the_environment():
    s = Settings.from_env(REQUIRED_ENV)
    assert s.nemo_api_key == "test-nemo-key"
    assert s.litellm_url == "http://litellm.test"
    assert s.litellm_api_key == "test-litellm-key"
    assert s.openwebui_service_key == "test-openwebui-key"
    assert s.oidc_issuer == "https://id.test"
    assert s.oidc_client_id == "dictee"
    assert s.oidc_client_secret == "test-oidc-secret"
    assert s.public_url == "https://dictee.test"
    assert s.session_secret == "test-session-secret"


def test_optional_variables_override_the_defaults():
    s = Settings.from_env(
        {
            **REQUIRED_ENV,
            "NEMO_URL": "http://nemo.test:9000",
            "CLEANUP_MODEL": "other-model",
            "DEFAULT_VOICE": "Aria",
            "GLOSSARY_BOOST": "4.5",
            "LONG_AUDIO_SECONDS": "120",
            "MAX_UPLOAD_MB": "50",
            "DB_PATH": "/tmp/dictee.db",
        }
    )
    assert s.nemo_url == "http://nemo.test:9000"
    assert s.cleanup_model == "other-model"
    assert s.default_voice == "Aria"
    assert s.glossary_boost == 4.5
    assert s.long_audio_seconds == 120.0
    assert s.max_upload_mb == 50
    assert s.db_path == "/tmp/dictee.db"


def test_missing_required_variable_is_named():
    env = {k: v for k, v in REQUIRED_ENV.items() if k != "NEMO_API_KEY"}
    with pytest.raises(ValueError, match="NEMO_API_KEY"):
        Settings.from_env(env)


def test_every_missing_required_variable_is_named_at_once():
    with pytest.raises(ValueError) as error:
        Settings.from_env({})
    for name in REQUIRED_ENV:
        assert name in str(error.value)


def test_blank_required_variable_counts_as_missing():
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings.from_env({**REQUIRED_ENV, "SESSION_SECRET": "  "})


def test_invalid_number_is_reported_by_variable_name():
    with pytest.raises(ValueError, match="MAX_UPLOAD_MB"):
        Settings.from_env({**REQUIRED_ENV, "MAX_UPLOAD_MB": "beaucoup"})


def test_settings_are_frozen():
    s = Settings.from_env(REQUIRED_ENV)
    with pytest.raises(AttributeError):
        s.nemo_url = "http://elsewhere"  # type: ignore[misc]
