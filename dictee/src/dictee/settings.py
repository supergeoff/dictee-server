"""Réglages de dictee, lus dans les variables d'environnement."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Champ de `Settings` -> variable d'environnement, pour les réglages sans valeur par défaut.
_REQUIRED = {
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
    "nemo_api_key": ("NEMO_API_KEY", str),
    "crispasr_url": ("CRISPASR_URL", str),
    "crispasr_api_key": ("CRISPASR_API_KEY", str),
    "stt_engine": ("STT_ENGINE", str),
    "live_engine": ("LIVE_ENGINE", str),
    "tts_engine": ("TTS_ENGINE", str),
    "live_command": ("LIVE_COMMAND", str),
    "live_model_path": ("LIVE_MODEL_PATH", str),
    "live_vad_model_path": ("LIVE_VAD_MODEL_PATH", str),
    "live_max_sessions": ("LIVE_MAX_SESSIONS", int),
    "live_threads": ("LIVE_THREADS", int),
    "live_stream_args": ("LIVE_STREAM_ARGS", str),
    "tts_url": ("TTS_URL", str),
    "tts_sample_rate": ("TTS_SAMPLE_RATE", int),
    "tts_max_concurrent": ("TTS_MAX_CONCURRENT", int),
    "cleanup_model": ("CLEANUP_MODEL", str),
    "default_voice": ("DEFAULT_VOICE", str),
    "glossary_boost": ("GLOSSARY_BOOST", float),
    "long_audio_seconds": ("LONG_AUDIO_SECONDS", float),
    "max_upload_mb": ("MAX_UPLOAD_MB", int),
    "max_queued_long": ("MAX_QUEUED_LONG", int),
    "task_retention_hours": ("TASK_RETENTION_HOURS", float),
    "db_path": ("DB_PATH", str),
}


# Réglages qui bornent une file : à 0 ou moins, tout appel attendrait indéfiniment.
_AT_LEAST_ONE = {"tts_max_concurrent": "TTS_MAX_CONCURRENT", "live_max_sessions": "LIVE_MAX_SESSIONS"}


def _is_set(value: str | None) -> bool:
    return value is not None and value.strip() != ""


@dataclass(frozen=True)
class Settings:
    litellm_url: str
    litellm_api_key: str
    openwebui_service_key: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    public_url: str
    session_secret: str
    nemo_url: str = "http://nemo:8000"
    # Clés des moteurs : obligatoires seulement quand un moteur qui s'en sert est choisi (voir `from_env`).
    nemo_api_key: str = ""
    crispasr_url: str = "http://crispasr:8080"
    crispasr_api_key: str = ""
    # Moteur de chaque rôle audio (voir `dictee.engines`) : transcription de fichier, dictée en direct, synthèse.
    stt_engine: str = "nemo"
    live_engine: str = "nemo"
    tts_engine: str = "nemo"
    # Dictée en direct par un processus CrispASR : commande, modèle de transcription, modèle de détection de voix,
    # nombre de sessions et de threads, et réglages de fenêtre (`LIVE_STREAM_ARGS`, gardés en chaîne : l'adaptateur
    # les découpe lui-même).
    live_command: str = "crispasr"
    live_model_path: str = "/models/current/asr.gguf"
    live_vad_model_path: str = "/models/current/vad.bin"
    live_max_sessions: int = 2
    live_threads: int = 4
    live_stream_args: str = ""
    # Synthèse vocale par un serveur compatible OpenAI : adresse, fréquence d'échantillonnage du PCM et requêtes
    # simultanées.
    tts_url: str = "http://phonon:8880"
    tts_sample_rate: int = 24000
    tts_max_concurrent: int = 1
    cleanup_model: str = "simple"
    default_voice: str = "Leo"
    # Force du glossaire : la plus haute sans insertion sur le corpus de `integration/boost.py` (spec 7.6, tâche 13).
    glossary_boost: float = 1.0
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
        engine_variables = ("STT_ENGINE", "LIVE_ENGINE", "TTS_ENGINE")
        engines = {name: env[name] if _is_set(env.get(name)) else "nemo" for name in engine_variables}
        needed = list(_REQUIRED.values())
        if "nemo" in engines.values():
            needed.append("NEMO_API_KEY")
        if engines["STT_ENGINE"] == "crispasr":
            needed.append("CRISPASR_API_KEY")
        missing = [name for name in needed if not _is_set(env.get(name))]
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
        for field, name in _AT_LEAST_ONE.items():
            if values.get(field, 1) < 1:
                raise ValueError(f"{name} must be at least 1, got {values[field]!r}")
        return cls(**values)  # type: ignore[arg-type]
