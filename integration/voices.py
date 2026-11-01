# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1"]
# ///
"""Fait dire la même phrase par chacune des cinq voix de Magpie, pour choisir la voix par défaut (tâche 14).

Prérequis : `make integration` (dictee répond sur http://127.0.0.1:18000).
Usage : uv run --script integration/voices.py [--no-open]

Le script appelle `POST /v1/audio/speech` de dictee en `mp3` avec la clé de service (`OPENWEBUI_SERVICE_KEY` du `.env`,
jamais affichée), écrit `.integration/voices/<Voix>.mp3` et ouvre le dossier avec `open` (macOS), sauf avec `--no-open`.
Chaque ligne donne la taille du fichier et la durée de la synthèse.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".integration" / "voices"
BASE_URL = "http://127.0.0.1:18000"
VOICES = ["John", "Sofia", "Aria", "Jason", "Leo"]
TEXT = "Bonjour, voici la voix qui lira vos textes. Elle servira aussi au mode appel d'Open WebUI."
# Début d'un mp3 : une étiquette ID3, ou une trame MPEG couche 3.
MP3_SIGNATURES = (b"ID3", b"\xff\xfb", b"\xff\xf3")


def service_key() -> str:
    """Lit la clé de service dans le .env du dépôt, sans l'afficher."""
    for line in (ROOT / ".env").read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "OPENWEBUI_SERVICE_KEY":
            return value.strip()
    raise SystemExit("OPENWEBUI_SERVICE_KEY absente de .env (lancer `make integration`)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-open", action="store_true", help="n'ouvre pas le dossier à la fin")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {service_key()}"}
    OUT.mkdir(parents=True, exist_ok=True)
    failed = False
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=httpx.Timeout(300.0, connect=10.0)) as http:
        for voice in VOICES:
            started = time.monotonic()
            response = http.post("/v1/audio/speech", json={"input": TEXT, "voice": voice, "response_format": "mp3"})
            seconds = time.monotonic() - started
            if response.status_code != 200:
                print(f"ÉCHEC {voice} : HTTP {response.status_code} {response.text[:200]}", flush=True)
                failed = True
                continue
            if not response.content.startswith(MP3_SIGNATURES):
                print(f"ÉCHEC {voice} : la réponse commence par {response.content[:4]!r}, pas par un mp3", flush=True)
                failed = True
                continue
            path = OUT / f"{voice}.mp3"
            path.write_bytes(response.content)
            print(f"OK {voice} : {len(response.content)} octets, synthèse en {seconds:.1f} s, {path}", flush=True)
    print(f"Dossier : {OUT}", flush=True)
    if not failed and not args.no_open:
        subprocess.run(["open", str(OUT)], check=False)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
