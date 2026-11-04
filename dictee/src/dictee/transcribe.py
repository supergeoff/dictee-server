"""Route `POST /v1/audio/transcriptions`, compatible avec celle d'OpenAI.

La route lit l'envoi, dont elle calcule l'empreinte pendant la réception. Sans `stream`, la transcription passe par une
tâche enregistrée : un envoi identique rejoint la tâche existante, sinon l'audio est décodé dans le dossier d'une
tâche neuve, que le runner traite par morceaux ou en réunion. La requête attend la fin de la tâche et répond dans le
format demandé, ou, avec `background=true`, rend tout de suite l'objet de la tâche. Avec `stream=true`, rien n'est
enregistré : l'audio est décodé dans un fichier temporaire et NeMo le transcrit en direct (avec les personnes que sa
session marque ou la reformulation selon le modèle demandé), en événements SSE.

Rien n'est journalisé ici qui vienne du contenu : ni le texte transcrit, ni les consignes, ni l'audio. La ligne de
chaque requête dit qui, quoi, combien de temps, comment elle s'est terminée et quelle tâche elle a servie.

Les pièces que d'autres routes réutilisent sont dans `upload` (lecture de l'envoi), `lifecycle` (ligne de journal,
tâches détachées, client qui raccroche), `errors` (erreurs de NeMo) et `formats` (événements SSE).
"""

import asyncio
import contextlib
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from dictee.audio import AudioDecodeError, AudioTooLong, decode_wav16k
from dictee.auth import require_caller
from dictee.cleanup import clean
from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure, unexpected_error
from dictee.formats import delta_event, done_event, error_event, render, segment_event
from dictee.lifecycle import (
    CLIENT_CLOSED,
    Trace,
    abandon_stream,
    cancel_once,
    elapsed_ms,
    loggable,
    run_unless_disconnected,
    spawn,
)
from dictee.modes import Mode, resolve_mode, to_iso_language, to_nemo_language
from dictee.processing import task_audio, task_dir
from dictee.profile import Profile, glossary_terms, load_profile
from dictee.runner import LongTicket
from dictee.speakers import build_segments, label_speakers
from dictee.tasks_api import task_error, task_object
from dictee.taskstore import TaskParams, TaskRecord, TaskStatus, new_task_id
from dictee.types import Partial, Transcript, Word
from dictee.upload import Upload, read_upload

# Plafond de durée d'un envoi, tous modes confondus : 2 heures, la durée de l'enregistreur. Le WAV décodé vit sur le
# disque, 230 Mio à ce plafond (32 000 octets par seconde), et seule une trame d'une seconde passe en mémoire.
MAX_AUDIO_SECONDS = 7200.0
GRANULARITIES = frozenset({"word", "segment"})


@dataclass(frozen=True)
class _Params:
    """Les champs du formulaire qui pilotent la transcription, une fois validés.

    `model` est le nom de modèle envoyé, vide sans lui : la tâche le garde tel quel.
    """

    model: str
    nemo_language: str
    language: str
    response_format: str
    mode: Mode
    stream: bool
    background: bool
    granularities: frozenset[str]


def _first(fields: dict[str, list[str]], name: str) -> str | None:
    values = fields.get(name)
    return values[0] if values else None


def _flag(fields: dict[str, list[str]], name: str) -> bool:
    """Un drapeau du formulaire, `true` ou `false` en toutes casses, `false` par défaut ; erreur 400 sinon."""
    match (_first(fields, name) or "false").lower():
        case "true":
            return True
        case "false":
            return False
    raise ApiError(400, f"{name} must be 'true' or 'false'.", param=name)


def _parse_params(fields: dict[str, list[str]], trace: Trace) -> _Params:
    """Lit les champs du formulaire, et lève une erreur 400 pour une langue, un format ou des drapeaux refusés.

    Un champ absent prend sa valeur par défaut : la fonction vaut aussi pour les champs reçus avant le fichier.
    `stream` et `background` ne vont pas ensemble : un flux n'a pas de tâche à laisser en arrière-plan. `prompt`,
    `chunking_strategy`, `temperature`, `known_speaker_names[]`, `known_speaker_references[]` et `include[]` sont
    acceptés et ignorés : NeMo ne reçoit jamais `prompt`.
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
    stream = _flag(fields, "stream")
    background = _flag(fields, "background")
    if stream and background:
        message = "stream and background cannot be used together."
        raise ApiError(400, message, param="background", code="unsupported_combination")
    return _Params(
        model=model or "",
        nemo_language=nemo_language,
        language=to_iso_language(language),
        response_format=response_format,
        mode=mode,
        stream=stream,
        background=background,
        granularities=frozenset(values) & GRANULARITIES or frozenset({"segment"}),
    )


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


def _unreadable_audio() -> ApiError:
    """Erreur 422 d'un audio que dictee ne sait pas lire, ou qui ne contient rien à transcrire."""
    return ApiError(422, "The audio file could not be read.", param="file", code="invalid_audio")


async def _decode(src: Path, dst: Path, long_seconds: float, on_long: Callable[[], None]) -> float:
    """Décode l'envoi `src` en WAV 16 kHz dans `dst` et rend sa durée en secondes.

    Un audio trop long ou illisible devient une erreur 400 ou 422. Le décodage s'arrête à `MAX_AUDIO_SECONDS`, pour
    tous les modes : au delà, le reste n'est pas décodé. `on_long` est appelé quand l'audio passe `long_seconds` (voir
    `decode_wav16k`) ; l'erreur 429 qu'il lève arrête le décodage à ce moment. Quelle que soit l'erreur, `dst` n'existe
    plus. L'envoi `src` disparaît dès la fin du décodage, quelle qu'en soit l'issue.
    """
    try:
        return await decode_wav16k(src, dst, max_seconds=MAX_AUDIO_SECONDS, long_seconds=long_seconds, on_long=on_long)
    except AudioTooLong:
        message = f"The audio is longer than {_minutes(MAX_AUDIO_SECONDS)} minutes."
        raise ApiError(400, message, param="file", code="audio_too_long") from None
    except AudioDecodeError:
        raise _unreadable_audio() from None
    finally:
        src.unlink(missing_ok=True)


@dataclass
class _Recognition:
    """Ce que NeMo a rendu : les textes des phrases finales et leurs mots."""

    texts: list[str]
    words: list[Word]


class _End:
    """Marque, dans la file des deltas, la fin de la reconnaissance, réussie ou non."""


_END = _End()


class _DeltaFeed:
    """Découpe en deltas le texte que NeMo rend : un par `Partial`, puis le reste de la phrase finale de l'item.

    Le texte de `done` joint les phrases finales par une espace : le premier delta de chaque item, sauf du premier
    item non vide, la porte, quelles que soient les espaces que NeMo met en tête de ses morceaux. Quand la phrase
    finale ne commence pas par ce qui est déjà parti pour son item, rien de plus ne part : le texte de `done` fait
    foi, la suite reprend avec l'item suivant, et `mismatches` compte ces items.
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._sent_any = False  # un texte est déjà parti : le premier delta d'un nouvel item porte l'espace
        self._item = ""  # ce qui est parti pour l'item en cours
        self.mismatches = 0  # items dont la phrase finale ne prolonge pas ce qui est parti

    def partial(self, text: str) -> None:
        # Le premier morceau d'un item perd ses espaces de tête : seule celle qui sépare les items est posée.
        self._send(text if self._item else text.lstrip())

    def final(self, text: str) -> None:
        """Termine l'item avec le texte de sa phrase finale, sans espaces autour, vide ou non."""
        if text.startswith(self._item):
            self._send(text[len(self._item) :])
        else:
            self.mismatches += 1
        self._item = ""

    def _send(self, text: str) -> None:
        if not text:
            return
        first = not self._item
        self._item += text
        self._emit(f" {text}" if first and self._sent_any else text)
        self._sent_any = True


@dataclass
class _Job:
    """Un flux en cours : l'audio converti et ce que ses étapes partagent.

    `wav` est le WAV décodé, dans le dossier `scratch`. Le dossier appartient au flux : il disparaît à la fin de la
    reconnaissance, quelle qu'en soit l'issue (voir `open_stream`). `ticket` est la place d'un audio long dans la file
    des tâches longues, réservée pendant le décodage, ou `None` pour un audio court.
    """

    state: State
    params: _Params
    profile: Profile
    trace: Trace
    wav: Path
    duration: float
    scratch: tempfile.TemporaryDirectory
    ticket: LongTicket | None = None

    async def recognize(self, on_text: Callable[[str], None] | None = None) -> _Recognition:
        """Les passes de NeMo sur l'audio, à leur tour parmi les tâches longues.

        `on_text` reçoit le texte au fur et à mesure (voir `_DeltaFeed`) : chaque morceau que NeMo produit en cours de
        route, puis le reste de la phrase finale de son item. Sans `on_text`, NeMo ne rend que les phrases finales. En
        mode réunion, la session de NeMo marque les personnes (`speakers`). La session de NeMo se ferme, et la place se
        libère, quelle que soit la sortie, annulation comprise. Un audio long a déjà sa place, réservée pendant son
        décodage : il attend son tour derrière la tâche longue qui tourne, sans pouvoir être refusé ici. Un audio court
        n'attend pas.
        """
        state, params = self.state, self.params
        texts: list[str] = []
        words: list[Word] = []
        feed = None if on_text is None else _DeltaFeed(on_text)
        async with contextlib.nullcontext() if self.ticket is None else self.ticket.turn():
            started = time.monotonic()
            try:
                async with contextlib.aclosing(
                    state.nemo.transcribe(
                        self.wav,
                        language=params.nemo_language,
                        phrases=glossary_terms(self.profile.glossary),
                        boost=state.settings.glossary_boost,
                        speakers=params.mode is Mode.DIARIZE,
                        partials=feed is not None,
                    )
                ) as events:
                    async for event in events:
                        if isinstance(event, Partial):
                            if feed is not None:
                                feed.partial(event.text)
                            continue
                        words.extend(event.words)
                        text = event.text.strip()
                        if text:
                            texts.append(text)
                        if feed is not None:
                            feed.final(text)
            finally:
                self.trace.nemo_ms = elapsed_ms(started)
                if feed is not None:
                    self.trace.mismatch = feed.mismatches
        return _Recognition(texts, words)

    async def finish(self, recognition: _Recognition) -> Transcript:
        """Le texte entier, avec ses personnes ou reformulé selon le mode."""
        params = self.params
        text = " ".join(recognition.texts)
        words = tuple(recognition.words)
        segments = None
        if params.mode is Mode.DIARIZE:
            words = tuple(label_speakers(words))
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

        Le premier delta est le premier morceau de texte que NeMo produit en cours de route (un `Partial`), bien avant
        sa première phrase finale ; avec la reformulation, qui ne relaie rien, la réponse s'ouvre à la fin de la
        reconnaissance. La reconnaissance démarre ici, sous la garde du client qui raccroche. Un échec qui survient
        avant le premier delta est rendu comme celui d'une réponse ordinaire (429 ou 502 pour NeMo) : le statut part
        après, quand on sait déjà si NeMo répond. Un échec plus tard n'a plus de statut à changer et part en événement
        `error` (voir `events`). La place d'un audio long, réservée pendant le décodage, et le dossier du WAV se
        rendent à la fin de la reconnaissance.
        """
        deltas: asyncio.Queue[str | _End] = asyncio.Queue()
        # Avec la reformulation, le texte part en un seul delta, quand le LLM l'a rendu.
        on_text = None if self.params.mode is Mode.CLEAN else deltas.put_nowait
        recognition = spawn(self.recognize(on_text))
        recognition.add_done_callback(lambda _: deltas.put_nowait(_END))
        # Une tâche annulée avant son premier pas n'entre jamais dans `recognize` : le dossier du WAV et la place se
        # rendent ici, à la fin de la tâche. Le dossier est créé avec `ignore_cleanup_errors` (voir `_handle`) : son
        # effacement ne lève jamais dans ce rappel.
        recognition.add_done_callback(lambda _: self.scratch.cleanup())
        if self.ticket is not None:
            recognition.add_done_callback(lambda _: self.ticket.release())
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
            background=BackgroundTask(abandon_stream, recognition, self.trace),
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


async def _stream(
    request: Request,
    trace: Trace,
    params: _Params,
    profile: Profile,
    scratch: tempfile.TemporaryDirectory,
    upload_path: Path,
) -> Response:
    """Le texte progressif (`stream=true`) : décode l'envoi à côté de lui, puis ouvre la réponse en continu.

    Aucune tâche n'est créée. Un audio long réserve sa place dans la file des tâches longues pendant son décodage, comme
    une tâche, et sa reconnaissance attend son tour (`_Job.recognize`). Le dossier `scratch` appartient au flux dès
    l'appel : il est rendu ici, avec la place, si la reconnaissance ne démarre pas, et sinon par `open_stream` à la fin
    de la reconnaissance.
    """
    state = request.app.state
    ticket: LongTicket | None = None

    def reserve() -> None:
        nonlocal ticket
        ticket = state.lane.reserve()

    job = None
    try:
        wav = Path(scratch.name) / "audio.wav"
        duration = await _decode(upload_path, wav, state.settings.long_audio_seconds, reserve)
        trace.audio_s = duration
        job = _Job(state, params, profile, trace, wav, duration, scratch, ticket)
    finally:
        if job is None:
            scratch.cleanup()
            if ticket is not None:
                ticket.release()
    return await job.open_stream(request)


async def _task_for(
    state: State, owner: int | None, params: _Params, profile: Profile, upload: Upload, upload_path: Path
) -> TaskRecord:
    """La tâche de cet envoi : une tâche identique qu'il rejoint sans rien décoder, ou une tâche neuve, déjà lancée.

    Deux envois sont identiques quand ils ont le même propriétaire, les mêmes octets et les mêmes réglages : langue,
    mode, glossaire et force, plus les consignes pour `dictee-clean` seulement, pour qu'une dictée brute ne dépende
    pas des consignes de reformulation. Le format de réponse ne compte pas. Une tâche neuve a son dossier, où l'envoi
    est décodé ; un audio long y réserve sa place dans la file des tâches longues dès qu'il passe le seuil, et un refus
    arrête le décodage à ce moment. Un audio qui dure 0 s est refusé comme un audio illisible : il n'a rien à
    transcrire, et son traitement diviserait par sa durée. Quel que soit le refus ou l'erreur avant le lancement, le
    dossier est effacé et la place rendue ; une fois la tâche lancée, le runner en a la charge.
    """
    settings = state.settings
    instructions = profile.instructions if params.mode is Mode.CLEAN else None
    task_params = TaskParams(params.nemo_language, params.mode, profile.glossary, settings.glossary_boost, instructions)
    found = state.store.find_identical(owner, upload.sha256, task_params.digest(), int(time.time()))
    if found is not None:
        return found

    task_id = new_task_id()
    folder = task_dir(settings, task_id)
    ticket: LongTicket | None = None

    def reserve() -> None:
        nonlocal ticket
        ticket = state.lane.reserve()

    try:
        folder.mkdir(parents=True)
        duration = await _decode(upload_path, task_audio(settings, task_id), settings.long_audio_seconds, reserve)
        if duration <= 0:
            raise _unreadable_audio()
        task = state.store.create(
            task_id=task_id,
            owner=owner,
            model=params.model,
            params=task_params,
            response_format=params.response_format,
            granularities=params.granularities,
            upload_sha256=upload.sha256,
            duration=duration,
            now=int(time.time()),
        )
        # Rien n'attend entre la création et le lancement : une annulation ne peut pas s'y glisser.
        state.runner.submit(task, ticket)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        if ticket is not None:
            ticket.release()
        raise
    return task


async def _handle(request: Request, trace: Trace) -> Response:
    state = request.app.state
    settings = state.settings
    caller = require_caller(request)
    trace.user = "default" if caller.user_id is None else str(caller.user_id)
    profile = load_profile(state.db, settings, caller)

    # Le dossier garde l'envoi le temps de le lire et de le décoder. Un flux garde ensuite son WAV à côté et prend le
    # dossier à sa charge (`_stream`) ; pour une tâche, le dossier disparaît dès qu'elle est trouvée ou créée. Un
    # effacement qui échoue est ignoré : il ne change pas la réponse et ne lève rien dans le rappel de `open_stream`.
    scratch = tempfile.TemporaryDirectory(prefix="dictee-", ignore_cleanup_errors=True)
    handed_over = False
    try:
        upload_path = Path(scratch.name) / "upload"
        # Une langue, un format ou des drapeaux refusés le sont dès le début du fichier, avant d'en lire un octet.
        upload = await read_upload(
            request, upload_path, settings.max_upload_mb, lambda early: _parse_params(early, trace)
        )
        params = _parse_params(upload.fields, trace)
        if not upload.has_file:
            raise ApiError(400, "Missing required parameter: 'file'.", param="file", code="missing_required_parameter")
        if params.stream:
            handed_over = True
            return await _stream(request, trace, params, profile, scratch, upload_path)
        task = await _task_for(state, caller.user_id, params, profile, upload, upload_path)
    finally:
        if not handed_over:
            scratch.cleanup()

    # Les temps de NeMo et du LLM d'une tâche sont sur la ligne de la tâche, que le runner écrit à sa fin.
    trace.task = task.id
    trace.audio_s = task.duration
    if params.background:
        return JSONResponse(task_object(task))
    # `wait` n'est jamais réveillé après l'arrêt du runner : la garde du client qui raccroche la borne. Un client parti
    # laisse la tâche aller à son terme.
    finished = await run_unless_disconnected(request, state.runner.wait(task.id))
    if finished is None:
        return Response(status_code=CLIENT_CLOSED)
    if finished.status is TaskStatus.COMPLETED:
        return render(finished.result.transcript(), params.response_format, granularities=params.granularities)
    error = task_error(finished)
    if error.code == "server_error":
        # La requête synchrone, comme un flux, rend une erreur imprévue sans code : le corps reste le même.
        error = ApiError(error.status, error.message, type=error.type)
    raise error


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
