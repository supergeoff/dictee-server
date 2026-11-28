"""Client du serveur CrispASR : transcription d'un morceau d'audio par HTTP et disponibilité du serveur.

Rien n'est journalisé ici : ni le texte transcrit, ni l'audio. Les messages des exceptions ne reprennent jamais le
corps d'une réponse, qui peut citer le texte transcrit.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import httpx

from dictee.engines.ports import EngineError, EngineOverloaded, EngineUnavailable
from dictee.modes import is_auto_language, to_iso_language
from dictee.types import Final, Partial, Word

# Délais en secondes. Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
OPEN_TIMEOUT = 10.0  # ouverture d'une connexion HTTP
# Lecture de la réponse d'un morceau. CrispASR met en file les requêtes simultanées au lieu de les refuser : l'attente
# dans la file compte dans ce délai, qui reprend celui de NeMo (600 s) pour la même raison. Une requête abandonnée
# avant sa réponse serait renvoyée alors que le serveur la traite encore.
CHUNK_TIMEOUT = 600.0
READY_TIMEOUT = 5.0


def _word(word: dict) -> Word:
    """Un mot de la réponse `verbose_json` : son texte et ses instants, sans personne."""
    return Word(text=word["word"], start=float(word["start"]), end=float(word["end"]))


class CrispasrTranscriber:
    """Les appels de dictee au serveur CrispASR ; `http` est le client HTTP partagé de l'app.

    Il joue le rôle `FileTranscriber`. Il déclare qu'il marque les personnes (`speakers`) et suit la progression d'un
    fichier (`progress`) : le serveur le fait par `diarize=true` et `GET /progress`, ce que la transcription d'un
    fichier entier mettra en œuvre. Pour l'instant, seuls `transcribe_chunk` et `ready` sont écrits.
    """

    speakers = True
    progress = True

    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http

    async def _request(self, method: str, path: str, *, timeout: float, **kwargs) -> httpx.Response:
        """Appel d'une route HTTP de CrispASR.

        Un échec de transport ou un 503 (modèle en chargement) donne `EngineUnavailable`, un 429 `EngineOverloaded`
        (le serveur met en file au lieu de refuser : ce code est une précaution). Toute autre erreur donne
        `EngineError`, dont le message ne porte que le code HTTP.
        """
        try:
            response = await self._http.request(
                method,
                self._base_url + path,
                headers=self._headers,
                timeout=httpx.Timeout(timeout, connect=OPEN_TIMEOUT),
                **kwargs,
            )
        except httpx.TransportError as exc:
            raise EngineUnavailable(f"CrispASR is unreachable: {exc!r}") from exc
        if response.status_code == 503:
            raise EngineUnavailable("CrispASR is not ready (HTTP 503)")
        if response.status_code == 429:
            raise EngineOverloaded("CrispASR's request queue is full")
        if response.is_error:
            raise EngineError(f"CrispASR answered HTTP {response.status_code}")
        return response

    def transcribe(
        self,
        wav: Path,
        *,
        language: str,
        phrases: Sequence[str],
        boost: float,
        speakers: bool = False,
        partials: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> AsyncIterator[Final | Partial]:
        """Pas encore écrite : la transcription d'un fichier entier est l'objet d'une tâche suivante."""
        raise NotImplementedError("CrispASR file transcription is not written yet")

    async def transcribe_chunk(self, wav: bytes, *, language: str, phrases: Sequence[str], boost: float) -> Final:
        """Transcrit un morceau de WAV par `POST /v1/audio/transcriptions` ; rend son `Final`.

        La langue part en ISO-639-1 (`fr-FR` donne `fr`) ; `auto` part tel quel et laisse CrispASR détecter la langue.
        `phrases` et `boost` sont ignorés : l'adaptateur n'envoie jamais de hotwords. Le texte du `Final` est celui de
        la réponse sans espaces autour, et ses mots sont ceux de tous les segments, dans l'ordre, sans personne.

        Lève les erreurs de `_request`, et `EngineError` pour une réponse 200 qui n'est pas une transcription lisible.
        """
        data = {
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
            "language": "auto" if is_auto_language(language) else to_iso_language(language),
        }
        response = await self._request(
            "POST",
            "/v1/audio/transcriptions",
            timeout=CHUNK_TIMEOUT,
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
        )
        try:
            body = response.json()
            return Final(
                text=body["text"].strip(),
                words=tuple(_word(word) for segment in body["segments"] for word in segment.get("words") or ()),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise EngineError("CrispASR answered with an unreadable transcription") from None

    async def ready(self) -> bool:
        """Vrai seulement si `GET /health` répond 200 avec `status` à `ok` ; 503 (chargement) ou une panne rend faux."""
        try:
            response = await self._request("GET", "/health", timeout=READY_TIMEOUT)
            body = response.json()
        except (EngineUnavailable, EngineOverloaded, EngineError, ValueError):
            return False
        return isinstance(body, dict) and body.get("status") == "ok"
