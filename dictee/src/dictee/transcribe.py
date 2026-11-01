"""Route `POST /v1/audio/transcriptions`, compatible avec celle d'OpenAI.

La route lit l'envoi, décode l'audio dans un fichier temporaire, fait transcrire par NeMo (avec les personnes que sa
session marque ou la reformulation selon le modèle demandé) et répond dans le format demandé, en une fois ou en
événements SSE. Rien n'est journalisé ici qui vienne du contenu : ni le texte transcrit, ni les consignes, ni l'audio.
La ligne de chaque requête dit qui, quoi, combien de temps et comment elle s'est terminée.

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

from dictee.audio import AudioDecodeError, AudioTooLong, decode_wav16k
from dictee.auth import require_caller
from dictee.cleanup import clean
from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure, unexpected_error
from dictee.formats import delta_event, done_event, error_event, render, segment_event
from dictee.lifecycle import CLIENT_CLOSED, Trace, cancel_once, elapsed_ms, loggable, run_unless_disconnected, spawn
from dictee.modes import Mode, resolve_mode, to_iso_language, to_nemo_language
from dictee.profile import Profile, glossary_terms, load_profile
from dictee.runner import long_audio_busy
from dictee.speakers import build_segments, label_speakers
from dictee.types import Partial, Transcript, Word
from dictee.upload import read_upload

# Plafond de durée d'un envoi, tous modes confondus : 2 heures, la durée de l'enregistreur. Le WAV décodé vit sur le
# disque, 230 Mio à ce plafond (32 000 octets par seconde), et seule une trame d'une seconde passe en mémoire.
MAX_AUDIO_SECONDS = 7200.0
GRANULARITIES = frozenset({"word", "segment"})


class LongAudioGate:
    """Une transcription longue à la fois et une seule en attente, pour borner la mémoire de NeMo et le disque.

    Un audio plus long que `threshold_s` est « long » ; un audio de `threshold_s` ou moins est « court » et ne passe
    pas par la porte : il n'attend jamais, même quand les places des longs sont prises. Le décodage réserve la place
    d'un audio long (`reserve`) dès qu'il franchit le seuil, donc avant d'avoir le WAV entier sur le disque : une place
    pour celui qui tourne, une pour celui qui attend. Un audio long qui arrive quand les deux sont prises est refusé
    tout de suite par `long_audio_busy`, et son décodage s'arrête au seuil.
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

    def reserve(self) -> "LongPlace":
        """Réserve la place d'un audio long ; lève `long_audio_busy` quand celui qui tourne et celui qui attend sont là.

        La place se rend avec `LongPlace.release`, ou en quittant le bloc `LongPlace.turn`.
        """
        if self.full:
            raise long_audio_busy()
        self._long += 1
        return LongPlace(self)


class LongPlace:
    """Une place de la porte des audios longs : à son tour, celle qui tourne, avant lui, celle qui attend."""

    def __init__(self, gate: LongAudioGate) -> None:
        self._gate = gate
        self._held = True

    def release(self) -> None:
        """Rend la place à la porte ; sans effet quand elle est déjà rendue."""
        if self._held:
            self._held = False
            self._gate._long -= 1

    @contextlib.asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        """Attend derrière l'audio long qui tourne, puis tourne jusqu'à la sortie du bloc.

        La place est rendue à la sortie, quelle qu'elle soit : fin normale, erreur ou annulation, y compris pendant
        l'attente. Un audio long annulé pendant qu'il attend libère donc sa place d'attente.
        """
        try:
            async with self._gate._lock:
                yield
        finally:
            self.release()


@dataclass(frozen=True)
class _Params:
    """Les champs du formulaire qui pilotent la transcription, une fois validés."""

    nemo_language: str
    language: str
    response_format: str
    mode: Mode
    stream: bool
    granularities: frozenset[str]


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
    `prompt`, `chunking_strategy`, `temperature`, `known_speaker_names[]`, `known_speaker_references[]` et
    `include[]` sont acceptés et ignorés : NeMo ne reçoit jamais `prompt`.
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
    )


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


async def _decode(src: Path, dst: Path, long_seconds: float, on_long: Callable[[], None]) -> float:
    """Décode l'envoi `src` en WAV 16 kHz dans `dst` et rend sa durée en secondes.

    Un audio trop long ou illisible devient une erreur 400 ou 422. Le décodage s'arrête à `MAX_AUDIO_SECONDS`, pour
    tous les modes : au delà, le reste n'est pas décodé. `on_long` est appelé quand l'audio passe `long_seconds` (voir
    `decode_wav16k`) ; l'erreur 429 qu'il lève arrête le décodage à ce moment. Quelle que soit l'erreur, `dst` n'existe
    plus.
    """
    try:
        return await decode_wav16k(src, dst, max_seconds=MAX_AUDIO_SECONDS, long_seconds=long_seconds, on_long=on_long)
    except AudioTooLong:
        message = f"The audio is longer than {_minutes(MAX_AUDIO_SECONDS)} minutes."
        raise ApiError(400, message, param="file", code="audio_too_long") from None
    except AudioDecodeError:
        raise ApiError(422, "The audio file could not be read.", param="file", code="invalid_audio") from None


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
    """Une transcription en cours : l'audio converti et ce que ses étapes partagent.

    `wav` est le WAV décodé, dans le dossier `scratch`. Le dossier appartient à la transcription : il disparaît à la
    fin de la reconnaissance, quelle qu'en soit l'issue (par `_handle` ou, pour une réponse en continu, par
    `open_stream`).
    """

    state: State
    params: _Params
    profile: Profile
    trace: Trace
    wav: Path
    duration: float
    scratch: tempfile.TemporaryDirectory
    place: LongPlace | None = None

    async def recognize(self, on_text: Callable[[str], None] | None = None) -> _Recognition:
        """Les passes de NeMo sur l'audio, à leur tour parmi les transcriptions longues.

        `on_text` reçoit le texte au fur et à mesure (voir `_DeltaFeed`) : chaque morceau que NeMo produit en cours de
        route, puis le reste de la phrase finale de son item. Sans `on_text`, NeMo ne rend que les phrases finales. En
        mode réunion, la session de NeMo marque les personnes (`speakers`). La session de NeMo se ferme, et la place se
        libère, quelle que soit la sortie, annulation comprise. Un audio long a déjà sa place, réservée pendant son
        décodage : il attend son tour derrière celui qui tourne, sans pouvoir être refusé ici. Un audio court n'attend
        pas.
        """
        state, params = self.state, self.params
        texts: list[str] = []
        words: list[Word] = []
        feed = None if on_text is None else _DeltaFeed(on_text)
        async with contextlib.nullcontext() if self.place is None else self.place.turn():
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
        # rendent ici, à la fin de la tâche.
        recognition.add_done_callback(lambda _: self.scratch.cleanup())
        if self.place is not None:
            recognition.add_done_callback(lambda _: self.place.release())
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

    # Le décodage réserve la place d'un audio long dès qu'il passe le seuil : un refus l'arrête à ce moment.
    place: LongPlace | None = None

    def reserve() -> None:
        nonlocal place
        place = state.gate.reserve()

    handed_over = False
    # Le dossier garde l'envoi, puis le WAV décodé. Il vit jusqu'à la fin de la reconnaissance, quelle qu'en soit
    # l'issue : pour une réponse en continu, le rappel de fin de la tâche de reconnaissance le rend (`open_stream`).
    scratch = tempfile.TemporaryDirectory(prefix="dictee-")
    try:
        directory = Path(scratch.name)
        upload, wav = directory / "upload", directory / "audio.wav"
        # Une langue ou un format refusés le sont dès le début du fichier, avant d'en lire le moindre octet.
        fields, has_file = await read_upload(
            request, upload, settings.max_upload_mb, lambda early: _parse_params(early, trace)
        )
        params = _parse_params(fields, trace)
        if not has_file:
            raise ApiError(400, "Missing required parameter: 'file'.", param="file", code="missing_required_parameter")
        try:
            duration = await _decode(upload, wav, state.gate.threshold_s, reserve)
        finally:
            upload.unlink(missing_ok=True)  # l'envoi disparaît dès le décodage fini, quelle qu'en soit l'issue
        trace.audio_s = duration
        job = _Job(state, params, profile, trace, wav, duration, scratch, place)

        if params.stream:
            handed_over = True  # la reconnaissance, lancée par `open_stream`, rend la place et le dossier à sa fin
            return await job.open_stream(request)
        try:
            recognition = await run_unless_disconnected(request, job.recognize())
            if recognition is None:
                return Response(status_code=CLIENT_CLOSED)
            transcript = await job.finish(recognition)
        except NEMO_ERRORS as error:
            raise nemo_failure(error) from None
        return render(transcript, params.response_format, granularities=params.granularities)
    finally:
        # Quelle que soit la sortie (refus, audio illisible, panne, client parti), la place réservée et le dossier sont
        # rendus.
        if not handed_over:
            scratch.cleanup()
            if place is not None:
                place.release()


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
