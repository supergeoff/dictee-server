"""Faux NeMo et faux LiteLLM des tests de routes : aucun réseau, aucun modèle."""

import asyncio
import io
import json
import wave
from collections.abc import Sequence
from dataclasses import replace

import httpx

from dictee.types import Final, Partial, Word

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


def _wav_info(source: str | io.BytesIO) -> tuple[int, float]:
    """Cadence et durée en secondes d'un WAV : le chemin d'un fichier écrit sur le disque, ou ses octets en mémoire."""
    with wave.open(source) as wav:
        return wav.getframerate(), wav.getnframes() / wav.getframerate()


class FakeNemo:
    """Faux `NemoClient` : mêmes méthodes, rien d'autre.

    `finals` sont rendus par `transcribe` (avec des `Partial` mêlés aux `Final`, ils ne sortent que si l'appel passe
    `partials=True`), `pcm` par `speak`. Les mots de `finals` portent les personnes brutes de la session ; comme
    NeMo, `transcribe` les retire quand l'appel ne passe pas `speakers=True`. `error`, s'il est donné, est levée par
    `transcribe`, `speak` et `voices`. `release` (un `anyio.Event`) retient la première transcription, de `transcribe`
    ou de `transcribe_chunk` selon celle qui arrive d'abord : elle n'a de résultat à rendre qu'une fois l'événement
    posé, ce qui laisse les transcriptions suivantes passer sans attendre. `after` est le nombre d'éléments
    (`Partial` demandés ou `Final`) que `transcribe` rend avant de lever `error` ou d'attendre `release` : avec 1, une
    réponse en continu a déjà son premier delta quand la panne ou l'attente arrive.

    `chunks` sont rendus tour à tour par `transcribe_chunk` : un `Final` est rendu, une exception est levée, et le
    dernier élément se répète une fois la liste épuisée. Par défaut, `chunks` ne contient que `CHUNK`. `error` ne
    touche pas `transcribe_chunk` : une panne s'y donne par une exception dans `chunks`. De même, `ready` rend tour à
    tour les éléments de `ready_answers` (par défaut `[True]`, le dernier se répète) et ne lève jamais `error`.

    Chaque appel est noté dans `calls`, sous forme de dict : `language`, `phrases`, `boost`, `speakers`, `partials`,
    `wav_rate` et `seconds` (lus dans le fichier WAV) pour `transcribe` ; `chunk` (toujours vrai), `language`,
    `phrases`, `boost` et `seconds` (lus dans les octets du WAV) pour `transcribe_chunk` ; `text`, `voice` et
    `language` pour `speak` ; `voices` pour `voices` ; `ready` pour `ready`. `transcribe` appelle
    `on_progress(seconds)` avant son premier événement. `sessions_closed` compte les sessions de transcription
    terminées, de quelque façon que ce soit : `NemoClient` ferme son socket dans un `finally` qui attend, et ce
    nettoyage-ci attend de même.
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
    ) -> None:
        self.finals = tuple(finals)
        self.pcm = pcm
        self.error = error
        self.release = release
        self.after = after
        self.chunks = (CHUNK,) if chunks is None else tuple(chunks)
        self.ready_answers = (True,) if ready_answers is None else tuple(ready_answers)
        self.calls: list[dict] = []
        self.sessions_closed = 0

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
        """Attend l'événement `release`, que seule la première transcription (de l'une ou l'autre sorte) prend."""
        release, self.release = self.release, None
        if release is not None:
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
            if self.error is not None:
                raise self.error
            await self._hold()
            for event in events[self.after :]:
                yield event
        finally:
            await asyncio.sleep(0)
            self.sessions_closed += 1

    async def transcribe_chunk(self, wav: bytes, *, language, phrases, boost) -> Final:
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
