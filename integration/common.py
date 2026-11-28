"""Aides des scripts d'intégration qui ne dépendent d'aucun moteur : racine du dépôt, `.env`, WAV et compose.

Le module n'importe que la bibliothèque standard : un script qui l'importe n'a besoin d'aucune dépendance de plus.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def env_value(name: str) -> str:
    """La variable `name` de l'environnement si elle existe, sinon sa valeur dans le `.env` du dépôt.

    C'est la seule lecture du `.env` des scripts d'intégration. La valeur n'est jamais affichée : le message d'erreur
    ne cite que son nom. Un conteneur jetable, qui n'a pas de `.env`, passe la valeur par l'environnement.
    """
    if os.environ.get(name):
        return os.environ[name]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip()
    raise RuntimeError(f"{name} absente de l'environnement et de .env (lancer `make integration`)")


def parse_wav(data: bytes) -> tuple[int, bytes]:
    """Rend (fréquence, PCM16 mono) d'un WAV PCM16, en lisant l'en-tête RIFF."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("réponse qui n'est pas un WAV")
    rate = channels = bits = 0
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            _fmt, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", data[body : body + 16])
        elif chunk_id == b"data":
            if (channels, bits) != (1, 16):
                raise ValueError(f"WAV attendu en mono PCM16, reçu {channels} canaux, {bits} bits")
            return rate, data[body : body + min(size, len(data) - body)]
        pos = body + size + (size & 1)
    raise ValueError("WAV sans bloc data")


def wav_file(pcm: bytes, rate: int) -> bytes:
    """Enveloppe du PCM16 mono dans un en-tête WAV."""
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


def compose_base() -> list[str]:
    """Le binaire compose : `COMPOSE` s'il est fixé, sinon le plugin `docker compose`, sinon `docker-compose`."""
    if os.environ.get("COMPOSE"):
        return os.environ["COMPOSE"].split()
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"]


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    """Lance docker compose (plugin) ou docker-compose (binaire seul) sur le compose d'intégration."""
    cmd = [*compose_base(), "-f", "compose.integration.yaml", *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
