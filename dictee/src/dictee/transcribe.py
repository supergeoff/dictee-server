"""Route `POST /v1/audio/transcriptions`, compatible avec celle d'OpenAI.

La route lit l'envoi, convertit l'audio, fait transcrire par NeMo (avec les locuteurs ou la reformulation selon le
modèle demandé) et répond dans le format demandé, en une fois ou en événements SSE. Rien n'est journalisé ici qui
vienne du contenu : ni le texte transcrit, ni les consignes, ni l'audio. La ligne de chaque requête dit qui, quoi,
combien de temps et comment elle s'est terminée.

Les pièces que d'autres routes réutilisent sont dans `upload` (lecture de l'envoi), `lifecycle` (ligne de journal,
tâches détachées, client qui raccroche), `errors` (erreurs de NeMo) et `formats` (événements SSE).
"""

import asyncio
import contextlib
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse

from dictee.audio import AudioDecodeError, AudioTooLong, to_wav16k, wav_duration
from dictee.auth import require_caller
from dictee.cleanup import clean
from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure, unexpected_error
from dictee.formats import delta_event, done_event, error_event, render, segment_event
from dictee.lifecycle import CLIENT_CLOSED, Trace, cancel_once, elapsed_ms, loggable, run_unless_disconnected, spawn
from dictee.modes import Mode, resolve_mode, to_iso_language, to_nemo_language
from dictee.profile import Profile, glossary_terms, load_profile
from dictee.speakers import assign_speakers, build_segments
from dictee.types import Transcript, Turn, Word
from dictee.upload import read_upload

# Plafond de durée d'un envoi, toutes transcriptions confondues. Dictee garde le WAV décodé en mémoire (173 Mo pour
# 5400 s, sous sa limite de 512 Mo) : une heure et demie est ce que la place unique des audios longs peut porter.
MAX_AUDIO_SECONDS = 5400.0
# Les routes REST de NeMo refusent plus de 3600 s : la passe de locuteurs s'arrête un peu avant.
MAX_DIARIZE_SECONDS = 3590.0
GRANULARITIES = frozenset({"word", "segment"})


def long_audio_busy() -> ApiError:
    """Erreur 429 d'un audio long refusé parce que la place et la file d'attente des audios longs sont prises."""
    message = "Another long transcription is already waiting, try again later."
    return ApiError(429, message, type="rate_limit_error", code="long_audio_busy")


class LongAudioGate:
    """Une transcription longue à la fois et une seule en attente, pour borner la mémoire de dictee et de NeMo.

    Un audio plus long que `threshold_s` est « long » ; un audio de `threshold_s` ou moins est « court » et passe
    sans attendre, même quand les places des longs sont prises. `slot(durée)` est un gestionnaire de contexte
    asynchrone : un audio long prend la place unique, ou attend derrière celui qui la tient, et la garde jusqu'à la
    sortie du bloc, y compris quand la tâche est annulée. Un audio long qui arrive quand l'un tourne et l'autre
    attend est refusé tout de suite par `long_audio_busy` : l'attente est bornée à une seule requête. Un audio long
    qui attend et dont la tâche est annulée libère sa place d'attente.
    """

    def __init__(self, threshold_s: float) -> None:
        self.threshold_s = threshold_s
        self._lock = asyncio.Lock()
        # Les audios longs admis : celui qui tourne et celui qui attend. Compté sans attente entre le test et
        # l'incrément, donc sans course entre deux requêtes.
        self._long = 0

    @property
    def full(self) -> bool:
        """Vrai quand un audio long de plus serait refusé."""
        return self._long >= 2

    @contextlib.asynccontextmanager
    async def slot(self, duration_s: float) -> AsyncIterator[None]:
        if duration_s <= self.threshold_s:
            yield
            return
        if self.full:
            raise long_audio_busy()
        self._long += 1
        try:
            async with self._lock:
                yield
        finally:
            self._long -= 1


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


def _parse_params(fields: dict[str, list[str]], trace: Trace) -> _Params:
    """Lit les champs du formulaire, et lève une erreur 400 pour une langue, un format ou un `stream` refusés.

    Un champ absent prend sa valeur par défaut : la fonction vaut aussi pour les champs reçus avant le fichier.
    `chunking_strategy`, `temperature`, `known_speaker_names[]`, `known_speaker_references[]` et `include[]` sont
    acceptés et ignorés.
    """
    model = _first(fields, "model")
    trace.model = loggable(model)
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


async def _decode(path: Path, gate: LongAudioGate) -> bytearray:
    """Convertit l'envoi en WAV 16 kHz ; un audio trop long ou illisible devient une erreur 400, 422 ou 429.

    Quand les places des audios longs sont prises, le décodage s'arrête au seuil des audios longs : un audio long
    serait refusé de toute façon, et ne doit pas garder son WAV décodé en mémoire le temps de l'apprendre.
    """
    busy = gate.full
    try:
        return await to_wav16k(path, max_seconds=gate.threshold_s if busy else MAX_AUDIO_SECONDS)
    except AudioTooLong:
        if busy:
            raise long_audio_busy() from None
        raise ApiError(400, "The audio is longer than 90 minutes.", param="file", code="audio_too_long") from None
    except AudioDecodeError:
        raise ApiError(422, "The audio file could not be read.", param="file", code="invalid_audio") from None


@dataclass
class _Recognition:
    """Ce que NeMo a rendu : les textes des phrases finales, leurs mots, les tours de parole."""

    texts: list[str]
    words: list[Word]
    turns: list[Turn]


class _End:
    """Marque, dans la file des deltas, la fin de la reconnaissance, réussie ou non."""


_END = _End()


async def _abandon(recognition: asyncio.Task, trace: Trace) -> None:
    """Filet d'une réponse en continu, lancé par Starlette quand la réponse est finie, même annulée.

    Si le client raccroche entre la création de la réponse et le premier octet, le générateur ne démarre jamais et
    ses `finally` ne courent pas : la reconnaissance, déjà lancée, serait seule à continuer et garderait sa place.
    """
    cancel_once(recognition)
    if not trace.logged:
        trace.status = CLIENT_CLOSED
        trace.log()


@dataclass
class _Job:
    """Une transcription en cours : l'audio converti et ce que ses étapes partagent."""

    state: State
    params: _Params
    profile: Profile
    trace: Trace
    wav: bytearray
    duration: float

    async def recognize(self, on_text: Callable[[str], None] | None = None) -> _Recognition:
        """Les passes de NeMo sur l'audio, sous la place des transcriptions longues.

        `on_text` reçoit le texte de chaque phrase finale dès qu'elle arrive, précédé d'une espace à partir de la
        seconde. La session de NeMo se ferme, et la place se libère, quelle que soit la sortie, annulation comprise.
        Un audio long qui n'a pas de place à attendre lève `long_audio_busy` avant toute passe.
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
                self.trace.nemo_ms = elapsed_ms(started)
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
            self.trace.llm_ms = elapsed_ms(started)
            self.trace.decision = result.decision
            text = result.text
        return Transcript(text, words, self.duration, params.language, segments)

    async def open_stream(self, request: Request) -> Response:
        """Ouvre la réponse en continu, une fois que NeMo a rendu son premier delta ou fini.

        La reconnaissance démarre ici, sous la garde du client qui raccroche. Un échec qui survient avant le premier
        delta est rendu comme celui d'une réponse ordinaire (429 ou 502 pour NeMo, 429 pour une place des audios longs
        refusée) : le statut part après, quand on sait déjà si NeMo répond. Un échec plus tard n'a plus de statut à
        changer et part en événement `error` (voir `events`).
        """
        deltas: asyncio.Queue[str | _End] = asyncio.Queue()
        # Avec la reformulation, le texte part en un seul delta, quand le LLM l'a rendu.
        on_text = None if self.params.mode is Mode.CLEAN else deltas.put_nowait
        recognition = spawn(self.recognize(on_text))
        recognition.add_done_callback(lambda _: deltas.put_nowait(_END))
        try:
            first = await run_unless_disconnected(request, deltas.get())
            if first is None:
                cancel_once(recognition)
                return Response(status_code=CLIENT_CLOSED)
            if first is _END:
                try:
                    recognition.result()
                except NEMO_ERRORS as error:
                    raise nemo_failure(error) from None
        except BaseException:
            cancel_once(recognition)
            raise
        return StreamingResponse(
            self.events(first, deltas, recognition),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
            background=BackgroundTask(_abandon, recognition, self.trace),
        )

    async def events(
        self, first: str | _End, deltas: asyncio.Queue[str | _End], recognition: asyncio.Task[_Recognition]
    ) -> AsyncIterator[str]:
        """Les événements SSE de la réponse en continu ; la ligne de journal s'écrit à leur fin.

        `first` est le premier élément de `deltas`, déjà retiré de la file par `open_stream`. Une erreur arrive quand
        la réponse a déjà commencé : elle part comme un événement `error`, et le flux se ferme.
        """
        try:
            item = first
            while item is not _END:
                yield delta_event(item)
                item = await deltas.get()
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
        except NEMO_ERRORS as error:
            yield self._fail(nemo_failure(error))
        except Exception as error:
            yield self._fail(unexpected_error(error))
        finally:
            cancel_once(recognition)
            self.trace.log()

    def _fail(self, failure: ApiError) -> str:
        self.trace.status = failure.status
        return error_event(failure)


async def _handle(request: Request, trace: Trace) -> Response:
    state = request.app.state
    settings = state.settings
    caller = require_caller(request)
    trace.user = "default" if caller.user_id is None else str(caller.user_id)
    profile = load_profile(state.db, settings, caller)

    # L'envoi vit seul dans son dossier, supprimé dès la conversion faite, quelle que soit l'issue.
    with tempfile.TemporaryDirectory(prefix="dictee-") as directory:
        path = Path(directory) / "upload"
        # Une langue ou un format refusés le sont dès le début du fichier, avant d'en lire le moindre octet.
        fields, has_file = await read_upload(
            request, path, settings.max_upload_mb, lambda early: _parse_params(early, trace)
        )
        params = _parse_params(fields, trace)
        if not has_file:
            raise ApiError(400, "Missing required parameter: 'file'.", param="file", code="missing_required_parameter")
        wav = await _decode(path, state.gate)

    duration = wav_duration(wav)
    trace.audio_s = duration
    if params.mode is Mode.DIARIZE and duration > MAX_DIARIZE_SECONDS:
        raise ApiError(400, "Speaker separation accepts audio up to 59 minutes.", param="file", code="audio_too_long")
    job = _Job(state, params, profile, trace, wav, duration)

    if params.stream:
        return await job.open_stream(request)
    try:
        recognition = await run_unless_disconnected(request, job.recognize())
        if recognition is None:
            return Response(status_code=CLIENT_CLOSED)
        transcript = await job.finish(recognition)
    except NEMO_ERRORS as error:
        raise nemo_failure(error) from None
    return render(transcript, params.response_format, granularities=params.granularities)


async def transcriptions(request: Request) -> Response:
    """Transcrit l'audio envoyé ; chaque requête écrit une ligne dans le journal `dictee`.

    Toute exception imprévue devient ici l'erreur 500 de l'API, avant que Starlette la voie : sinon son middleware
    la relève après avoir répondu, et uvicorn en journalise le message, qui peut citer du texte transcrit.
    """
    trace = Trace(route="transcriptions")
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
    except Exception as error:
        trace.status = 500
        raise unexpected_error(error) from None
    finally:
        # Une réponse en continu écrit sa ligne à la fin du flux.
        if not streaming:
            trace.log()
