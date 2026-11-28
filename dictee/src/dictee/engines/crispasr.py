"""Client du serveur CrispASR : transcription d'un fichier ou d'un morceau d'audio par HTTP et disponibilité du serveur.

Rien n'est journalisé ici : ni le texte transcrit, ni l'audio. Les messages des exceptions ne reprennent jamais le
corps d'une réponse, qui peut citer le texte transcrit.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import httpx

from dictee.audio import SAMPLE_WIDTH, TRANSCRIPTION_RATE, WAV_HEADER_SIZE, read_wav_slice
from dictee.cuts import chunk_bounds, detect_silences, plan_cuts
from dictee.engines.ports import EngineError, EngineOverloaded, EngineUnavailable
from dictee.modes import is_auto_language, to_iso_language
from dictee.types import Final, Partial, Word

# Délais en secondes. Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
OPEN_TIMEOUT = 10.0  # ouverture d'une connexion HTTP
# Lecture de la réponse d'un morceau. CrispASR met en file les requêtes simultanées au lieu de les refuser : l'attente
# dans la file compte dans ce délai, qui reprend celui de NeMo (600 s) pour la même raison. Une requête abandonnée
# avant sa réponse serait renvoyée alors que le serveur la traite encore.
CHUNK_TIMEOUT = 600.0
# Lecture de la réponse d'un fichier entier avec locuteurs, en secondes de délai par seconde d'audio : le serveur ne
# renvoie rien avant la fin de la transcription. Le serveur visé transcrit en un quart du temps de l'audio (67 s de
# calcul pour 300 s d'audio) : un facteur de 1 laisse une marge de quatre, file d'attente comprise.
WHOLE_TIMEOUT_FACTOR = 1.0
READY_TIMEOUT = 5.0
# Avancement d'un fichier avec locuteurs. Le serveur n'en annonce aucun pour ce modèle (`GET /progress` vaut 0 ou 100) :
# il est estimé d'après le temps écoulé, toutes les `PROGRESS_EVERY_S` secondes, à raison de `WHOLE_SPEED_FACTOR`
# secondes de calcul par seconde d'audio, et il s'arrête à `WHOLE_PROGRESS_CAP` du fichier tant que la réponse manque.
# shortcut: le facteur est celui du serveur visé (0,23 mesuré sur un enregistrement de 35 minutes) ; sur une autre
# machine la barre finit trop tôt ou attend au plafond, et il faudra alors en faire un réglage.
PROGRESS_EVERY_S = 5.0
WHOLE_SPEED_FACTOR = 0.25
WHOLE_PROGRESS_CAP = 0.95
# Un morceau d'un fichier sans locuteurs dure 15 s au plus : la route émet un `transcript.text.delta` par morceau fini.
STREAM_CHUNK_S = 15.0


def whole_timeout(duration: float) -> float:
    """Le délai de lecture, en secondes, de la requête d'un fichier entier de `duration` secondes d'audio."""
    return max(CHUNK_TIMEOUT, duration * WHOLE_TIMEOUT_FACTOR)


def estimated_seconds(elapsed: float, duration: float) -> float:
    """Les secondes d'audio qu'un fichier avec locuteurs de `duration` secondes a dû traiter en `elapsed` secondes."""
    return min(elapsed / WHOLE_SPEED_FACTOR, duration * WHOLE_PROGRESS_CAP)


async def _estimate_progress(on_progress: Callable[[float], None], duration: float) -> None:
    """Donne à `on_progress`, toutes les `PROGRESS_EVERY_S` secondes, l'avancement estimé d'après le temps écoulé."""
    started = time.monotonic()
    while True:
        await asyncio.sleep(PROGRESS_EVERY_S)
        on_progress(estimated_seconds(time.monotonic() - started, duration))


def _duration(wav: Path) -> float:
    """La durée, en secondes, d'un WAV PCM16 mono 16 kHz, lue dans la taille du fichier."""
    return (wav.stat().st_size - WAV_HEADER_SIZE) / SAMPLE_WIDTH / TRANSCRIPTION_RATE


def _word(word: dict, speaker: object = None) -> Word:
    """Un mot de la réponse `verbose_json` : son texte, ses instants et la personne de son segment (`None` sans)."""
    return Word(
        text=word["word"],
        start=float(word["start"]),
        end=float(word["end"]),
        speaker=None if speaker is None else str(speaker),
    )


def _final(response: httpx.Response, *, with_speakers: bool = False) -> Final:
    """Le `Final` d'une réponse `verbose_json` ; `with_speakers` donne à chaque mot la personne de son segment.

    Le texte est celui de la réponse sans espaces autour, et les mots sont ceux de tous les segments, dans l'ordre.
    Lève `EngineError` pour une réponse 200 qui n'est pas une transcription lisible.
    """
    try:
        body = response.json()
        return Final(
            text=body["text"].strip(),
            words=tuple(
                _word(word, segment.get("speaker") if with_speakers else None)
                for segment in body["segments"]
                for word in segment.get("words") or ()
            ),
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        raise EngineError("CrispASR answered with an unreadable transcription") from None


class CrispasrTranscriber:
    """Les appels de dictee au serveur CrispASR ; `http` est le client HTTP partagé de l'app.

    Il joue le rôle `FileTranscriber`. Il déclare qu'il marque les personnes (`speakers`) : le serveur le fait par
    `diarize=true`, avec la méthode `diar_method` (le réglage `DIAR_METHOD`), ce que `transcribe` met en œuvre pour un
    fichier entier.
    """

    speakers = True

    def __init__(
        self, base_url: str, api_key: str, http: httpx.AsyncClient, *, diar_method: str = "sortformer"
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http
        self._diar_method = diar_method

    async def _request(
        self, method: str, path: str, *, timeout: float, authenticated: bool = True, **kwargs
    ) -> httpx.Response:
        """Appel d'une route HTTP de CrispASR ; `authenticated=False` n'envoie pas la clé (route publique).

        Un échec de transport ou un 503 (modèle en chargement) donne `EngineUnavailable`, un 429 `EngineOverloaded`
        (le serveur met en file au lieu de refuser : ce code est une précaution). Toute autre erreur donne
        `EngineError`, dont le message ne porte que le code HTTP.
        """
        try:
            response = await self._http.request(
                method,
                self._base_url + path,
                headers=self._headers if authenticated else None,
                timeout=httpx.Timeout(timeout, connect=OPEN_TIMEOUT),
                **kwargs,
            )
        except httpx.TransportError as exc:
            raise EngineUnavailable("CrispASR is unreachable") from exc
        if response.status_code == 503:
            raise EngineUnavailable("CrispASR is not ready (HTTP 503)")
        if response.status_code == 429:
            raise EngineOverloaded("CrispASR's request queue is full")
        if response.is_error:
            raise EngineError(f"CrispASR answered HTTP {response.status_code}")
        return response

    async def transcribe(
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
        """Transcrit un fichier WAV entier ; aucun `Partial` ne sort, même avec `partials` : CrispASR n'en rend pas.

        `on_progress` reçoit, comme le veut le rôle, les secondes d'audio déjà traitées. Avec `speakers`, le fichier
        part en une requête (`diarize=true`, `diarize_method` du réglage), lu depuis le disque au fil de l'envoi, avec
        un délai de lecture qui suit sa durée (`whole_timeout`) ; il sort en un seul `Final`, dont chaque mot porte la
        personne de son segment, telle que le serveur la nomme. Pendant la requête, `on_progress` reçoit toutes les
        `PROGRESS_EVERY_S` secondes une estimation tirée du temps écoulé (`estimated_seconds`), parce que le serveur
        n'annonce pas d'avancement pour ce modèle. Sans `speakers`, le fichier est coupé aux silences en morceaux de
        `STREAM_CHUNK_S` au plus : chaque morceau fini sort en un `Final` aux instants du fichier, puis `on_progress`
        reçoit la fin de ce morceau. Fermer le générateur avant la fin arrête la requête en cours et l'estimation.
        """
        if speakers:
            yield await self._transcribe_whole(wav, language, on_progress)
        else:
            async with contextlib.aclosing(
                self._transcribe_chunks(wav, language, phrases, boost, on_progress)
            ) as finals:
                async for final in finals:
                    yield final

    async def _transcribe_whole(self, wav: Path, language: str, on_progress: Callable[[float], None] | None) -> Final:
        """Le fichier entier en une requête avec les personnes, avec l'avancement estimé pendant l'attente.

        httpx lit le fichier ouvert par blocs pendant l'envoi : l'audio d'une réunion ne passe pas en mémoire d'un bloc.
        """
        duration = _duration(wav)
        extra = {"diarize": "true", "diarize_method": self._diar_method}
        with wav.open("rb") as audio:
            request = asyncio.create_task(self._post_audio(audio, language, extra, timeout=whole_timeout(duration)))
            reader = None if on_progress is None else asyncio.create_task(_estimate_progress(on_progress, duration))
            tasks = [request] if reader is None else [request, reader]
            try:
                # L'estimation ne finit jamais d'elle-même : si elle se termine, `on_progress` a levé.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if reader is not None and reader.done():
                    reader.result()
                response = await request
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return _final(response, with_speakers=True)

    async def _transcribe_chunks(
        self,
        wav: Path,
        language: str,
        phrases: Sequence[str],
        boost: float,
        on_progress: Callable[[float], None] | None,
    ) -> AsyncIterator[Final]:
        """Les morceaux du fichier, coupés aux silences, un `Final` par morceau fini."""
        duration = _duration(wav)
        cuts: list[float] = []
        if duration > STREAM_CHUNK_S:
            cuts = plan_cuts(duration, await detect_silences(wav, duration), STREAM_CHUNK_S)
        for start, end in chunk_bounds(duration, cuts):
            final = await self.transcribe_chunk(
                read_wav_slice(wav, start, end), language=language, phrases=phrases, boost=boost
            )
            shifted = tuple(replace(word, start=word.start + start, end=word.end + start) for word in final.words)
            yield replace(final, words=shifted)
            if on_progress is not None:
                on_progress(end)

    async def _post_audio(
        self,
        wav: bytes | BinaryIO,
        language: str,
        extra: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> httpx.Response:
        """`POST /v1/audio/transcriptions` d'un WAV, en octets ou en fichier ouvert, avec les champs de `extra` en plus.

        La langue part en ISO-639-1 (`fr-FR` donne `fr`) ; `auto` part tel quel et laisse CrispASR détecter la langue.
        Le délai de lecture vaut `timeout`, ou `CHUNK_TIMEOUT` sans lui.
        """
        data = {
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
            "language": "auto" if is_auto_language(language) else to_iso_language(language),
            **(extra or {}),
        }
        return await self._request(
            "POST",
            "/v1/audio/transcriptions",
            timeout=CHUNK_TIMEOUT if timeout is None else timeout,
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
        )

    async def transcribe_chunk(self, wav: bytes, *, language: str, phrases: Sequence[str], boost: float) -> Final:
        """Transcrit un morceau de WAV par `POST /v1/audio/transcriptions` ; rend son `Final`.

        `phrases` et `boost` sont ignorés : l'adaptateur n'envoie jamais de hotwords. Le texte du `Final` est celui de
        la réponse sans espaces autour, et ses mots sont ceux de tous les segments, dans l'ordre, sans personne.

        Lève les erreurs de `_request`, et `EngineError` pour une réponse 200 qui n'est pas une transcription lisible.
        """
        return _final(await self._post_audio(wav, language))

    async def ready(self) -> bool:
        """Vrai seulement si `GET /health` répond 200 avec `status` à `ok` ; 503 (chargement) ou une panne rend faux."""
        try:
            response = await self._request("GET", "/health", timeout=READY_TIMEOUT, authenticated=False)
            body = response.json()
        except (EngineUnavailable, EngineOverloaded, EngineError, ValueError):
            return False
        return isinstance(body, dict) and body.get("status") == "ok"
