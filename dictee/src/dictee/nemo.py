"""Client de NeMo-Speech.cpp : transcription en continu ou par morceaux, synthèse, liste des voix et disponibilité.

Rien n'est journalisé ici : ni le texte transcrit, ni le texte à dire, ni l'audio. Les messages des exceptions ne
reprennent que ce que NeMo dit de son propre fonctionnement. La bibliothèque `websockets` journalise elle aussi, mais
elle est réduite au silence en DEBUG (voir `_WEBSOCKETS_LOGGER`).
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from dictee.audio import SAMPLE_WIDTH, TRANSCRIPTION_RATE, pcm_frames
from dictee.types import Cleared, Committed, Final, Partial, Word

# Fréquence native de la synthèse de NeMo (mesurée en tâche 1) : `speak` rend du PCM16 mono à cette fréquence.
TTS_SAMPLE_RATE = 22050

# Une trame binaire du temps réel pèse une seconde de PCM16 mono à 16 kHz.
FRAME_BYTES = TRANSCRIPTION_RATE * SAMPLE_WIDTH

# Délais en secondes. Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
OPEN_TIMEOUT = 10.0  # ouverture d'une connexion, WebSocket ou HTTP
EVENT_TIMEOUT = 300.0  # silence maximal entre deux événements de NeMo pendant une session temps réel
# Silence maximal entre deux événements quand la session marque les personnes : une réunion de 2 heures peut rester
# bien plus longtemps sans événement qu'une dictée. La tâche 1 a mesuré au plus 23 s de silence sur une réunion de
# 2 heures, commit et événement final compris : le délai retenu reste le plancher de 300 s.
SPEAKERS_EVENT_TIMEOUT = 300.0
# Clé de la personne dans chaque mot de l'événement final : un entier, numéroté à partir de 1 (relevé de la tâche 1).
SPEAKER_KEY = "speaker"
# Attente de la réponse de NeMo à la fermeture d'une session. Une session abandonnée pendant l'envoi de l'audio n'y
# répond pas : NeMo ne lit la fermeture qu'après l'audio déjà envoyé (10,02 s d'attente avec le délai par défaut
# de `websockets`, mesuré sur 184 s d'audio), et la place de la transcription longue reste prise pendant ce temps.
CLOSE_TIMEOUT = 1.0
SPEECH_TIMEOUT = 120.0
MODELS_TIMEOUT = 10.0
# Lecture de la réponse d'un morceau. Un morceau de 30 s prend 7,5 à 8,5 s seul chez NeMo, mais NeMo met en file les
# requêtes simultanées : 16 requêtes envoyées ensemble finissent en 127,7 s (mesuré en tâche 1). Le délai doit couvrir
# cette attente dans la file : une requête abandonnée avant sa réponse serait renvoyée alors que NeMo la traite
# encore, et la file s'allongerait de doublons.
CHUNK_TIMEOUT = 600.0
READY_TIMEOUT = 5.0

_DELTA = "conversation.item.input_audio_transcription.delta"
_COMPLETED = "conversation.item.input_audio_transcription.completed"
_COMMITTED = "input_audio_buffer.committed"
_CLEARED = "input_audio_buffer.cleared"

# À son niveau DEBUG, `websockets` écrit chaque en-tête de la poignée de main (dont la clé d'API) et un extrait de
# chaque trame (dont la session, qui porte le glossaire de l'appelant). Il lit ce niveau à la création de la
# connexion : un logger dédié, au niveau INFO fixé, reste muet en DEBUG quel que soit le réglage du logger racine.
_WEBSOCKETS_LOGGER = logging.getLogger("dictee.nemo.websockets")
_WEBSOCKETS_LOGGER.setLevel(logging.INFO)


class NemoUnavailable(Exception):
    """NeMo ne répond pas : connexion refusée ou coupée, délai dépassé."""


class NemoOverloaded(Exception):
    """La file de requêtes de NeMo est pleine (HTTP 429 ou événement `resource_exhausted`)."""


class NemoError(Exception):
    """NeMo a refusé la requête, signalé une erreur ou cessé d'émettre pendant une session."""


def _event_error(event: dict) -> Exception:
    """Exception d'un événement `error` : `resource_exhausted` est une surcharge, tout autre code une erreur."""
    error = event.get("error") or {}
    code = error.get("code")
    message = error.get("message") or "no error message"
    if code == "resource_exhausted":
        return NemoOverloaded(message)
    return NemoError(f"{code}: {message}")


async def _receive(ws: ClientConnection, sender: asyncio.Task) -> str | bytes:
    """Prochain message de NeMo, ou l'erreur de `sender` quand l'envoi de l'audio échoue avant.

    Une connexion coupée n'est pas une erreur de l'envoi : la lecture la relève elle-même. Sans cette garde, un envoi
    qui échoue (fichier illisible, rappel de progression en erreur) laisserait la session attendre un événement qui
    ne viendra jamais, jusqu'au délai d'attente.
    """
    receiving = asyncio.ensure_future(ws.recv())
    try:
        await asyncio.wait({receiving, sender}, return_when=asyncio.FIRST_COMPLETED)
        if sender.done() and not sender.cancelled():
            error = sender.exception()
            if error is not None and not isinstance(error, ConnectionClosed):
                raise error
        return await receiving
    finally:
        if not receiving.cancel() and not receiving.cancelled():
            receiving.exception()  # lue ici, pour qu'asyncio n'en journalise rien si personne ne la lit


async def _next_event(ws: ClientConnection, timeout: float | None = None, sender: asyncio.Task | None = None) -> dict:
    """Prochain événement de NeMo, attendu `timeout` secondes au plus (`EVENT_TIMEOUT` par défaut).

    Un événement `error` lève l'exception qui lui correspond ; avec `sender`, l'erreur de l'envoi de l'audio aussi.
    """
    timeout = EVENT_TIMEOUT if timeout is None else timeout
    try:
        async with asyncio.timeout(timeout):
            raw = await (ws.recv() if sender is None else _receive(ws, sender))
    except TimeoutError:
        raise NemoError(f"no event from NeMo for {timeout:g} seconds") from None
    return _decode(raw)


def _decode(raw: str | bytes) -> dict:
    """Événement lu dans un message de NeMo ; un événement `error` lève l'exception qui lui correspond."""
    event = json.loads(raw)
    if event.get("type") == "error":
        raise _event_error(event)
    return event


async def _expect(ws: ClientConnection, kind: str) -> None:
    event = await _next_event(ws)
    if event.get("type") != kind:
        raise NemoError(f"expected the event {kind}, NeMo sent {event.get('type')}")


async def _send_audio(ws: ClientConnection, wav: Path, on_progress: Callable[[float], None] | None) -> None:
    """Envoie le WAV par trames d'une seconde lues dans le fichier, puis le commit.

    NeMo lit les trames à la vitesse de son traitement : `send` ne rend la main qu'à ce rythme, d'où une tâche à
    part qui laisse la lecture des événements avancer pendant l'envoi. Une seule trame est en mémoire à la fois.
    `on_progress` reçoit, après chaque trame, les secondes d'audio déjà envoyées.
    """
    sent = 0
    with contextlib.closing(pcm_frames(wav, FRAME_BYTES)) as frames:
        for frame in frames:
            await ws.send(frame)
            sent += len(frame)
            if on_progress is not None:
                on_progress(sent / FRAME_BYTES)
    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))


def _word(word: dict) -> Word:
    """Un mot de l'événement final ; sa personne est celle de `SPEAKER_KEY` en texte, `None` sans elle."""
    speaker = word.get(SPEAKER_KEY)
    return Word(
        text=word["word"],
        start=float(word["start"]),
        end=float(word["end"]),
        speaker=None if speaker is None else str(speaker),
    )


def _final(event: dict) -> Final:
    return Final(text=event["transcript"], words=tuple(_word(word) for word in event.get("words") or ()))


class LiveSession:
    """Session temps réel de NeMo ouverte par `NemoClient.live` pour la dictée en direct.

    `send_audio` envoie le PCM16 tel quel en une trame binaire, à la fréquence déclarée à l'ouverture ; `commit`
    valide la prise de parole en cours et `clear` en vide l'audio. `events` rend ce que NeMo répond, dans l'ordre
    d'arrivée : un `Partial` par delta non vide, un `Final` par texte final (sans mots, que la session ne demande
    pas), un `Committed` quand NeMo a fini une prise de parole validée et un `Cleared` quand il en a vidé l'audio.

    L'attente des événements n'a pas de délai : pendant une dictée, NeMo se tait tant que le client n'envoie rien, et
    c'est la connexion du client qui borne la session. Lève les mêmes exceptions que `NemoClient.transcribe` :
    `NemoUnavailable` quand NeMo coupe la connexion, `NemoOverloaded` et `NemoError` pour ses événements `error`.
    """

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    async def _send(self, message: str | bytes) -> None:
        try:
            await self._ws.send(message)
        except ConnectionClosed as exc:
            raise NemoUnavailable(f"NeMo closed the realtime connection: {exc}") from exc

    async def send_audio(self, pcm: bytes) -> None:
        await self._send(pcm)

    async def commit(self) -> None:
        await self._send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def clear(self) -> None:
        await self._send(json.dumps({"type": "input_audio_buffer.clear"}))

    async def events(self) -> AsyncIterator[Partial | Final | Committed | Cleared]:
        while True:
            try:
                event = _decode(await self._ws.recv())
            except ConnectionClosed as exc:
                raise NemoUnavailable(f"NeMo closed the realtime connection: {exc}") from exc
            kind = event.get("type")
            if kind == _DELTA:
                if text := event.get("delta"):
                    yield Partial(text)
            elif kind == _COMPLETED:
                transcript = event.get("transcript")
                if not isinstance(transcript, str):
                    raise NemoError("NeMo sent a final event without its transcript")
                yield Final(transcript, ())
            elif kind == _COMMITTED:
                yield Committed()
            elif kind == _CLEARED:
                yield Cleared()


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return "no error message"


class NemoClient:
    """Les appels de dictee à NeMo ; `http` est le client HTTP partagé de l'app."""

    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http

    async def _open(self) -> ClientConnection:
        """Ouvre la session WebSocket de transcription en continu.

        Pas de ping de maintien : NeMo ne répond à un ping qu'après l'audio envoyé avant lui (113 s sur dix minutes
        d'audio, mesuré en tâche 1), donc le ping par défaut de `websockets` couperait une session longue.
        """
        url = "ws" + self._base_url.removeprefix("http") + "/v1/audio/transcriptions/realtime"
        try:
            return await connect(
                url,
                additional_headers=self._headers,
                max_size=None,
                ping_interval=None,
                open_timeout=OPEN_TIMEOUT,
                close_timeout=CLOSE_TIMEOUT,
                logger=_WEBSOCKETS_LOGGER,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 429:
                raise NemoOverloaded("NeMo refused the realtime connection: its request queue is full") from exc
            raise NemoError(f"NeMo refused the realtime connection with HTTP {status}") from exc
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            raise NemoUnavailable(f"cannot open the realtime connection to NeMo: {exc!r}") from exc

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
        """Transcrit le WAV PCM16 mono 16 kHz du fichier `wav` par une session temps réel ; rend un `Final` par phrase.

        Le fichier est lu par trames d'une seconde (voir `_send_audio`) : sa durée ne pèse pas sur la mémoire.
        `on_progress`, s'il est donné, reçoit les secondes d'audio déjà envoyées après chaque trame.

        Avec `speakers=True`, la session demande à NeMo de marquer les personnes (`speaker_diarization`) : chaque
        `Word.speaker` du `Final` vaut alors la personne que NeMo donne, en texte, `None` quand il n'en donne pas, et
        l'attente entre deux événements va jusqu'à `SPEAKERS_EVENT_TIMEOUT` secondes. Le champ `prompt` de NeMo n'est
        jamais transmis : il renforce une phrase à une force qui insère des mots que personne n'a dits.

        Avec `partials=True`, chaque événement `delta` de NeMo non vide rend aussi un `Partial`, dans l'ordre d'arrivée
        et avant le `Final` de son item : NeMo en émet un tous les 1,12 s d'audio traité, puis un seul `completed` au
        commit. Sans `partials`, seuls les `Final` sortent.

        La session finit sur le premier `input_audio_buffer.committed` reçu après le commit : NeMo laisse alors le
        socket ouvert et c'est le client qui le ferme. La session se ferme aussi quand la tâche qui attend un
        événement est annulée. Un consommateur qui s'arrête avant la fin ferme le générateur
        (`contextlib.aclosing`) : c'est à ce moment que la session se ferme.

        Lève `NemoUnavailable` si NeMo est injoignable ou coupe la connexion, `NemoOverloaded` si sa file est pleine
        et `NemoError` pour toute autre erreur, y compris plus de `EVENT_TIMEOUT` secondes sans événement
        (`SPEAKERS_EVENT_TIMEOUT` avec les personnes).
        """
        session: dict[str, object] = {"sample_rate": TRANSCRIPTION_RATE, "language": language, "word_timestamps": True}
        if speakers:
            session["speaker_diarization"] = True
        if phrases:
            session["speech_contexts"] = [{"phrases": list(phrases), "boost": boost}]
        event_timeout = SPEAKERS_EVENT_TIMEOUT if speakers else EVENT_TIMEOUT

        ws = await self._open()
        sender: asyncio.Task | None = None
        try:
            await _expect(ws, "session.created")
            await ws.send(json.dumps({"type": "session.update", "session": session}))
            await _expect(ws, "session.updated")
            sender = asyncio.create_task(_send_audio(ws, wav, on_progress))
            while True:
                event = await _next_event(ws, event_timeout, sender)
                kind = event.get("type")
                if kind == _COMPLETED:
                    yield _final(event)
                elif kind == _DELTA:
                    if partials and (text := event.get("delta")):
                        yield Partial(text)
                elif kind == _COMMITTED:
                    return
        except ConnectionClosed as exc:
            raise NemoUnavailable(f"NeMo closed the realtime connection: {exc}") from exc
        finally:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            await ws.close()

    @contextlib.asynccontextmanager
    async def live(
        self,
        *,
        sample_rate: int,
        language: str,
        phrases: Sequence[str],
        boost: float,
        endpointing_ms: int | None = None,
    ) -> AsyncIterator[LiveSession]:
        """Ouvre une session temps réel pour la dictée en direct et la ferme à la sortie du bloc `async with`.

        La session reçoit `sample_rate`, la fréquence de l'audio que le client envoie (NeMo accepte de 8000 à
        96000 Hz), `language`, `speech_contexts` s'il y a des termes, et `endpointing_ms` s'il est donné : NeMo
        ferme alors seul une phrase après ce silence. Ni `word_timestamps` ni `prompt` ne partent. NeMo refuse un
        `session.update` une fois l'audio commencé : les réglages se fixent ici, une fois pour toutes.

        L'ouverture lève les erreurs de `transcribe` ; la session, celles que décrit `LiveSession`.
        """
        session: dict[str, object] = {"sample_rate": sample_rate, "language": language}
        if phrases:
            session["speech_contexts"] = [{"phrases": list(phrases), "boost": boost}]
        if endpointing_ms is not None:
            session["endpointing_ms"] = endpointing_ms
        ws = await self._open()
        try:
            await _expect(ws, "session.created")
            await ws.send(json.dumps({"type": "session.update", "session": session}))
            await _expect(ws, "session.updated")
            yield LiveSession(ws)
        except ConnectionClosed as exc:
            raise NemoUnavailable(f"NeMo closed the realtime connection: {exc}") from exc
        finally:
            await ws.close()

    async def _request(self, method: str, path: str, *, timeout: float, **kwargs) -> httpx.Response:
        """Appel d'une route HTTP de NeMo : 429 donne `NemoOverloaded`, un échec de transport `NemoUnavailable`."""
        try:
            response = await self._http.request(
                method,
                self._base_url + path,
                headers=self._headers,
                timeout=httpx.Timeout(timeout, connect=OPEN_TIMEOUT),
                **kwargs,
            )
        except httpx.TransportError as exc:
            raise NemoUnavailable(f"NeMo is unreachable: {exc!r}") from exc
        if response.status_code == 429:
            raise NemoOverloaded("NeMo's request queue is full")
        if response.is_error:
            raise NemoError(f"NeMo answered HTTP {response.status_code}: {_error_message(response)}")
        return response

    async def speak(self, text: str, *, voice: str, language: str) -> bytes:
        """Synthèse vocale : du PCM16 mono à `TTS_SAMPLE_RATE`, sans en-tête."""
        response = await self._request(
            "POST",
            "/v1/audio/speech",
            timeout=SPEECH_TIMEOUT,
            json={"input": text, "voice": voice, "language": language, "response_format": "pcm"},
        )
        return response.content

    async def voices(self) -> list[str]:
        """Voix de la synthèse, lues dans l'entrée `speech` de `/v1/models`."""
        response = await self._request("GET", "/v1/models", timeout=MODELS_TIMEOUT)
        for model in response.json()["data"]:
            if model.get("capability") == "speech":
                return list(model["voices"])
        raise NemoError("NeMo lists no speech model")

    async def transcribe_chunk(self, wav: bytes, *, language: str, phrases: Sequence[str], boost: float) -> Final:
        """Transcrit un morceau de WAV par la route REST `POST /v1/audio/transcriptions` ; rend son `Final`.

        Le corps multipart est construit en mémoire : un morceau ne dépasse pas 40 s, soit 1,3 Mo. Le texte du
        `Final` est celui de la réponse sans espaces autour, ses mots portent leurs instants et aucune personne. Un
        morceau muet donne un `Final` au texte vide. Comme pour la session temps réel, le champ `prompt` de NeMo n'est
        jamais transmis, et `speech_contexts` ne part que s'il y a des termes.

        Lève les erreurs de `_request` (`NemoUnavailable`, `NemoOverloaded`, `NemoError`), et `NemoError` pour une
        réponse 200 qui n'est pas une transcription lisible. L'attente de la réponse va jusqu'à `CHUNK_TIMEOUT`
        secondes, file de NeMo comprise.
        """
        data = {"language": language, "response_format": "verbose_json", "timestamp_granularities[]": "word"}
        if phrases:
            data["speech_contexts"] = json.dumps([{"phrases": list(phrases), "boost": boost}])
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
                words=tuple(_word(word) for word in body.get("words") or ()),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise NemoError("NeMo answered with an unreadable transcription") from None

    async def ready(self) -> bool:
        """Vrai seulement si `GET /ready` répond 200 avec `ready` à `true` ; toute autre réponse ou panne rend faux.

        Sert à attendre que NeMo redevienne disponible avant de renvoyer un morceau qu'il n'a pas traité.
        """
        try:
            response = await self._request("GET", "/ready", timeout=READY_TIMEOUT)
            body = response.json()
        except (NemoUnavailable, NemoOverloaded, NemoError, ValueError):
            return False
        return response.status_code == 200 and isinstance(body, dict) and body.get("ready") is True
