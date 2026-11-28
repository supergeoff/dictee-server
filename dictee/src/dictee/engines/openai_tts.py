"""Client d'un serveur de voix compatible avec `POST /v1/audio/speech` d'OpenAI, comme celui de phonon.

Le serveur n'a pas d'authentification : aucune clé n'est envoyée. Rien n'est journalisé ici : ni le texte à dire, ni
l'audio. Les messages des exceptions ne reprennent jamais le corps d'une réponse, qui peut citer le texte.
"""

import asyncio
import time

import httpx

from dictee.engines.ports import EngineError, EngineOverloaded, EngineUnavailable

# Délais en secondes. Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
OPEN_TIMEOUT = 10.0  # ouverture d'une connexion HTTP
SPEECH_TIMEOUT = 120.0  # lecture du son d'une phrase (300 caractères au plus)
VOICES_TIMEOUT = 10.0
READY_TIMEOUT = 5.0
# Le serveur de voix refuse (429) tout appel au-delà de ce qu'il traite : un refus reçu malgré la file de l'adaptateur
# est réessayé toutes les `RETRY_INTERVAL_S` secondes pendant `RETRY_MAX_S` au plus, puis devient « saturé ».
RETRY_INTERVAL_S = 0.5
RETRY_MAX_S = 10.0
# Le serveur de voix ne choisit pas de modèle d'après ce champ, mais l'API d'OpenAI l'exige.
MODEL = "tts-1"


class OpenAISpeech:
    """Les appels de dictee au serveur de voix ; `http` est le client HTTP partagé de l'app.

    Il joue le rôle `Synthesizer`. `sample_rate` est la fréquence du PCM16 mono que le serveur rend (24 kHz pour
    phonon). Un sémaphore de `max_concurrent` places borne les appels à `speak` : le serveur de voix ne traite qu'une
    synthèse à la fois par défaut, les suivantes attendent leur tour ici. La vitesse n'est pas transmise : dictee
    l'applique lui-même à l'encodage.
    """

    def __init__(self, base_url: str, http: httpx.AsyncClient, *, sample_rate: int, max_concurrent: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._slots = asyncio.Semaphore(max_concurrent)
        self.sample_rate = sample_rate

    async def _request(self, method: str, path: str, *, timeout: float, **kwargs) -> httpx.Response:
        """Appel d'une route HTTP du serveur de voix.

        Un échec de transport ou un 503 donne `EngineUnavailable`, un 429 `EngineOverloaded`. Toute autre erreur (une
        voix inconnue donne 400) donne `EngineError`, dont le message ne porte que le code HTTP.
        """
        try:
            response = await self._http.request(
                method,
                self._base_url + path,
                timeout=httpx.Timeout(timeout, connect=OPEN_TIMEOUT),
                **kwargs,
            )
        except httpx.TransportError as exc:
            raise EngineUnavailable(f"The voice server is unreachable: {exc!r}") from exc
        if response.status_code == 503:
            raise EngineUnavailable("The voice server is not ready (HTTP 503)")
        if response.status_code == 429:
            raise EngineOverloaded("The voice server is busy")
        if response.is_error:
            raise EngineError(f"The voice server answered HTTP {response.status_code}")
        return response

    async def speak(self, text: str, *, voice: str, language: str) -> bytes:
        """Synthétise `text` : du PCM16 mono à `sample_rate`, sans en-tête. `language` n'est pas transmis.

        L'appel attend une place du sémaphore, puis un refus 429 est réessayé jusqu'à `RETRY_MAX_S` secondes avant de
        lever `EngineOverloaded`.
        """
        body = {"model": MODEL, "input": text, "voice": voice, "response_format": "pcm"}
        async with self._slots:
            deadline = time.monotonic() + RETRY_MAX_S
            while True:
                try:
                    response = await self._request("POST", "/v1/audio/speech", timeout=SPEECH_TIMEOUT, json=body)
                    return response.content
                except EngineOverloaded:
                    if time.monotonic() + RETRY_INTERVAL_S > deadline:
                        raise
                await asyncio.sleep(RETRY_INTERVAL_S)

    async def voices(self) -> list[str]:
        """Voix de la synthèse, lues dans `voices` de `GET /v1/audio/voices` (les alias ne sont pas des voix)."""
        response = await self._request("GET", "/v1/audio/voices", timeout=VOICES_TIMEOUT)
        try:
            voices = response.json()["voices"]
            if not isinstance(voices, list) or not all(isinstance(voice, str) for voice in voices):
                raise TypeError
            return voices
        except (ValueError, KeyError, TypeError):
            raise EngineError("The voice server answered with an unreadable list of voices") from None

    async def ready(self) -> bool:
        """Vrai si `GET /health` répond 200 ; une autre réponse ou une panne rend faux."""
        try:
            await self._request("GET", "/health", timeout=READY_TIMEOUT)
        except (EngineUnavailable, EngineOverloaded, EngineError):
            return False
        return True
