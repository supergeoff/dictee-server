"""Client de NeMo-Speech.cpp : transcription en continu, diarisation, synthèse et liste des voix.

Rien n'est journalisé ici : ni le texte transcrit, ni le texte à dire, ni l'audio. Les messages des exceptions ne
reprennent que ce que NeMo dit de son propre fonctionnement. La bibliothèque `websockets` journalise elle aussi, mais
elle est réduite au silence en DEBUG (voir `_WEBSOCKETS_LOGGER`).
"""

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Buffer

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from dictee.audio import SAMPLE_WIDTH, TRANSCRIPTION_RATE, wav_pcm
from dictee.types import Final, Partial, Turn, Word

# Fréquence native de la synthèse de NeMo (mesurée en tâche 1) : `speak` rend du PCM16 mono à cette fréquence.
TTS_SAMPLE_RATE = 22050

# Une trame binaire du temps réel pèse une seconde de PCM16 mono à 16 kHz.
FRAME_BYTES = TRANSCRIPTION_RATE * SAMPLE_WIDTH

# Délais en secondes. Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe le sien.
OPEN_TIMEOUT = 10.0  # ouverture d'une connexion, WebSocket ou HTTP
EVENT_TIMEOUT = 300.0  # silence maximal entre deux événements de NeMo pendant une session temps réel
# Attente de la réponse de NeMo à la fermeture d'une session. Une session abandonnée pendant l'envoi de l'audio n'y
# répond pas : NeMo ne lit la fermeture qu'après l'audio déjà envoyé (10,02 s d'attente avec le délai par défaut
# de `websockets`, mesuré sur 184 s d'audio), et la place de la transcription longue reste prise pendant ce temps.
CLOSE_TIMEOUT = 1.0
DIARIZE_TIMEOUT = 1200.0
SPEECH_TIMEOUT = 120.0
MODELS_TIMEOUT = 10.0
# Taille des morceaux d'un WAV envoyé à la diarisation : la mémoire de l'envoi ne dépend pas de la durée de l'audio.
UPLOAD_CHUNK_SIZE = 64 * 1024

_DELTA = "conversation.item.input_audio_transcription.delta"
_COMPLETED = "conversation.item.input_audio_transcription.completed"
_COMMITTED = "input_audio_buffer.committed"

# À son niveau DEBUG, `websockets` écrit chaque en-tête de la poignée de main (dont la clé d'API) et un extrait de
# chaque trame (dont la session, qui porte le glossaire et la consigne). Il lit ce niveau à la création de la
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


async def _next_event(ws: ClientConnection) -> dict:
    """Prochain événement de NeMo ; un événement `error` lève l'exception qui lui correspond."""
    try:
        async with asyncio.timeout(EVENT_TIMEOUT):
            raw = await ws.recv()
    except TimeoutError:
        raise NemoError(f"no event from NeMo for {EVENT_TIMEOUT:g} seconds") from None
    event = json.loads(raw)
    if event.get("type") == "error":
        raise _event_error(event)
    return event


async def _expect(ws: ClientConnection, kind: str) -> None:
    event = await _next_event(ws)
    if event.get("type") != kind:
        raise NemoError(f"expected the event {kind}, NeMo sent {event.get('type')}")


async def _send_audio(ws: ClientConnection, pcm: memoryview) -> None:
    """Envoie le PCM par trames d'une seconde, puis le commit.

    NeMo lit les trames à la vitesse de son traitement : `send` ne rend la main qu'à ce rythme, d'où une tâche à
    part qui laisse la lecture des événements avancer pendant l'envoi.
    """
    for start in range(0, len(pcm), FRAME_BYTES):
        await ws.send(pcm[start : start + FRAME_BYTES])
    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))


def _final(event: dict) -> Final:
    words = tuple(
        Word(text=word["word"], start=float(word["start"]), end=float(word["end"])) for word in event.get("words") or ()
    )
    return Final(text=event["transcript"], words=words)


async def _blocks(head: bytes, wav: memoryview, tail: bytes) -> AsyncIterator[bytes]:
    """Le corps d'un envoi : `head`, le WAV par morceaux de `UPLOAD_CHUNK_SIZE` octets, puis `tail`."""
    yield head
    for start in range(0, len(wav), UPLOAD_CHUNK_SIZE):
        yield bytes(wav[start : start + UPLOAD_CHUNK_SIZE])
    yield tail


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return "no error message"


class NemoClient:
    """Les quatre appels de dictee à NeMo ; `http` est le client HTTP partagé de l'app."""

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
        wav: Buffer,
        *,
        language: str,
        phrases: list[str],
        boost: float,
        prompt: str | None,
        partials: bool = False,
    ) -> AsyncIterator[Final | Partial]:
        """Transcrit un WAV PCM16 mono 16 kHz par une session temps réel et rend un `Final` par phrase finale.

        Avec `partials=True`, chaque événement `delta` de NeMo non vide rend aussi un `Partial`, dans l'ordre d'arrivée
        et avant le `Final` de son item : NeMo en émet un tous les 1,12 s d'audio traité, puis un seul `completed` au
        commit. Sans `partials`, seuls les `Final` sortent.

        La session finit sur le premier `input_audio_buffer.committed` reçu après le commit : NeMo laisse alors le
        socket ouvert et c'est le client qui le ferme. La session se ferme aussi quand la tâche qui attend un
        événement est annulée. Un consommateur qui s'arrête avant la fin ferme le générateur
        (`contextlib.aclosing`) : c'est à ce moment que la session se ferme.

        Lève `NemoUnavailable` si NeMo est injoignable ou coupe la connexion, `NemoOverloaded` si sa file est pleine
        et `NemoError` pour toute autre erreur, y compris plus de `EVENT_TIMEOUT` secondes sans événement.
        """
        pcm = wav_pcm(wav)
        session: dict[str, object] = {"sample_rate": TRANSCRIPTION_RATE, "language": language, "word_timestamps": True}
        if phrases:
            session["speech_contexts"] = [{"phrases": phrases, "boost": boost}]
        if prompt:
            session["prompt"] = prompt

        ws = await self._open()
        sender: asyncio.Task | None = None
        try:
            await _expect(ws, "session.created")
            await ws.send(json.dumps({"type": "session.update", "session": session}))
            await _expect(ws, "session.updated")
            sender = asyncio.create_task(_send_audio(ws, pcm))
            while True:
                event = await _next_event(ws)
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

    async def _request(
        self, method: str, path: str, *, timeout: float, headers: dict[str, str] | None = None, **kwargs
    ) -> httpx.Response:
        """Appel d'une route HTTP de NeMo : 429 donne `NemoOverloaded`, un échec de transport `NemoUnavailable`.

        `headers` s'ajoute à l'en-tête d'authentification.
        """
        try:
            response = await self._http.request(
                method,
                self._base_url + path,
                headers={**self._headers, **(headers or {})},
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

    async def diarize(self, wav: Buffer) -> list[Turn]:
        """Tours de parole d'un WAV, en mode `streaming`, adapté aux longs fichiers.

        Le formulaire multipart est écrit ici et non par `httpx` : celui-ci n'accepte pas un `bytearray` (le décodage
        de `to_wav16k`) et en ferait une copie en `bytes`, soit 110 Mio de plus pour une heure d'audio, pendant que
        dictee en garde déjà un exemplaire. Le WAV part en morceaux de `UPLOAD_CHUNK_SIZE` octets pris dans une vue
        du tampon, sans autre copie que celle du morceau en cours. Le corps annonce sa taille exacte
        (`Content-Length`) au lieu de passer par l'encodage `chunked`.
        """
        boundary = secrets.token_hex(16)
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="mode"\r\n\r\nstreaming\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        view = memoryview(wav).cast("B")
        response = await self._request(
            "POST",
            "/v1/audio/diarizations",
            timeout=DIARIZE_TIMEOUT,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head) + len(view) + len(tail)),
            },
            content=_blocks(head, view, tail),
        )
        return [
            Turn(start=float(segment["start"]), end=float(segment["end"]), speaker=int(segment["speaker"]))
            for segment in response.json()["segments"]
        ]

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
