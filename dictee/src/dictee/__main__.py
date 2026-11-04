"""Lance dictee avec uvicorn, réglé par les variables d'environnement."""

import logging
import os

import uvicorn

from dictee.app import create_app
from dictee.settings import Settings

# Délai laissé aux requêtes en cours à l'arrêt, plus court que le `stop_grace_period` de dictee dans compose.yaml.
GRACEFUL_SHUTDOWN_S = 20


def configure_logging() -> None:
    """Journal de dictee à INFO, sur la sortie d'erreur ; uvicorn règle déjà les siens.

    httpx écrit à INFO l'adresse de chaque appel, et httpx2, le client qu'Authlib charge à sa place, fait de même sous
    son propre nom : la sortie des deux est réduite aux avertissements, car la ligne de dictee dit déjà ce qu'il faut
    sur chaque requête.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for client in ("httpx", "httpx2"):
        logging.getLogger(client).setLevel(logging.WARNING)


def main() -> None:
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as error:
        raise SystemExit(f"configuration error: {error}") from None
    configure_logging()
    # Pas de journal d'accès : les journaux ne portent jamais de contenu des requêtes. À l'arrêt, uvicorn laisse
    # `GRACEFUL_SHUTDOWN_S` secondes aux requêtes en cours, puis arrête l'app : la boucle de travail des tâches s'arrête
    # ainsi avant le SIGKILL que Docker envoie à la fin de `stop_grace_period` (30 s dans compose.yaml).
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8000,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )


if __name__ == "__main__":
    main()
