"""Faux NeMo et faux LiteLLM des tests de routes : aucun réseau, aucun modèle."""

import asyncio
import contextlib
import io
import json
import wave
from collections.abc import Callable, Sequence
from dataclasses import replace

import httpx

from dictee.types import Cleared, Committed, Final, Partial, Word

# Deux phrases finales de deux personnes, que la session temps réel de NeMo numérote « 0 » puis « 1 » : le texte
# attendu est « Bonjour à tous. Salut. ».
FINALS = (
    Final(
        "Bonjour à tous.",
        (Word("Bonjour", 0.0, 0.4, "0"), Word("à", 0.5, 0.6, "0"), Word("tous.", 0.6, 1.2, "0")),
    ),
    Final("Salut.", (Word("Salut.", 2.0, 2.5, "1"),)),
)

# Le morceau que `transcribe_chunk` rend par défaut : le texte et les quatre mots de `FINALS`, que la route REST de
# NeMo rend sans personne.
CHUNK = Final(
    "Bonjour à tous. Salut.",
    tuple(replace(word, speaker=None) for final in FINALS for word in final.words),
)

# Voix de Magpie, comme la route `/v1/models` de NeMo les liste.
VOICES = ["John", "Sofia", "Aria", "Jason", "Leo"]

# Les prises de parole que la dictée en direct reçoit par défaut : chacune un texte partiel, puis la phrase finale de
# `FINALS` qui lui correspond.
LIVE_TURNS = ((Partial("Bonjour"), FINALS[0]), (Partial("Salut"), FINALS[1]))


def _wav_info(source: str | io.BytesIO) -> tuple[int, float]:
    """Cadence et durée en secondes d'un WAV : le chemin d'un fichier écrit sur le disque, ou ses octets en mémoire."""
    with wave.open(source) as wav:
        return wav.getframerate(), wav.getnframes() / wav.getframerate()


class FakeLiveSession:
    """Fausse `LiveSession` que `FakeNemo.live` ouvre : elle note ce qu'elle reçoit et rejoue `live_turns`.

    `options` garde les arguments de `live`, `audio` les octets reçus bout à bout, `commits` et `clears` le nombre de
    commits et d'effacements, `closed` la fermeture de la session. Les prises de parole de `nemo.live_turns` se suivent
    d'une session à l'autre : les `Partial` de la prise de parole en cours partent à son premier `send_audio`, son
    `Final` puis un `Committed` à son commit, et la suivante commence. Avec `endpointing_ms`, comme NeMo qui ferme seul
    une phrase après un silence, le `Final` part aussitôt après les `Partial`, sans `Committed`, et la prise de parole
    suivante commence au prochain `send_audio`. Un effacement rend un `Cleared` et laisse la même prise de parole
    recommencer. `nemo.live_error`, s'il est donné, est levée par `events`.
    """

    def __init__(self, nemo: "FakeNemo", options: dict) -> None:
        self.nemo = nemo
        self.options = options
        self.audio = bytearray()
        self.commits = 0
        self.clears = 0
        self.closed = False
        self._events: asyncio.Queue = asyncio.Queue()
        self._started = False  # les `Partial` de la prise de parole en cours sont déjà partis

    def _put(self, kind: type) -> None:
        for event in self.nemo.live_turn():
            if isinstance(event, kind):
                self._events.put_nowait(event)

    async def send_audio(self, pcm: bytes) -> None:
        self.audio += pcm
        if self._started:
            return
        self._started = True
        self._put(Partial)
        if self.options["endpointing_ms"] is not None:
            self._put(Final)
            self.nemo.next_live_turn()
            self._started = False

    async def commit(self) -> None:
        self.commits += 1
        self._put(Final)
        self._events.put_nowait(Committed())
        self.nemo.next_live_turn()
        self._started = False

    async def clear(self) -> None:
        self.clears += 1
        self._events.put_nowait(Cleared())
        self._started = False

    async def events(self):
        if self.nemo.live_error is not None:
            raise self.nemo.live_error
        while True:
            yield await self._events.get()


class FakeNemo:
    """Faux `NemoClient` : mêmes méthodes, rien d'autre.

    `finals` sont rendus par `transcribe` (avec des `Partial` mêlés aux `Final`, ils ne sortent que si l'appel passe
    `partials=True`), `pcm` par `speak`. Les mots de `finals` portent les personnes brutes de la session ; comme
    NeMo, `transcribe` les retire quand l'appel ne passe pas `speakers=True`. `error`, s'il est donné, est levée par
    `transcribe`, `speak` et `voices`. `release` (un `anyio.Event`) retient la première transcription qui arrive après
    lui, de `transcribe` ou de `transcribe_chunk` : elle n'a de résultat à rendre qu'une fois l'événement posé, ce qui
    laisse les transcriptions suivantes passer sans attendre. L'événement reste dans `release`, pour que le test le
    pose quand il veut, même après que la transcription l'a pris ; un nouvel événement placé dans `release` retient de
    même la transcription qui le suit. `after` est le nombre d'éléments
    (`Partial` demandés ou `Final`) que `transcribe` rend avant de lever `error` ou d'attendre `release` : avec 1, une
    réponse en continu a déjà son premier delta quand la panne ou l'attente arrive.

    `chunks` sont rendus tour à tour par `transcribe_chunk` : un `Final` est rendu, une exception est levée, et le
    dernier élément se répète une fois la liste épuisée. Par défaut, `chunks` ne contient que `CHUNK`. `error` ne
    touche pas `transcribe_chunk` : une panne s'y donne par une exception dans `chunks`. `on_chunk`, s'il est donné,
    est appelé sans argument au début de chaque `transcribe_chunk`, avant tout le reste. De même, `ready` rend tour à
    tour les éléments de `ready_answers` (par défaut `[True]`, le dernier se répète) et ne lève jamais `error`.

    `transcribe_errors` donne une panne par appel de `transcribe`, dans l'ordre : la panne du rang de l'appel est
    levée (`None` pour un appel sans panne), au même endroit que `error` (après les `after` premiers éléments). Une
    fois la liste épuisée, c'est `error` qui vaut, comme avant : sans `transcribe_errors`, rien ne change. Un
    élément `None` l'emporte sur `error` pour son appel.

    Chaque appel est noté dans `calls`, sous forme de dict : `language`, `phrases`, `boost`, `speakers`, `partials`,
    `wav_rate` et `seconds` (lus dans le fichier WAV) pour `transcribe` ; `chunk` (toujours vrai), `language`,
    `phrases`, `boost` et `seconds` (lus dans les octets du WAV) pour `transcribe_chunk` ; `text`, `voice` et
    `language` pour `speak` ; `voices` pour `voices` ; `ready` pour `ready`. `transcribe` appelle
    `on_progress(seconds)` avant son premier événement. `sessions_closed` compte les sessions de transcription
    terminées, de quelque façon que ce soit : `NemoClient` ferme son socket dans un `finally` qui attend, et ce
    nettoyage-ci attend de même.

    `live` ouvre une `FakeLiveSession`, ajoutée à `live_sessions`. `live_turns` donne les prises de parole de la dictée
    en direct, chacune une suite de `Partial` et de `Final` (par défaut `LIVE_TURNS`) ; la dernière se répète une fois
    la liste épuisée. `live_error`, s'il est donné, est levée par `events` de chaque session ; `error` ne touche pas
    la dictée en direct.
    """

    def __init__(
        self,
        *,
        finals=(),
        pcm=b"",
        error=None,
        release=None,
        after: int = 0,
        chunks: Sequence[Final | Exception] | None = None,
        ready_answers: Sequence[bool] | None = None,
        transcribe_errors: Sequence[Exception | None] = (),
        on_chunk: Callable[[], None] | None = None,
        live_turns: Sequence[Sequence[Partial | Final]] = LIVE_TURNS,
        live_error: Exception | None = None,
    ) -> None:
        self.finals = tuple(finals)
        self.pcm = pcm
        self.error = error
        self.release = release
        self.after = after
        self.chunks = (CHUNK,) if chunks is None else tuple(chunks)
        self.ready_answers = (True,) if ready_answers is None else tuple(ready_answers)
        self.transcribe_errors = list(transcribe_errors)
        self.on_chunk = on_chunk
        self.live_turns = [list(turn) for turn in live_turns]
        self.live_error = live_error
        self.live_sessions: list[FakeLiveSession] = []
        self.calls: list[dict] = []
        self.sessions_closed = 0
        self._taken = None  # le dernier événement de `release` qu'une transcription a pris
        self._live_turn = 0  # rang de la prise de parole en cours dans `live_turns`

    def live_turn(self) -> list[Partial | Final]:
        """La prise de parole en cours de la dictée en direct ; la dernière se répète."""
        return self.live_turns[min(self._live_turn, len(self.live_turns) - 1)]

    def next_live_turn(self) -> None:
        self._live_turn += 1

    @contextlib.asynccontextmanager
    async def live(self, *, sample_rate, language, phrases, boost, endpointing_ms=None):
        options = {
            "sample_rate": sample_rate,
            "language": language,
            "phrases": list(phrases),
            "boost": boost,
            "endpointing_ms": endpointing_ms,
        }
        session = FakeLiveSession(self, options)
        self.live_sessions.append(session)
        try:
            yield session
        finally:
            session.closed = True

    @property
    def transcribe_calls(self) -> list[dict]:
        return [call for call in self.calls if "speakers" in call]

    @property
    def speak_calls(self) -> list[dict]:
        return [call for call in self.calls if "text" in call]

    @property
    def voices_calls(self) -> list[dict]:
        return [call for call in self.calls if "voices" in call]

    @property
    def chunk_calls(self) -> list[dict]:
        return [call for call in self.calls if "chunk" in call]

    @property
    def ready_calls(self) -> list[dict]:
        return [call for call in self.calls if "ready" in call]

    async def _hold(self) -> None:
        """Attend `release`, que seule la première transcription qui le trouve prend ; il reste là pour le test."""
        release = self.release
        if release is not None and release is not self._taken:
            self._taken = release
            await release.wait()

    async def transcribe(self, wav, *, language, phrases, boost, speakers=False, partials=False, on_progress=None):
        rate, seconds = _wav_info(str(wav))
        self.calls.append(
            {
                "language": language,
                "phrases": list(phrases),
                "boost": boost,
                "speakers": speakers,
                "partials": partials,
                "wav_rate": rate,
                "seconds": seconds,
            }
        )
        # La panne de cet appel est celle de son rang dans `transcribe_errors` tant qu'il en reste, sinon `error`.
        turn = len(self.transcribe_calls) - 1
        error = self.transcribe_errors[turn] if turn < len(self.transcribe_errors) else self.error
        if on_progress is not None:
            on_progress(seconds)
        # Comme `NemoClient`, les `Partial` ne sortent que si on les demande, les personnes que si on les demande aussi.
        events = [event for event in self.finals if partials or not isinstance(event, Partial)]
        if not speakers:
            events = [
                replace(event, words=tuple(replace(word, speaker=None) for word in event.words))
                if isinstance(event, Final)
                else event
                for event in events
            ]
        try:
            for event in events[: self.after]:
                yield event
            if error is not None:
                raise error
            await self._hold()
            for event in events[self.after :]:
                yield event
        finally:
            await asyncio.sleep(0)
            self.sessions_closed += 1

    async def transcribe_chunk(self, wav: bytes, *, language, phrases, boost) -> Final:
        if self.on_chunk is not None:
            self.on_chunk()
        _, seconds = _wav_info(io.BytesIO(wav))
        self.calls.append(
            {"chunk": True, "language": language, "phrases": list(phrases), "boost": boost, "seconds": seconds}
        )
        # Le tour de cet appel se règle avant l'attente de `release` : l'appel retenu garde le premier élément.
        chunk = self.chunks[min(len(self.chunk_calls), len(self.chunks)) - 1]
        await self._hold()
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    async def ready(self) -> bool:
        self.calls.append({"ready": True})
        return self.ready_answers[min(len(self.ready_calls), len(self.ready_answers)) - 1]

    async def speak(self, text: str, *, voice: str, language: str) -> bytes:
        self.calls.append({"text": text, "voice": voice, "language": language})
        if self.error is not None:
            raise self.error
        return self.pcm

    async def voices(self) -> list[str]:
        self.calls.append({"voices": True})
        if self.error is not None:
            raise self.error
        return list(VOICES)


class FakeLlm:
    """Faux LiteLLM : répond `reply` à chaque requête de chat et note les corps reçus dans `calls`.

    `http` est le client à donner à l'app.
    """

    def __init__(self, reply: str = "Bonjour à tous, salut.") -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(request.content))
        body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply}}]}
        return httpx.Response(200, json=body)
