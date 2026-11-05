"""Route `POST /v1/audio/speech`, compatible avec celle d'OpenAI.

La route découpe le texte en phrases, fait synthétiser la première par NeMo avant d'ouvrir la réponse, puis rend le
son au fil de l'eau : un seul ffmpeg encode le son de toutes les phrases, synthétisées l'une après l'autre. Rien n'est
journalisé ici qui vienne du contenu : ni le texte à dire, ni l'audio. La ligne de chaque requête dit qui, quoi et
comment elle s'est terminée.

Les pièces que la route de transcription partage sont dans `lifecycle` (tâches détachées, client qui raccroche) et
`errors` (erreurs de NeMo et erreur imprévue).
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse

from dictee.audio import MAX_SPEED, MIN_SPEED, OUTPUT_FORMATS, encode_stream
from dictee.auth import require_caller
from dictee.errors import NEMO_ERRORS, ApiError, nemo_failure, unexpected_error
from dictee.lifecycle import (
    CLIENT_CLOSED,
    RequestTrace,
    abandon_stream,
    cancel_once,
    elapsed_ms,
    loggable,
    run_unless_disconnected,
    spawn,
)
from dictee.nemo import TTS_SAMPLE_RATE
from dictee.profile import load_profile
from dictee.upload import bounded_request

logger = logging.getLogger("dictee")

# Au-delà, une phrase est recoupée : NeMo met environ deux fois la durée du son à le synthétiser, et son délai de
# réponse (120 s) doit suffire pour une phrase.
MAX_SENTENCE_CHARS = 300
# Limite de l'API d'OpenAI : la synthèse d'un texte plus long dure plusieurs minutes.
MAX_INPUT_CHARS = 4096
# Le corps d'une requête est un petit objet JSON ; sa lecture est bornée, car elle se fait en mémoire.
MAX_BODY_BYTES = 1024 * 1024
# La langue de la synthèse est toujours le français, quelle que soit la requête.
LANGUAGE = "fr-FR"
DEFAULT_FORMAT = "mp3"

# Fin d'une phrase : un signe final ou plusieurs (`?!`, `...`), puis les guillemets et parenthèses fermants qui le
# suivent (« Salut. » garde son guillemet). Une espace ou la fin de la ligne doit venir ensuite : « 3.14 » et
# « example.org » ne se coupent pas. L'espace qui précède le signe (« Ça va ? ») reste dans la phrase. Le premier
# lookbehind limite le départ d'une recherche au début d'une suite de signes : sans lui, une longue suite de signes
# suivie d'une lettre est reprise depuis chacun de ses signes, en temps quadratique.
_SENTENCE_END = re.compile(r"(?<![.!?…])[.!?…]+(?:[ \u00a0\u202f]?»|[”\"')\]])*(?!\S)")
# Coupes d'une phrase trop longue, de la plus naturelle à la moins naturelle : après une virgule, après une espace.
_CUTS = (re.compile(r"(?<=,)"), re.compile(r"(?<=[ \t])"))


def _fit(text: str, max_chars: int, cuts: tuple[re.Pattern[str], ...]) -> list[str]:
    """Morceaux de `text` d'au plus `max_chars` caractères, sans espace de bord, coupés sur les `cuts` dans l'ordre.

    Les parties que la première coupe sépare se regroupent tant que le groupe tient. Une partie encore trop longue
    passe aux coupes suivantes, et un mot plus long que `max_chars` se coupe net au caractère.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    if not cuts:
        slices = (text[start : start + max_chars].strip() for start in range(0, len(text), max_chars))
        return [piece for piece in slices if piece]
    chunks: list[str] = []
    current = ""
    for part in cuts[0].split(text):
        if len((current + part).strip()) <= max_chars:
            current += part
            continue
        chunks += _fit(current, max_chars, cuts[1:])
        current = part
    return chunks + _fit(current, max_chars, cuts[1:])


def _has_speech(piece: str) -> bool:
    """Vrai si `piece` contient une lettre ou un chiffre : sinon NeMo n'a rien à dire."""
    return any(char.isalnum() for char in piece)


def _attach_silent(pieces: list[str]) -> list[str]:
    """Rattache chaque morceau sans lettre ni chiffre au morceau précédent, ou au suivant s'il n'y en a pas.

    Les deux morceaux se joignent par une espace. Si aucun morceau ne contient de lettre ni de chiffre, la liste est
    rendue telle quelle.
    """
    merged: list[str] = []
    leading: list[str] = []
    for piece in pieces:
        if _has_speech(piece):
            merged.append(" ".join([*leading, piece]))
            leading = []
        elif merged:
            merged[-1] += " " + piece
        else:
            leading.append(piece)
    return merged or leading


def split_sentences(text: str, max_chars: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Découpe `text` en phrases, sur la ponctuation finale (`.`, `!`, `?`, `…`) et sur chaque saut de ligne.

    Une phrase de plus de `max_chars` caractères est recoupée sur ses virgules, puis sur ses espaces, puis au
    caractère pour un mot qui dépasse seul la limite. Les espaces de bord et les morceaux vides disparaissent. Un
    morceau sans lettre ni chiffre (`...` entre deux phrases, un signe resté seul après une coupe) se rattache au
    morceau précédent, ou au suivant s'il n'y en a pas : la limite compte donc les lettres et les chiffres de chaque
    morceau, et non ces signes. Aucun autre caractère n'est perdu ni réordonné. Lève `ValueError` si `max_chars` est
    inférieur à 1.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars}")
    sentences: list[str] = []
    for line in text.splitlines():
        start = 0
        for end in _SENTENCE_END.finditer(line):
            sentences += _fit(line[start : end.end()], max_chars, _CUTS)
            start = end.end()
        sentences += _fit(line[start:], max_chars, _CUTS)
    return _attach_silent(sentences)


def _nemo_name(voice: str | None, available: Sequence[str]) -> str | None:
    """Nom de `voice` tel que NeMo l'écrit, sans tenir compte de la casse ; `None` si NeMo ne la propose pas."""
    if voice:
        wanted = voice.casefold()
        for name in available:
            if name.casefold() == wanted:
                return name
    return None


def resolve_voice(requested: str | None, profile_voice: str, available: Sequence[str]) -> str:
    """Voix de la synthèse : celle demandée si NeMo la propose, sinon celle du profil, sans tenir compte de la casse.

    Le nom rendu est écrit comme NeMo l'écrit (`leo` donne `Leo`), pour la voix du profil comme pour celle de la requête
    (la voix par défaut du service, que `DEFAULT_VOICE` fixe, passe par là). Les voix d'OpenAI (`alloy`, `nova`), que
    les clients envoient par défaut, ne sont pas des voix de NeMo : elles donnent la voix du profil. Une voix du profil
    que NeMo ne propose pas part telle quelle, NeMo restant seul juge, et laisse un avertissement au journal : elle
    vient d'un réglage et non de la requête, donc le même nom reviendra à chaque appel. Sans voix de repli (chaîne
    vide), la fonction rend une chaîne vide pour un nom inconnu, sans rien dire.
    """
    for voice in (requested, profile_voice):
        name = _nemo_name(voice, available)
        if name is not None:
            return name
    if profile_voice:
        logger.warning("voice unknown to nemo name=%s", loggable(profile_voice))
    return profile_voice


@dataclass(kw_only=True)
class _Trace(RequestTrace):
    """Ce que la ligne de journal d'une requête dit d'elle ; un champ resté inconnu s'écrit `-`.

    `model` est le modèle que la requête nomme, rendu sûr par `loggable`. `first_ms` est le délai entre l'arrivée de la
    requête et le premier PCM rendu par NeMo : il compte la lecture des voix (la première fois), le profil et la
    synthèse de la première phrase. `nemo_ms` est le temps cumulé des appels à NeMo (lecture des voix et synthèse de
    chaque phrase), même ceux qui échouent ou qu'on abandonne. Le statut et l'écriture unique de la ligne viennent de
    `RequestTrace`.
    """

    user: str = "-"
    model: str = "-"
    chars: int | None = None
    sentences: int | None = None
    voice: str = "-"
    format: str = "-"
    first_ms: int | None = None
    nemo_ms: int | None = None
    started: float = field(default_factory=time.monotonic)

    @contextlib.contextmanager
    def nemo_call(self) -> Iterator[None]:
        """Compte dans `nemo_ms` le temps d'un appel à NeMo, qu'il réussisse, échoue ou soit annulé."""
        called = time.monotonic()
        try:
            yield
        finally:
            self.nemo_ms = (self.nemo_ms or 0) + elapsed_ms(called)

    async def speak(self, nemo: Any, sentence: str, voice: str) -> bytes:
        """Fait synthétiser `sentence` par NeMo ; le premier PCM reçu fixe `first_ms`."""
        with self.nemo_call():
            pcm = await nemo.speak(sentence, voice=voice, language=LANGUAGE)
        if self.first_ms is None:
            self.first_ms = elapsed_ms(self.started)
        return pcm

    def _write(self) -> None:
        logger.info(
            "user=%s route=speech model=%s chars=%s sentences=%s voice=%s format=%s first_ms=%s nemo_ms=%s status=%d",
            self.user,
            self.model,
            "-" if self.chars is None else self.chars,
            "-" if self.sentences is None else self.sentences,
            self.voice,
            self.format,
            "-" if self.first_ms is None else self.first_ms,
            "-" if self.nemo_ms is None else self.nemo_ms,
            self.status,
        )


@dataclass(frozen=True)
class _Params:
    """Les champs du corps qui pilotent la synthèse, une fois validés."""

    text: str
    voice: str | None
    response_format: str
    speed: float


def _body_too_large() -> ApiError:
    message = f"The request body is larger than the limit of {MAX_BODY_BYTES // (1024 * 1024)} MiB."
    return ApiError(413, message, code="payload_too_large")


async def _read_json(request: Request) -> dict[str, Any]:
    """Lit le corps JSON de la requête, borné à `MAX_BODY_BYTES` (413 au-delà).

    Un corps illisible ou qui n'est pas un objet JSON donne une erreur 400.
    """
    body = await bounded_request(request, MAX_BODY_BYTES, _body_too_large).body()
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise ApiError(400, "The request body must be valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(400, "The request body must be a JSON object.")
    return data


def _parse_params(body: dict[str, Any], trace: _Trace) -> _Params:
    """Valide les champs, dans l'ordre : `input`, `response_format`, `speed` ; la première erreur est un 400.

    `voice` absent ou non textuel est ignoré (la voix du profil s'applique). `model`, `instructions`,
    `stream_format` et tout autre champ sont acceptés et ignorés ; `model` n'entre que dans la ligne de journal.
    """
    model = body.get("model")
    trace.model = loggable(model if isinstance(model, str) else None)
    text = body.get("input")
    if text is None:
        raise ApiError(400, "Missing required parameter: 'input'.", param="input", code="missing_required_parameter")
    if not isinstance(text, str):
        raise ApiError(400, "'input' must be a string.", param="input", code="invalid_type")
    trace.chars = len(text)
    if not text.strip():
        raise ApiError(400, "'input' must not be empty.", param="input", code="empty_string")
    if len(text) > MAX_INPUT_CHARS:
        message = f"'input' is longer than {MAX_INPUT_CHARS} characters."
        raise ApiError(400, message, param="input", code="string_above_max_length")
    if not _has_speech(text):
        raise ApiError(400, "'input' must contain a letter or a digit.", param="input", code="invalid_value")
    try:
        text.encode("utf-8")  # un substitut isolé (`\ud800` en JSON) ne s'encode pas : httpx lèverait une erreur
    except UnicodeEncodeError:
        raise ApiError(400, "'input' must be valid Unicode text.", param="input", code="invalid_value") from None

    response_format = body.get("response_format", DEFAULT_FORMAT)
    if response_format is None:
        response_format = DEFAULT_FORMAT
    if not isinstance(response_format, str) or response_format not in OUTPUT_FORMATS:
        message = f"Unsupported response_format. Use one of: {', '.join(OUTPUT_FORMATS)}."
        raise ApiError(400, message, param="response_format")
    trace.format = response_format

    speed = body.get("speed", 1.0)
    if speed is None:
        speed = 1.0
    if isinstance(speed, bool) or not isinstance(speed, int | float) or not MIN_SPEED <= speed <= MAX_SPEED:
        message = f"speed must be a number between {MIN_SPEED:g} and {MAX_SPEED:g}."
        raise ApiError(400, message, param="speed")

    voice = body.get("voice")
    return _Params(text, voice if isinstance(voice, str) else None, response_format, float(speed))


async def list_voices(state: State) -> tuple[str, ...]:
    """Les voix de NeMo, lues une fois puis gardées dans `state.voices` ; un échec de la lecture n'est pas gardé.

    La page de configuration s'en sert aussi, pour la liste des voix et pour valider celle qu'on choisit.
    """
    if state.voices is None:
        state.voices = tuple(await state.nemo.voices())
    return state.voices


async def _open(state: State, params: _Params, profile_voice: str, sentence: str, trace: _Trace) -> tuple[str, bytes]:
    """Choisit la voix et fait synthétiser la première phrase ; rend la voix et le PCM de cette phrase."""
    with trace.nemo_call():
        available = await list_voices(state)
    voice = resolve_voice(params.voice, profile_voice, available)
    trace.voice = loggable(voice)
    return voice, await trace.speak(state.nemo, sentence, voice)


class _End:
    """Marque, dans la file des blocs encodés, la fin de l'encodage, réussi ou non."""


_END = _End()


@dataclass
class _Synthesis:
    """La synthèse en cours : la voix, le format, le PCM de la première phrase et les phrases qui restent."""

    nemo: Any
    voice: str
    fmt: str
    speed: float
    first: bytes
    rest: list[str]
    trace: _Trace

    async def _pcm(self) -> AsyncIterator[bytes]:
        """Le PCM de chaque phrase : la première est déjà synthétisée, les autres le sont l'une après l'autre."""
        yield self.first
        for sentence in self.rest:
            yield await self.trace.speak(self.nemo, sentence, self.voice)

    async def _encode(self, chunks: asyncio.Queue[bytes | _End]) -> None:
        """Encode le PCM des phrases et pose les blocs dans `chunks`.

        Elle tourne dans sa propre tâche (voir `lifecycle.spawn`) : Starlette annule une réponse en continu avec une
        portée d'annulation d'anyio, qui renouvelle l'annulation à chaque attente et interromprait le nettoyage
        d'`encode_stream` à sa première attente, avec ffmpeg tué mais ni attendu, ni ses tubes vidés. Annulée par
        `cancel_once`, la tâche va jusqu'au bout de ce nettoyage : ffmpeg est tué, la phrase en cours de synthèse est
        abandonnée, puis le flux d'entrée est fermé.
        """
        async with (
            contextlib.aclosing(self._pcm()) as pcm,
            contextlib.aclosing(
                encode_stream(pcm, fmt=self.fmt, speed=self.speed, input_rate=TTS_SAMPLE_RATE)
            ) as encoded,
        ):
            async for chunk in encoded:
                chunks.put_nowait(chunk)

    def respond(self) -> StreamingResponse:
        """La réponse en continu ; l'encodage démarre ici, avant le premier octet envoyé."""
        chunks: asyncio.Queue[bytes | _End] = asyncio.Queue()
        encoding = spawn(self._encode(chunks))
        encoding.add_done_callback(lambda _: chunks.put_nowait(_END))
        return StreamingResponse(
            self._stream(chunks, encoding),
            media_type=OUTPUT_FORMATS[self.fmt],
            background=BackgroundTask(abandon_stream, encoding, self.trace),
        )

    async def _stream(self, chunks: asyncio.Queue[bytes | _End], encoding: asyncio.Task) -> AsyncIterator[bytes]:
        """Les blocs encodés, au fur et à mesure ; la ligne de journal s'écrit à la fin du flux.

        Le statut 200 est parti avec la réponse : une panne de NeMo, de ffmpeg ou imprévue ne peut plus que couper le
        flux. Elle laisse au journal un avertissement qui ne donne que la classe de l'exception, car son message
        pourrait citer le texte.
        """
        try:
            while (chunk := await chunks.get()) is not _END:
                yield chunk
            encoding.result()
            self.trace.status = 200
        except (asyncio.CancelledError, GeneratorExit):
            self.trace.status = CLIENT_CLOSED
            raise
        except Exception as error:
            self.trace.status = nemo_failure(error).status if isinstance(error, NEMO_ERRORS) else 500
            logger.warning("speech stream cut class=%s", type(error).__name__)
        finally:
            cancel_once(encoding)
            self.trace.log()


async def _handle(request: Request, trace: _Trace) -> Response:
    state = request.app.state
    caller = require_caller(request)
    trace.user = "default" if caller.user_id is None else str(caller.user_id)
    params = _parse_params(await _read_json(request), trace)
    sentences = split_sentences(params.text)
    trace.sentences = len(sentences)
    profile = load_profile(state.db, state.settings, caller)
    # La première phrase est synthétisée avant d'ouvrir la réponse : une panne de NeMo à ce moment est rendue comme
    # celle d'une réponse ordinaire (429 ou 502), le statut partant après, quand on sait déjà si NeMo répond.
    try:
        opened = await run_unless_disconnected(request, _open(state, params, profile.voice, sentences[0], trace))
    except NEMO_ERRORS as error:
        raise nemo_failure(error) from None
    if opened is None:
        return Response(status_code=CLIENT_CLOSED)
    voice, first = opened
    synthesis = _Synthesis(state.nemo, voice, params.response_format, params.speed, first, sentences[1:], trace)
    return synthesis.respond()


async def speech(request: Request) -> Response:
    """Fait dire le texte reçu ; chaque requête écrit une ligne dans le journal `dictee`.

    Toute exception imprévue devient ici l'erreur 500 de l'API, avant que Starlette la voie : sinon son middleware
    la relève après avoir répondu, et uvicorn en journalise le message, qui peut citer le texte à dire.
    """
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
    except Exception as error:
        trace.status = 500
        raise unexpected_error(error) from None
    finally:
        # Une réponse en continu écrit sa ligne à la fin du flux.
        if not streaming:
            trace.log()
