"""Faux NeMo et faux LiteLLM des tests de routes : aucun réseau, aucun modèle."""

import asyncio
import json
import struct

import httpx

from dictee.types import Final, Turn, Word

# Deux phrases finales de deux locuteurs : le texte attendu est « Bonjour à tous. Salut. ».
FINALS = (
    Final("Bonjour à tous.", (Word("Bonjour", 0.0, 0.4), Word("à", 0.5, 0.6), Word("tous.", 0.6, 1.2))),
    Final("Salut.", (Word("Salut.", 2.0, 2.5),)),
)
TURNS = [Turn(0.0, 1.3, 1), Turn(1.8, 2.6, 2)]

# Voix de Magpie, comme la route `/v1/models` de NeMo les liste.
VOICES = ["John", "Sofia", "Aria", "Jason", "Leo"]

# Cadence d'un WAV canonique (en-tête de 44 octets) : les octets 24 à 27.
_RATE_OFFSET = 24


def _wav_rate(wav) -> int:
    return struct.unpack_from("<I", wav, _RATE_OFFSET)[0]


class FakeNemo:
    """Faux `NemoClient` : mêmes quatre méthodes, rien d'autre.

    `finals` sont rendus par `transcribe`, `turns` par `diarize`, `pcm` par `speak`. `error`, s'il est donné, est levée
    par chacune des quatre méthodes. `release` (un `anyio.Event`) retient la première transcription : elle n'a de
    `Final` à rendre qu'une fois l'événement posé, ce qui laisse les transcriptions suivantes passer sans attendre.

    Chaque appel est noté dans `calls`, sous forme de dict : `language`, `phrases`, `boost`, `prompt` et `wav_rate`
    pour `transcribe` ; `diarize` et `wav_rate` pour `diarize` ; `text`, `voice` et `language` pour `speak` ; `voices`
    pour `voices`. `sessions_closed` compte les sessions de transcription terminées, de quelque façon que ce soit :
    `NemoClient` ferme son socket dans un `finally` qui attend, et ce nettoyage-ci attend de même.
    """

    def __init__(self, *, finals=(), turns=(), pcm=b"", error=None, release=None) -> None:
        self.finals = tuple(finals)
        self.turns = list(turns)
        self.pcm = pcm
        self.error = error
        self.release = release
        self.calls: list[dict] = []
        self.sessions_closed = 0

    @property
    def diarize_calls(self) -> list[dict]:
        return [call for call in self.calls if "diarize" in call]

    async def transcribe(self, wav, *, language, phrases, boost, prompt):
        call = {"language": language, "phrases": list(phrases), "boost": boost, "prompt": prompt}
        self.calls.append({**call, "wav_rate": _wav_rate(wav)})
        try:
            if self.error is not None:
                raise self.error
            release, self.release = self.release, None
            if release is not None:
                await release.wait()
            for final in self.finals:
                yield final
        finally:
            await asyncio.sleep(0)
            self.sessions_closed += 1

    async def diarize(self, wav) -> list[Turn]:
        self.calls.append({"diarize": True, "wav_rate": _wav_rate(wav)})
        if self.error is not None:
            raise self.error
        return list(self.turns)

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
