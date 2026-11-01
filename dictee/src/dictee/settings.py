"""Réglages de dictee, lus dans les variables d'environnement."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Champ de `Settings` -> variable d'environnement, pour les réglages sans valeur par défaut.
_REQUIRED = {
    "nemo_api_key": "NEMO_API_KEY",
    "litellm_url": "LITELLM_URL",
    "litellm_api_key": "LITELLM_API_KEY",
    "openwebui_service_key": "OPENWEBUI_SERVICE_KEY",
    "oidc_issuer": "OIDC_ISSUER",
    "oidc_client_id": "OIDC_CLIENT_ID",
    "oidc_client_secret": "OIDC_CLIENT_SECRET",
    "public_url": "PUBLIC_URL",
    "session_secret": "SESSION_SECRET",
}

# Champ -> (variable d'environnement, type). La valeur par défaut est celle du champ de `Settings`.
_OPTIONAL = {
    "nemo_url": ("NEMO_URL", str),
    "cleanup_model": ("CLEANUP_MODEL", str),
    "default_voice": ("DEFAULT_VOICE", str),
    "glossary_boost": ("GLOSSARY_BOOST", float),
    "long_audio_seconds": ("LONG_AUDIO_SECONDS", float),
    "max_upload_mb": ("MAX_UPLOAD_MB", int),
    "max_queued_long": ("MAX_QUEUED_LONG", int),
    "task_retention_hours": ("TASK_RETENTION_HOURS", float),
    "db_path": ("DB_PATH", str),
}


def _is_set(value: str | None) -> bool:
    return value is not None and value.strip() != ""


@dataclass(frozen=True)
class Settings:
    nemo_api_key: str
    litellm_url: str
    litellm_api_key: str
    openwebui_service_key: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    public_url: str
    session_secret: str
    nemo_url: str = "http://nemo:8000"
    cleanup_model: str = "simple-low"
    default_voice: str = "Leo"
    glossary_boost: float = 2.0
    long_audio_seconds: float = 600.0
    max_upload_mb: int = 300
    # Tâches longues en attente, en plus de celle qui tourne ; au-delà, l'envoi reçoit 429 `long_audio_busy`.
    max_queued_long: int = 3
    # Durée de conservation d'un résultat, comptée depuis la fin de la tâche (réussie, en échec ou annulée).
    task_retention_hours: float = 24.0
    db_path: str = "/data/dictee.db"

    @property
    def tasks_dir(self) -> Path:
        """Dossier des tâches : `tasks` à côté de la base, où chaque tâche garde son audio le temps du traitement."""
        return Path(self.db_path).parent / "tasks"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Lit les réglages ; une variable vide compte comme absente.

        Lève `ValueError` en nommant toutes les variables obligatoires absentes,
        ou la variable facultative dont la valeur n'a pas le bon type.
        """
        missing = [name for name in _REQUIRED.values() if not _is_set(env.get(name))]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))

        values: dict[str, object] = {field: env[name] for field, name in _REQUIRED.items()}
        for field, (name, kind) in _OPTIONAL.items():
            raw = env.get(name)
            if not _is_set(raw):
                continue
            try:
                values[field] = kind(raw)
            except ValueError:
                raise ValueError(f"{name} must be a {kind.__name__}, got {raw!r}") from None
        return cls(**values)  # type: ignore[arg-type]
