"""Dictée en direct : `/v1/realtime?intent=transcription`, au format du Realtime d'OpenAI en mode transcription.

dictee relaie l'audio du client vers une session temps réel de NeMo et traduit les événements dans les deux sens
(section 6.5 de la spec). La dictée en direct ne rend que le texte brut, sans reformulation ni personnes ; elle n'est
pas enregistrée et ne passe pas par la file des tâches longues.

Le journal ne reçoit qu'une ligne par connexion, sans texte, sans audio et sans glossaire. Les erreurs envoyées au
client portent des messages fixes : celui d'une exception de NeMo n'est jamais repris, car il peut citer le glossaire.
"""

import asyncio
import base64
import binascii
import contextlib
import itertools
import json
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field, replace
from typing import NoReturn

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from dictee.audio import SAMPLE_WIDTH
from dictee.auth import resolve_caller
from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure, unexpected_error
from dictee.modes import DEFAULT_LANGUAGE, MODELS, to_iso_language, to_nemo_language
from dictee.nemo import LiveSession
from dictee.profile import glossary_terms, load_profile
from dictee.types import Cleared, Committed, Final, Partial

logger = logging.getLogger("dictee")

MAX_AUDIO_S = 7200.0  # audio reçu au plus par connexion, en secondes
IDLE_TIMEOUT_S = 1800.0  # attente au plus d'un événement du client, en secondes
DEFAULT_RATE = 24000
MIN_RATE = 8000
MAX_RATE = 96000
DEFAULT_SILENCE_MS = 500  # silence qui ferme une prise de parole avec `server_vad`, quand le client n'en donne pas

# Mesures de la tâche 1 sur NeMo 0.3.0. Une session NeMo accepte plusieurs commits et rend un `completed` puis un
# `committed` pour chacun : une seule session sert toute la connexion. Avec `endpointing_ms` à 500, aucun `completed`
# n'arrive avant le commit, même après 2 s de silence : NeMo ne ferme pas seul une prise de parole, et `server_vad`
# est refusé.
NEMO_MANY_COMMITS = True
SERVER_VAD = False

_MODEL = "dictee"  # le modèle du texte brut, que la session annonce quel que soit le modèle demandé
_INPUT = "session.audio.input"
_DELTA = "conversation.item.input_audio_transcription.delta"
_COMPLETED = "conversation.item.input_audio_transcription.completed"
_LINE = "route=realtime user=%s audio_s=%.1f turns=%d status=%s"


@dataclass(frozen=True)
class _Config:
    """Réglages de la session en vigueur : ceux que la prochaine session NeMo reçoit."""

    rate: int = DEFAULT_RATE
    language: str = DEFAULT_LANGUAGE  # code ISO-639-1
    keywords: tuple[str, ...] = ()
    silence_ms: int | None = None  # silence de `server_vad` ; `None` quand le client valide lui-même


@dataclass
class _Item:
    """Une prise de parole : son identifiant, la durée de son audio, son commit et ses textes finals.

    NeMo peut rendre plusieurs textes finals pour une même prise de parole : le client n'en reçoit qu'un seul
    `completed`, qui les joint, une fois la prise de parole finie.
    """

    id: str
    seconds: float = 0.0
    committed: bool = False
    texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _End:
    """Fin d'une connexion : statut de sa ligne de journal, erreur à envoyer, code de fermeture.

    `event_id` est celui de l'événement du client qui a causé l'erreur.
    """

    status: str
    error: ApiError | None = None
    close_code: int | None = None
    event_id: str | None = None


def _invalid(param: str, message: str) -> ApiError:
    return ApiError(400, message, code="invalid_value", param=param)


def _object(container: dict, key: str, param: str) -> dict | None:
    """L'objet `container[key]`, `None` s'il manque ou vaut `null` ; toute autre valeur est refusée."""
    value = container.get(key)
    if value is None or isinstance(value, dict):
        return value
    raise _invalid(param, f"'{param}' must be an object.")


def _read_rate(audio_format: dict) -> int:
    if audio_format.get("type") != "audio/pcm":
        raise _invalid(f"{_INPUT}.format.type", "Only audio/pcm is supported: 16-bit little-endian mono PCM.")
    rate = audio_format.get("rate", DEFAULT_RATE)
    if type(rate) is not int or not MIN_RATE <= rate <= MAX_RATE:
        raise _invalid(f"{_INPUT}.format.rate", f"The rate must be an integer from {MIN_RATE} to {MAX_RATE}.")
    return rate


def _read_language(code: object, param: str) -> str:
    """Code ISO-639-1 d'une langue demandée ; une langue absente vaut le français."""
    if code is None or isinstance(code, str):
        with contextlib.suppress(ApiError):
            return to_iso_language(code)
    raise _invalid(param, "Unsupported language. Send an ISO-639-1 code such as 'fr' or 'en'.")


def _read_transcription(transcription: dict, config: _Config) -> _Config:
    """Langue (`languages[0]`, sinon `language`) et `keywords` ; `prompt`, `delay` et `model` sont ignorés."""
    if transcription.get("languages") is not None:
        param = f"{_INPUT}.transcription.languages"
        languages = transcription["languages"]
        if not isinstance(languages, list) or not languages:
            raise _invalid(param, "'languages' must be a non-empty list of language codes.")
        config = replace(config, language=_read_language(languages[0], param))
    elif "language" in transcription:
        config = replace(config, language=_read_language(transcription["language"], f"{_INPUT}.transcription.language"))
    if "keywords" in transcription:
        keywords = transcription["keywords"] or []
        if not isinstance(keywords, list) or not all(isinstance(keyword, str) for keyword in keywords):
            raise _invalid(f"{_INPUT}.transcription.keywords", "'keywords' must be a list of strings.")
        config = replace(config, keywords=tuple(dict.fromkeys(word.strip() for word in keywords if word.strip())))
    return config


def _read_turn_detection(turn_detection: object) -> int | None:
    """Silence de `server_vad`, ou `None` quand le client valide chaque prise de parole par un commit.

    `semantic_vad` est toujours refusé, et `server_vad` tant que NeMo ne ferme pas seul une prise de parole.
    """
    if turn_detection is None:
        return None
    param = f"{_INPUT}.turn_detection"
    kind = turn_detection.get("type") if isinstance(turn_detection, dict) else None
    if kind != "server_vad" or not SERVER_VAD:
        accepted = "null or server_vad" if SERVER_VAD else "null, with a commit for each turn"
        raise ApiError(
            400, f"Unsupported turn_detection: use {accepted}.", code="unsupported_turn_detection", param=param
        )
    silence = turn_detection.get("silence_duration_ms")
    if silence is None:
        return DEFAULT_SILENCE_MS
    if type(silence) is not int or silence <= 0:
        raise _invalid(f"{param}.silence_duration_ms", "'silence_duration_ms' must be a positive integer.")
    return silence


def _read_session(session: object, current: _Config) -> _Config:
    """Réglages que donne un `session.update`, à partir de ceux en vigueur : un champ absent garde sa valeur.

    Lève une `ApiError` dont `param` désigne le champ refusé ; les réglages en vigueur ne changent alors pas.
    """
    if not isinstance(session, dict):
        raise _invalid("session", "'session' must be an object.")
    if session.get("type", "transcription") != "transcription":
        raise _invalid("session.type", "Only transcription sessions are supported.")
    audio_input = _object(_object(session, "audio", "session.audio") or {}, "input", _INPUT) or {}
    config = current
    if (audio_format := _object(audio_input, "format", f"{_INPUT}.format")) is not None:
        config = replace(config, rate=_read_rate(audio_format))
    if (transcription := _object(audio_input, "transcription", f"{_INPUT}.transcription")) is not None:
        config = _read_transcription(transcription, config)
    if "turn_detection" in audio_input:
        config = replace(config, silence_ms=_read_turn_detection(audio_input["turn_detection"]))
    return config


def _client_event(message: dict) -> dict | None:
    """L'événement d'un message du client : un objet JSON dans un message texte, `None` pour tout autre message."""
    text = message.get("text")
    if text is None:
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _failure(error: Exception) -> _End:
    """Fin de la connexion sur une exception : client parti, panne de NeMo, ou panne imprévue dont seule la classe
    est journalisée."""
    if isinstance(error, WebSocketDisconnect):
        return _End("closed")
    if isinstance(error, NEMO_ERRORS):
        failure = nemo_failure(error)
        return _End(failure.code, failure, 1011)
    failure = unexpected_error(error)
    return _End(failure.type, failure, 1011)


def _outcome(task: asyncio.Task) -> _End:
    """Fin de la connexion d'après la tâche qui s'est arrêtée la première."""
    try:
        return task.result()
    except Exception as error:
        return _failure(error)


class _LiveConnection:
    """Une connexion de dictée en direct, de l'identification de l'appelant à sa ligne de journal.

    Deux tâches se partagent l'état de la connexion, sur la même boucle : la lecture du client et celle de NeMo.
    Les prises de parole dont NeMo n'a pas fini les textes attendent dans `_items`, dans l'ordre : NeMo traite
    l'audio dans l'ordre reçu, donc chacun de ses événements concerne la plus ancienne.
    """

    def __init__(self, websocket: WebSocket) -> None:
        state = websocket.app.state
        self._ws = websocket
        self._db, self._settings, self._nemo = state.db, state.settings, state.nemo
        self._id = f"sess_{secrets.token_hex(12)}"
        self._config = _Config()
        self._terms: list[str] = []
        self._event_numbers = itertools.count(1)
        self._item_numbers = itertools.count(1)
        self._send_lock = asyncio.Lock()
        # Les sessions NeMo ouvertes, de la plus ancienne à la plus récente, et la file où la lecture de NeMo les prend
        # dans le même ordre.
        self._stacks: list[contextlib.AsyncExitStack] = []
        self._opened: asyncio.Queue[LiveSession] = asyncio.Queue()
        self._sending: LiveSession | None = None  # la session qui reçoit l'audio de la prise de parole en cours
        self._items: deque[_Item] = deque()
        self._current: _Item | None = None  # la prise de parole qui reçoit l'audio
        self._last_committed: str | None = None
        self._user = "-"
        self._audio_s = 0.0
        self._turns = 0
        self._status = "closed"

    async def run(self) -> None:
        """Sert la connexion, puis ferme les sessions NeMo et écrit sa ligne, quelle que soit la façon dont elle finit.

        Le ménage ne se laisse pas annuler : la fermeture d'une session NeMo attend au plus `CLOSE_TIMEOUT`.
        """
        try:
            await self._finish(await self._serve())
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    while self._stacks:
                        await self._stacks.pop().aclose()
            finally:
                logger.info(_LINE, self._user, self._audio_s, self._turns, self._status)

    async def _serve(self) -> _End:
        """Accepte la connexion, puis lit le client et NeMo jusqu'à ce que l'une des deux lectures s'arrête."""
        try:
            refused = await self._accept()
        except Exception as error:
            return _failure(error)
        if refused is not None:
            return refused
        client = asyncio.create_task(self._read_client())
        nemo = asyncio.create_task(self._read_nemo())
        try:
            done, _ = await asyncio.wait((client, nemo), return_when=asyncio.FIRST_COMPLETED)
        finally:
            client.cancel()
            nemo.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(client, nemo, return_exceptions=True)
        return _outcome(client if client in done else nemo)

    async def _accept(self) -> _End | None:
        """Identifie l'appelant, accepte la connexion et envoie `session.created` ; rend la fin d'un refus."""
        headers = self._ws.headers
        try:
            caller = resolve_caller(
                self._db, self._settings, headers.get("authorization"), headers.get("x-openwebui-user-email")
            )
        except ApiError as error:
            await self._ws.accept()
            return _End(error.code or "invalid_api_key", error, 1008)
        await self._ws.accept()
        self._user = "default" if caller.user_id is None else str(caller.user_id)
        query = self._ws.query_params
        if query.get("intent") != "transcription":
            message = "This route serves transcription sessions only: connect with intent=transcription."
            return _End("unsupported_intent", ApiError(400, message, code="unsupported_intent"), 1008)
        model = query.get("model")
        if model is not None and model not in MODELS and "transcribe" not in model:
            message = "This route serves transcription models only."
            return _End("unsupported_model", ApiError(400, message, code="unsupported_model", param="model"), 1008)
        self._terms = glossary_terms(load_profile(self._db, self._settings, caller).glossary)
        await self._send("session.created", session=self._session())
        return None

    async def _finish(self, end: _End) -> None:
        """Envoie l'erreur de fin et ferme la connexion avec son code, au mieux.

        Un client déjà parti ne reçoit rien. Starlette refuse aussi par une `RuntimeError` un envoi sur une connexion
        pas encore acceptée, quand la panne précède l'acceptation : la fermeture part alors seule.
        """
        self._status = end.status
        if end.error is not None:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self._send_error(end.error, end.event_id)
        if end.close_code is not None:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self._ws.close(end.close_code)

    def _session(self) -> dict:
        config = self._config
        turn_detection = (
            None if config.silence_ms is None else {"type": "server_vad", "silence_duration_ms": config.silence_ms}
        )
        return {
            "id": self._id,
            "object": "realtime.transcription_session",
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": config.rate},
                    "transcription": {"model": _MODEL, "language": config.language},
                    "turn_detection": turn_detection,
                }
            },
        }

    async def _send(self, kind: str, **fields: object) -> None:
        async with self._send_lock:
            await self._ws.send_json({"type": kind, "event_id": f"event_{next(self._event_numbers)}", **fields})

    async def _send_error(self, error: ApiError, event_id: str | None) -> None:
        body = {"type": error.type, "code": error.code, "message": error.message, "param": error.param}
        await self._send("error", error={**body, "event_id": event_id})

    # --- Lecture du client ---

    async def _read_client(self) -> _End:
        """Lit les événements du client jusqu'à son départ, `IDLE_TIMEOUT_S` sans événement ou la limite d'audio."""
        while True:
            try:
                async with asyncio.timeout(IDLE_TIMEOUT_S):
                    message = await self._ws.receive()
            except TimeoutError:
                text = f"The session ended after {IDLE_TIMEOUT_S:g} seconds without any client event."
                return _End("idle", ApiError(400, text, code="idle_timeout"), 1000)
            if message["type"] == "websocket.disconnect":
                return _End("closed")
            event = _client_event(message)
            if event is None:
                text = "Each client event must be a JSON object sent as a text message."
                await self._send_error(ApiError(400, text, code="invalid_json"), None)
            elif (end := await self._handle(event)) is not None:
                return end

    async def _handle(self, event: dict) -> _End | None:
        """Traite un événement du client ; une erreur de sa part lui revient en `error`, et la session continue."""
        event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else None
        kind = event.get("type")
        try:
            if kind == "session.update":
                await self._update(event.get("session"))
            elif kind == "input_audio_buffer.append":
                return await self._append(event.get("audio"), event_id)
            elif kind == "input_audio_buffer.commit":
                await self._commit()
            elif kind == "input_audio_buffer.clear":
                await self._clear()
            else:
                raise _invalid("type", "Unsupported event type for a transcription session.")
        except ApiError as error:
            await self._send_error(error, event_id)
        return None

    async def _update(self, session: object) -> None:
        """Change les réglages en vigueur, tant qu'aucune session NeMo ne reçoit l'audio d'une prise de parole.

        NeMo refuse un `session.update` une fois l'audio commencé : ses réglages se fixent à l'ouverture.
        """
        if self._sending is not None:
            message = "The session settings cannot change once its audio has started."
            raise ApiError(400, message, code="session_already_started", param="session")
        self._config = _read_session(session, self._config)
        await self._send("session.updated", session=self._session())

    async def _append(self, audio: object, event_id: str | None) -> _End | None:
        """Transmet l'audio décodé à NeMo, en ouvrant la session et la prise de parole au besoin ; rend la fin à la
        limite de `MAX_AUDIO_S` secondes d'audio reçu."""
        try:
            pcm = base64.b64decode(audio, validate=True) if isinstance(audio, str) else None
        except binascii.Error:
            pcm = None
        if pcm is None:
            raise _invalid("audio", "'audio' must be base64-encoded 16-bit PCM.")
        # Un nombre impair d'octets décalerait d'un octet tous les échantillons suivants de la session : le morceau est
        # refusé avant de compter son audio ou d'ouvrir une session NeMo.
        if len(pcm) % SAMPLE_WIDTH:
            raise _invalid("audio", "'audio' must hold whole 16-bit samples, so an even number of bytes.")
        seconds = len(pcm) / (SAMPLE_WIDTH * self._config.rate)
        self._audio_s += seconds
        if self._audio_s > MAX_AUDIO_S:
            message = f"The session reached its limit of {MAX_AUDIO_S:g} seconds of audio."
            return _End("limit", ApiError(400, message, code="audio_too_long"), 1000, event_id)
        if not pcm:
            return None
        if self._sending is None:
            self._sending = await self._open_session()
        if self._current is None:
            self._current = _Item(f"item_{next(self._item_numbers)}")
            self._items.append(self._current)
        self._current.seconds += seconds
        await self._sending.send_audio(pcm)
        return None

    async def _open_session(self) -> LiveSession:
        """Ouvre une session NeMo avec les réglages en vigueur et la confie à la lecture de NeMo.

        NeMo reçoit le glossaire de l'appelant, puis les `keywords` qui n'y sont pas déjà.
        """
        config = self._config
        stack = contextlib.AsyncExitStack()
        session = await stack.enter_async_context(
            self._nemo.live(
                sample_rate=config.rate,
                language=to_nemo_language(config.language),
                phrases=[*self._terms, *(word for word in config.keywords if word not in self._terms)],
                boost=self._settings.glossary_boost,
                endpointing_ms=config.silence_ms,
            )
        )
        self._stacks.append(stack)
        self._opened.put_nowait(session)
        return session

    async def _commit(self) -> None:
        item, session = self._current, self._sending
        if item is None or session is None:
            message = "The audio buffer is empty: append audio before committing."
            raise ApiError(400, message, code="input_audio_buffer_commit_empty")
        self._current = None
        if not NEMO_MANY_COMMITS:
            self._sending = None  # la prise de parole suivante ouvre une autre session
        await self._announce(item)
        await session.commit()

    async def _clear(self) -> None:
        """Vide la prise de parole en cours ; NeMo confirme par `Cleared`, que la lecture de NeMo relaie.

        Sans audio en cours, rien n'est à vider : la confirmation part aussitôt.
        """
        item, session = self._current, self._sending
        if item is None or session is None:
            await self._send("input_audio_buffer.cleared")
            return
        self._current = None
        self._items.remove(item)
        await session.clear()

    # --- Lecture de NeMo ---

    async def _read_nemo(self) -> NoReturn:
        """Relaie les événements des sessions NeMo, une session après l'autre, dans l'ordre de leur ouverture.

        Quand une session n'accepte qu'un commit (`NEMO_MANY_COMMITS` faux), elle se ferme après son `Committed`.
        """
        while True:
            session = await self._opened.get()
            async with contextlib.aclosing(session.events()) as events:
                async for event in events:
                    await self._relay(event)
                    if isinstance(event, Committed) and not NEMO_MANY_COMMITS:
                        break
            await self._stacks.pop(0).aclose()

    async def _relay(self, event: Partial | Final | Committed | Cleared) -> None:
        """Traduit un événement de NeMo pour le client, sur la plus ancienne prise de parole qui l'attend."""
        item = self._items[0] if self._items else None
        if isinstance(event, Cleared):
            await self._send("input_audio_buffer.cleared")
        elif item is None:
            return  # un reste d'une prise de parole vidée : personne ne l'attend
        elif isinstance(event, Partial):
            await self._send(_DELTA, item_id=item.id, content_index=0, delta=event.text)
        elif isinstance(event, Final):
            item.texts.append(event.text)
            if not item.committed:
                # NeMo a fermé seul la prise de parole après le silence de `server_vad` : il n'enverra pas de
                # `Committed` pour elle, et l'audio suivant ouvre une autre prise de parole.
                self._items.popleft()
                if self._current is item:
                    self._current = None
                await self._announce(item)
                await self._complete(item)
        elif isinstance(event, Committed) and item.committed:
            self._items.popleft()
            await self._complete(item)  # NeMo a rendu tous les textes finals de la prise de parole validée

    async def _announce(self, item: _Item) -> None:
        """`input_audio_buffer.committed` d'une prise de parole, liée à celle validée avant elle."""
        item.committed = True
        previous, self._last_committed = self._last_committed, item.id
        self._turns += 1
        await self._send("input_audio_buffer.committed", item_id=item.id, previous_item_id=previous)

    async def _complete(self, item: _Item) -> None:
        """Le texte final d'une prise de parole, ses textes finals non vides joints par une espace, et la durée de son
        audio ; une seule fois par prise de parole, qui a déjà quitté `_items`."""
        transcript = " ".join(text for text in item.texts if text)
        usage = {"type": "duration", "seconds": round(item.seconds, 3)}
        await self._send(_COMPLETED, item_id=item.id, content_index=0, transcript=transcript, usage=usage)


async def realtime(websocket: WebSocket) -> None:
    """Route `/v1/realtime` : une connexion de dictée en direct, de bout en bout (voir `_LiveConnection`)."""
    await _LiveConnection(websocket).run()
