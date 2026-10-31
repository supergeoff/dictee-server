"""Lance dictee avec uvicorn, réglé par les variables d'environnement."""

import os

import uvicorn

from dictee.app import create_app
from dictee.settings import Settings


def main() -> None:
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as error:
        raise SystemExit(f"configuration error: {error}") from None
    # Pas de journal d'accès : les journaux ne portent jamais de contenu des requêtes.
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    main()
