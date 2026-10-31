"""Route `POST /v1/audio/transcriptions`, compatible avec celle d'OpenAI.

La route lit l'envoi, convertit l'audio, fait transcrire par NeMo (avec les locuteurs ou la reformulation selon le
modèle demandé) et répond dans le format demandé, en une fois ou en événements SSE. Rien n'est journalisé ici qui
vienne du contenu : ni le texte transcrit, ni les consignes, ni l'audio. La ligne de chaque requête dit qui, quoi,
combien de temps et comment elle s'est terminée.
"""

import asyncio
import contextlib
import json
import logging
import re
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.datastructures import State
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse

from dictee.audio import AudioDecodeError, AudioTooLong, to_wav16k, wav_duration
from dictee.auth import require_caller
from dictee.cleanup import clean
from dictee.errors import ApiError, envelope, unexpected_error
from dictee.formats import delta_event, done_event, render, segment_event
from dictee.modes import Mode, resolve_mode, to_iso_language, to_nemo_language
from dictee.nemo import NemoError, NemoOverloaded, NemoUnavailable
from dictee.profile import Profile, glossary_terms, load_profile
from dictee.speakers import assign_speakers, build_segments
from dictee.types import Transcript, Turn, Word

logger = logging.getLogger("dictee")

MIB = 1024 * 1024
# Plafond de durée d'un envoi, toutes transcriptions confondues.
MAX_AUDIO_SECONDS = 7200.0
# Les routes REST de NeMo refusent plus de 3600 s : la passe de locuteurs s'arrête un peu avant.
MAX_DIARIZE_SECONDS = 3590.0
# Un champ du formulaire (la consigne `prompt`, par exemple) reste en mémoire : sa taille et leur nombre sont bornés.
MAX_FIELD_BYTES = MIB
MAX_PARTS = 100
GRANULARITIES = frozenset({"word", "segment"})
# Statut que la ligne de journal donne à une requête dont le client a raccroché (celui de nginx).
CLIENT_CLOSED = 499

_NEMO_ERRORS = (NemoOverloaded, NemoUnavailable, NemoError)
# Les tâches lancées par `_spawn`, gardées ici : asyncio ne retient pas ses tâches.
_running: set[asyncio.Task] = set()


class LongAudioGate:
    """Une seule transcription longue à la fois, pour ne pas cumuler leur mémoire dans NeMo.

    `slot(durée)` est un gestionnaire de contexte asynchrone. Un audio plus long que `threshold_s` attend la place
    unique et la garde jusqu'à la sortie du bloc, y compris quand la tâche est annulée. Un audio de `threshold_s` ou
    moins passe sans attendre, même quand la place est prise.
    """

    def __init__(self, threshold_s: float) -> None:
        self.threshold_s = threshold_s
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def slot(self, duration_s: float) -> AsyncIterator[None]:
        if duration_s <= self.threshold_s:
            yield
            return
        async with self._lock:
            yield


@dataclass
class _Trace:
    """Ce que la ligne de journal d'une requête dit d'elle ; un champ resté inconnu s'écrit `-`.

    Le statut d'une requête qui s'arrête sur une erreur imprévue reste 500.
    """

    user: str = "-"
    model: str = "-"
    mode: str = "-"
    audio_s: float | None = None
    nemo_ms: int | None = None
    llm_ms: int | None = None
    decision: str = "-"
    status: int = 500

    def log(self) -> None:
        def show(value: float | int | None, template: str = "{}") -> str:
            return "-" if value is None else template.format(value)

        logger.info(
            "user=%s route=transcriptions model=%s mode=%s audio_s=%s nemo_ms=%s llm_ms=%s decision=%s status=%d",
            self.user,
            self.model,
            self.mode,
            show(self.audio_s, "{:.1f}"),
            show(self.nemo_ms),
            show(self.llm_ms),
            self.decision,
            self.status,
        )


def _loggable(model: str | None) -> str:
    """Nom de modèle sûr pour une ligne de journal : un client ne peut pas y forger une ligne d'après la sienne."""
    return re.sub(r"[^\w.:/-]", "?", model or "")[:64] or "-"


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


@dataclass(frozen=True)
class _Params:
    """Les champs du formulaire qui pilotent la transcription, une fois validés."""

    nemo_language: str
    language: str
    response_format: str
    mode: Mode
    stream: bool
    granularities: frozenset[str]
    prompt: str | None


def _first(fields: dict[str, list[str]], name: str) -> str | None:
    values = fields.get(name)
    return values[0] if values else None


def _stream_flag(value: str | None) -> bool:
    match (value or "false").lower():
        case "true":
            return True
        case "false":
            return False
    raise ApiError(400, "stream must be 'true' or 'false'.", param="stream")


def _parse_params(fields: dict[str, list[str]], trace: _Trace) -> _Params:
    """Lit les champs du formulaire, et lève une erreur 400 pour une langue, un format ou un `stream` refusés.

    Un champ absent prend sa valeur par défaut : la fonction vaut aussi pour les champs reçus avant le fichier.
    `chunking_strategy`, `temperature`, `known_speaker_names[]`, `known_speaker_references[]` et `include[]` sont
    acceptés et ignorés.
    """
    model = _first(fields, "model")
    trace.model = _loggable(model)
    language = _first(fields, "language")
    nemo_language = to_nemo_language(language)
    response_format = _first(fields, "response_format") or "json"
    mode = resolve_mode(model or "", response_format)
    trace.mode = mode.value
    # Comme chez OpenAI, `verbose_json` rend les segments quand aucune granularité n'est demandée.
    values = [*fields.get("timestamp_granularities[]", ()), *fields.get("timestamp_granularities", ())]
    return _Params(
        nemo_language=nemo_language,
        language=to_iso_language(language),
        response_format=response_format,
        mode=mode,
        stream=_stream_flag(_first(fields, "stream")),
        granularities=frozenset(values) & GRANULARITIES or frozenset({"segment"}),
        prompt=_first(fields, "prompt") or None,
    )


def _too_large(limit: int) -> ApiError:
    message = f"The request body is larger than the limit of {limit // MIB} MB."
    return ApiError(413, message, param="file", code="payload_too_large")


class _MultipartReader:
    """Lit un corps multipart bloc par bloc : les champs restent en mémoire, la partie `file` va sur disque.

    Les autres parties qui portent un fichier sont acceptées et ignorées. `on_file` reçoit les champs déjà lus quand
    la partie `file` commence, avant que son premier octet soit écrit.
    """

    def __init__(self, boundary: bytes, path: Path, on_file: Callable[[dict[str, list[str]]], None]) -> None:
        self.fields: dict[str, list[str]] = {}
        self.has_file = False
        self._path = path
        self._on_file = on_file
        self._parts = 0
        self._name_chunks = b""
        self._value_chunks = b""
        self._disposition = b""
        self._field_name = ""
        self._value = bytearray()
        self._file: BinaryIO | None = None
        self._ignored = False
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._part_begin,
                "on_header_field": self._header_field,
                "on_header_value": self._header_value,
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._part_data,
                "on_part_end": self._part_end,
            },
        )

    def write(self, chunk: bytes) -> None:
        self._parser.write(chunk)

    def finish(self) -> None:
        self._parser.finalize()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_PARTS:
            raise ApiError(400, "The form has too many parts.")
        self._disposition = b""
        self._value.clear()
        self._ignored = False

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._name_chunks += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value_chunks += data[start:end]

    def _header_end(self) -> None:
        if self._name_chunks.lower() == b"content-disposition":
            self._disposition = self._value_chunks
        self._name_chunks = self._value_chunks = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        self._field_name = options.get(b"name", b"").decode("utf-8", "replace")
        if b"filename" not in options:
            return
        if self._field_name == "file" and not self.has_file:
            self._on_file(self.fields)
            self._file = self._path.open("wb")
            self.has_file = True
        else:
            self._ignored = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._file is not None:
            self._file.write(data[start:end])
        elif not self._ignored:
            self._value += data[start:end]
            if len(self._value) > MAX_FIELD_BYTES:
                raise ApiError(400, "A form field is too large.")

    def _part_end(self) -> None:
        if self._file is not None:
            self.close()
        elif not self._ignored:
            self.fields.setdefault(self._field_name, []).append(self._value.decode("utf-8", "replace"))


async def _read_upload(
    request: Request, path: Path, limit: int, on_file: Callable[[dict[str, list[str]]], None]
) -> tuple[dict[str, list[str]], bool]:
    """Lit le formulaire de la requête et écrit son fichier à `path` ; rend les champs et si un fichier est arrivé.

    Le corps ne dépasse pas `limit` octets : au-delà, la lecture s'arrête sur une erreur 413 sans avoir tout reçu.
    """
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if content_type.lower() != b"multipart/form-data" or not boundary:
        raise ApiError(400, "Send the request as multipart/form-data.")
    reader = _MultipartReader(boundary, path, on_file)
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise _too_large(limit)
            reader.write(chunk)
        reader.finish()
    except FormParserError:
        raise ApiError(400, "The multipart body is malformed.") from None
    finally:
        reader.close()
    return reader.fields, reader.has_file


async def _decode(path: Path) -> bytearray:
    """Convertit l'envoi en WAV 16 kHz ; un audio trop long ou illisible devient une erreur 400 ou 422."""
    try:
        return await to_wav16k(path, max_seconds=MAX_AUDIO_SECONDS)
    except AudioTooLong:
        raise ApiError(400, "The audio is longer than 2 hours.", param="file", code="audio_too_long") from None
    except AudioDecodeError:
        raise ApiError(422, "The audio file could not be read.", param="file", code="invalid_audio") from None


def _nemo_failure(error: Exception) -> ApiError:
    """Erreur de l'API pour un échec de NeMo ; le message de NeMo n'est pas repris."""
    if isinstance(error, NemoOverloaded):
        message = "The speech engine is busy, try again shortly."
        return ApiError(429, message, type="rate_limit_error", code="overloaded")
    return ApiError(502, "The speech engine did not answer correctly.", type="server_error", code="upstream_error")


def _spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Lance `coro` dans sa propre tâche, hors de la portée d'annulation de la requête.

    Starlette annule une réponse en continu avec une portée d'annulation d'anyio, qui renouvelle l'annulation à
    chaque attente : le nettoyage d'un générateur annulé de cette façon est interrompu à sa première attente, et la
    session de NeMo resterait ouverte. Une tâche annulée par `Task.cancel` n'est annulée qu'une fois : son nettoyage
    va jusqu'au bout, dans sa propre tâche.
    """
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_forget)
    return task


def _forget(task: asyncio.Task) -> None:
    _running.discard(task)
    if not task.cancelled():
        task.exception()  # lue ici, pour qu'asyncio n'en journalise pas le message si personne ne la lit


async def _until_disconnect(receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _run_unless_disconnected(request: Request, coro: Coroutine[Any, Any, Any]) -> Any | None:
    """Rend le résultat de `coro`, ou `None` si le client raccroche avant : `coro` est alors annulée.

    Starlette ne prévient pas une route d'un client qui raccroche : sans cette garde, la transcription irait à son
    terme et garderait sa place pour rien.
    """
    job = _spawn(coro)
    watcher = asyncio.create_task(_until_disconnect(request.receive))
    try:
        await asyncio.wait({job, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        job.cancel()  # sans effet quand elle est terminée
        watcher.cancel()
        await asyncio.gather(job, watcher, return_exceptions=True)
    return None if job.cancelled() else job.result()


@dataclass
class _Recognition:
    """Ce que NeMo a rendu : les textes des phrases finales, leurs mots, les tours de parole."""

    texts: list[str]
    words: list[Word]
    turns: list[Turn]


@dataclass
class _Job:
    """Une transcription en cours : l'audio converti et ce que ses étapes partagent."""

    state: State
    params: _Params
    profile: Profile
    trace: _Trace
    wav: bytearray
    duration: float

    async def recognize(self, on_text: Callable[[str], None] | None = None) -> _Recognition:
        """Les passes de NeMo sur l'audio, sous la place des transcriptions longues.

        `on_text` reçoit le texte de chaque phrase finale dès qu'elle arrive, précédé d'une espace à partir de la
        seconde. La session de NeMo se ferme, et la place se libère, quelle que soit la sortie, annulation comprise.
        """
        state, params = self.state, self.params
        texts: list[str] = []
        words: list[Word] = []
        turns: list[Turn] = []
        async with state.gate.slot(self.duration):
            started = time.monotonic()
            try:
                async with contextlib.aclosing(
                    state.nemo.transcribe(
                        self.wav,
                        language=params.nemo_language,
                        phrases=glossary_terms(self.profile.glossary),
                        boost=state.settings.glossary_boost,
                        prompt=params.prompt,
                    )
                ) as finals:
                    async for final in finals:
                        words.extend(final.words)
                        text = final.text.strip()
                        if text:
                            texts.append(text)
                            if on_text is not None:
                                on_text(text if len(texts) == 1 else f" {text}")
                if params.mode is Mode.DIARIZE and words:
                    turns = await state.nemo.diarize(self.wav)
            finally:
                self.trace.nemo_ms = _elapsed_ms(started)
        return _Recognition(texts, words, turns)

    async def finish(self, recognition: _Recognition) -> Transcript:
        """Le texte entier, avec ses locuteurs ou reformulé selon le mode."""
        params = self.params
        text = " ".join(recognition.texts)
        words = tuple(recognition.words)
        segments = None
        if params.mode is Mode.DIARIZE:
            words = tuple(assign_speakers(words, recognition.turns))
            segments = tuple(build_segments(words))
        elif params.mode is Mode.CLEAN:
            started = time.monotonic()
            result = await clean(text, profile=self.profile, settings=self.state.settings, http=self.state.http)
            self.trace.llm_ms = _elapsed_ms(started)
            self.trace.decision = result.decision
            text = result.text
        return Transcript(text, words, self.duration, params.language, segments)

    async def events(self) -> AsyncIterator[str]:
        """Les événements SSE de la réponse en continu ; la ligne de journal s'écrit à leur fin.

        Une erreur arrive quand la réponse a déjà commencé : elle part comme un événement `error`, et le flux se ferme.
        """
        deltas: asyncio.Queue[str | None] = asyncio.Queue()
        # Avec la reformulation, le texte part en un seul delta, quand le LLM l'a rendu.
        on_text = None if self.params.mode is Mode.CLEAN else deltas.put_nowait
        recognition = _spawn(self.recognize(on_text))
        recognition.add_done_callback(lambda _: deltas.put_nowait(None))
        try:
            while (delta := await deltas.get()) is not None:
                yield delta_event(delta)
            transcript = await self.finish(recognition.result())
            if self.params.mode is Mode.CLEAN and transcript.text:
                yield delta_event(transcript.text)
            for segment in transcript.segments or ():
                yield segment_event(segment)
            yield done_event(transcript.text)
            self.trace.status = 200
        except (asyncio.CancelledError, GeneratorExit):
            self.trace.status = CLIENT_CLOSED
            raise
        except _NEMO_ERRORS as error:
            yield self._fail(_nemo_failure(error))
        except Exception as error:
            yield self._fail(unexpected_error(error))
        finally:
            recognition.cancel()
            self.trace.log()

    def _fail(self, failure: ApiError) -> str:
        self.trace.status = failure.status
        return _error_event(failure)


def _error_event(failure: ApiError) -> str:
    error = envelope(failure.message, type=failure.type, param=failure.param, code=failure.code)["error"]
    return "data: " + json.dumps({"type": "error", "error": error}, ensure_ascii=False) + "\n\n"


async def _handle(request: Request, trace: _Trace) -> Response:
    state = request.app.state
    settings = state.settings
    caller = require_caller(request)
    trace.user = "default" if caller.user_id is None else str(caller.user_id)
    profile = load_profile(state.db, settings, caller)

    limit = settings.max_upload_mb * MIB
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = 0
    if declared > limit:
        raise _too_large(limit)

    # L'envoi vit seul dans son dossier, supprimé dès la conversion faite, quelle que soit l'issue.
    with tempfile.TemporaryDirectory(prefix="dictee-") as directory:
        path = Path(directory) / "upload"
        # Une langue ou un format refusés le sont dès le début du fichier, avant d'en lire le moindre octet.
        fields, has_file = await _read_upload(request, path, limit, lambda early: _parse_params(early, trace))
        params = _parse_params(fields, trace)
        if not has_file:
            raise ApiError(400, "Missing required parameter: 'file'.", param="file", code="missing_required_parameter")
        wav = await _decode(path)

    duration = wav_duration(wav)
    trace.audio_s = duration
    if params.mode is Mode.DIARIZE and duration > MAX_DIARIZE_SECONDS:
        raise ApiError(400, "Speaker separation accepts audio up to 59 minutes.", param="file", code="audio_too_long")
    job = _Job(state, params, profile, trace, wav, duration)

    if params.stream:
        return StreamingResponse(job.events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    try:
        recognition = await _run_unless_disconnected(request, job.recognize())
        if recognition is None:
            return Response(status_code=CLIENT_CLOSED)
        transcript = await job.finish(recognition)
    except _NEMO_ERRORS as error:
        raise _nemo_failure(error) from None
    return render(transcript, params.response_format, granularities=params.granularities)


async def transcriptions(request: Request) -> Response:
    """Transcrit l'audio envoyé ; chaque requête écrit une ligne dans le journal `dictee`."""
    trace = _Trace()
    streaming = False
    try:
        response = await _handle(request, trace)
        trace.status = response.status_code
        streaming = isinstance(response, StreamingResponse)
        return response
    except ApiError as error:
        trace.status = error.status
        raise
    except ClientDisconnect:
        trace.status = CLIENT_CLOSED
        return Response(status_code=CLIENT_CLOSED)
    finally:
        # Une réponse en continu écrit sa ligne à la fin du flux.
        if not streaming:
            trace.log()
