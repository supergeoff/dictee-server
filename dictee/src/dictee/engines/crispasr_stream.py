"""Dictée en direct par CrispASR : un processus `crispasr --stream` par session, précédé d'un ffmpeg.

ffmpeg ramène le PCM du client à du PCM16 mono 16 kHz et l'écrit directement sur l'entrée de CrispASR, par un tube que
dictee ne lit pas. CrispASR répond un objet JSON par ligne (protocole relevé sur la version 0.8.55) : `partial` porte
tout le texte révisé de la prise de parole en cours, `final` son texte redécodé après le silence qui la ferme, et
`silence` la fin d'un pas sans parole quand aucune prise de parole n'est ouverte. Ses temps sont ceux de l'audio reçu :
sans audio, il n'émet rien. La session traduit ces événements vers le rôle `LiveTranscriber` (voir
`CrispasrLiveSession`).

Rien n'est journalisé ici. La sortie d'erreur des deux processus est jetée, car elle peut citer le texte transcrit, et
les messages des exceptions ne reprennent jamais ce que les processus écrivent.
"""

import asyncio
import contextlib
import json
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Sequence

import anyio

from dictee.audio import SAMPLE_WIDTH, TRANSCRIPTION_RATE
from dictee.engines.ports import EngineOverloaded, EngineUnavailable
from dictee.modes import is_auto_language, to_iso_language
from dictee.types import Cleared, Committed, Final, Partial

# Pas de traitement de CrispASR, en millisecondes. Avec le pas par défaut (3000 ms), des textes finals perdaient leurs
# derniers mots ; avec 1000 ms, ils étaient complets (mesure de la tâche 3).
STREAM_STEP_MS = 1000
# Silence qui ferme une prise de parole, en millisecondes : CrispASR rend son `final` au pas qui suit ce silence.
FINAL_ON_SILENCE_MS = 800
# Silence écrit à la validation et au vidage, en secondes : celui qui ferme la prise de parole, deux pas entiers et une
# marge. Le dernier `silence` attendu par une validation tombe donc avant l'audio qui suit ce silence.
CLOSING_SILENCE_S = 3.0
# Un `silence` dont le temps atteint celui de la validation plus cette durée prouve que tout ce qui précède est fermé.
_CLOSED_AFTER_S = (FINAL_ON_SILENCE_MS + STREAM_STEP_MS) / 1000

# Ce qui n'est ni lettre ni chiffre : la ponctuation d'un mot change d'un texte partiel à l'autre.
_PUNCTUATION = re.compile(r"\W+")


def _words(text: str) -> list[tuple[str, str]]:
    """Les mots d'un texte, chacun avec sa forme comparable : sans ponctuation et sans casse ; un signe seul n'en est
    pas un."""
    pairs = ((word, _PUNCTUATION.sub("", word).casefold()) for word in text.split())
    return [(word, key) for word, key in pairs if key]


def _common_prefix(first: list[str], second: list[str]) -> int:
    count = 0
    for a, b in zip(first, second, strict=False):
        if a != b:
            break
        count += 1
    return count


class CrispasrLiveSession:
    """Une session de dictée en direct sur deux processus : ffmpeg, puis CrispASR.

    `send_audio` écrit le PCM du client sur l'entrée de ffmpeg. Les textes partiels sortent au fil de l'eau : un mot
    n'est émis qu'une fois vu à la même place dans deux événements `partial` successifs de la même prise de parole, et
    jamais retiré ensuite. Le premier mot d'une prise de parole qui suit une autre prise de parole depuis la dernière
    validation est précédé d'une espace.

    Les textes finals attendent la validation du client : CrispASR ferme seul une prise de parole à chaque pause, mais
    la route traite un `Final` reçu avant le commit comme une prise de parole fermée par le moteur. `commit` écrit donc
    `CLOSING_SILENCE_S` de silence et note le temps d'audio écrit ; au premier `silence` qui prouve que tout ce qui
    précède est fermé, la session émet les textes finals gardés, dans l'ordre, puis `Committed`. CrispASR ne sait pas
    annuler une prise de parole : `clear` écrit le même silence, jette les textes gardés depuis la dernière validation,
    émet `Cleared` aussitôt et ignore ensuite les événements des prises de parole commencées avant lui.

    `events` lève `EngineUnavailable` quand CrispASR s'arrête, et `send_audio` quand ffmpeg n'accepte plus l'audio.
    """

    def __init__(self, ffmpeg: asyncio.subprocess.Process, crispasr: asyncio.subprocess.Process, rate: int) -> None:
        self._ffmpeg, self._crispasr, self._rate = ffmpeg, crispasr, rate
        self._written = 0  # octets de PCM écrits sur l'entrée de ffmpeg, silences compris
        self._queue: asyncio.Queue[Partial | Final | Committed | Cleared | EngineUnavailable] = asyncio.Queue()
        self._commits: deque[float] = deque()  # pour chaque validation en attente, le temps du `silence` qui la clôt
        self._last_commit = 0.0  # temps d'audio de la dernière validation
        self._dropped: list[tuple[float, float]] = []  # intervalles de début des prises de parole vidées
        self._finals: list[tuple[float, str]] = []  # textes finals gardés jusqu'à la validation, avec leur début
        self._utterance: object = None  # la prise de parole dont `_previous` et `_emitted` suivent le texte
        self._previous: list[str] = []  # formes comparables du dernier texte partiel reçu
        self._emitted = 0  # mots déjà émis de la prise de parole en cours
        self._spoken = False  # un mot est sorti depuis la dernière validation
        self._reader = asyncio.create_task(self._read())

    def _seconds(self) -> float:
        return self._written / (SAMPLE_WIDTH * self._rate)

    async def _write(self, pcm: bytes) -> None:
        try:
            self._ffmpeg.stdin.write(pcm)
            await self._ffmpeg.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise EngineUnavailable("the live transcription process stopped") from exc
        self._written += len(pcm)

    async def _write_closing_silence(self) -> None:
        await self._write(bytes(SAMPLE_WIDTH * round(CLOSING_SILENCE_S * self._rate)))

    async def send_audio(self, pcm: bytes) -> None:
        await self._write(pcm)

    async def commit(self) -> None:
        # L'attente est notée avant l'écriture : CrispASR ne peut pas émettre le `silence` attendu avant ce silence.
        self._commits.append(self._seconds() + _CLOSED_AFTER_S)
        self._last_commit = self._seconds()
        await self._write_closing_silence()

    async def clear(self) -> None:
        now = self._seconds()
        self._dropped.append((self._last_commit, now))
        self._finals = [(start, text) for start, text in self._finals if start < self._last_commit]
        self._utterance, self._previous, self._emitted, self._spoken = None, [], 0, False
        self._queue.put_nowait(Cleared())
        await self._write_closing_silence()

    async def events(self) -> AsyncIterator[Partial | Final | Committed | Cleared]:
        while True:
            event = await self._queue.get()
            if isinstance(event, EngineUnavailable):
                raise event
            yield event

    async def _read(self) -> None:
        """Lit la sortie de CrispASR jusqu'à sa fin, qui signale l'arrêt du processus."""
        while line := await self._crispasr.stdout.readline():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._handle(event)
        self._queue.put_nowait(EngineUnavailable("the live transcription process stopped"))

    def _handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "silence":
            t = event.get("t")
            while self._commits and isinstance(t, int | float) and t >= self._commits[0]:
                self._commits.popleft()
                for _, text in self._finals:
                    self._queue.put_nowait(Final(text, ()))
                self._finals = []
                self._utterance, self._previous, self._emitted, self._spoken = None, [], 0, False
                self._queue.put_nowait(Committed())
            return
        start, text = event.get("t0"), event.get("text")
        if not isinstance(start, int | float) or not isinstance(text, str):
            return
        if any(low <= start < high for low, high in self._dropped):
            return
        if kind == "partial":
            self._partial(event.get("utterance_id"), text)
        elif kind == "final":
            self._finals.append((start, text.strip()))
            self._utterance, self._previous, self._emitted = None, [], 0

    def _partial(self, utterance: object, text: str) -> None:
        """Émet les mots nouveaux de `text` que l'événement précédent de la même prise de parole confirme."""
        words = _words(text)
        keys = [key for _, key in words]
        if utterance != self._utterance:
            self._utterance, self._previous, self._emitted = utterance, [], 0
        stable = _common_prefix(self._previous, keys)
        self._previous = keys
        if stable <= self._emitted:
            return
        delta = " ".join(word for word, _ in words[self._emitted : stable])
        if self._emitted or self._spoken:
            delta = " " + delta
        self._emitted = stable
        self._spoken = True
        self._queue.put_nowait(Partial(delta))

    async def close(self) -> None:
        """Tue les deux processus, arrête la lecture et attend la fin des processus et de leurs tubes."""
        for process in (self._ffmpeg, self._crispasr):
            if process.returncode is None:
                process.kill()
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        self._ffmpeg.stdin.close()
        while await self._crispasr.stdout.read(65536):
            pass
        await asyncio.gather(self._ffmpeg.wait(), self._crispasr.wait())


async def _kill(*processes: asyncio.subprocess.Process) -> None:
    for process in processes:
        if process.returncode is None:
            process.kill()
    await asyncio.gather(*(process.wait() for process in processes))


class CrispasrLive:
    """Le rôle `LiveTranscriber` joué par un processus CrispASR par session, au plus `max_sessions` à la fois.

    CrispASR ferme seul les prises de parole, mais la session garde leurs textes finals jusqu'à la validation du client
    (`server_vad` faux) ; elle accepte plusieurs validations (`many_commits`).
    """

    server_vad = False
    many_commits = True

    def __init__(
        self, command: str, model_path: str, *, max_sessions: int, threads: int, stream_args: Sequence[str] = ()
    ) -> None:
        self._command, self._model_path = command, model_path
        self._max_sessions, self._threads = max_sessions, threads
        self._stream_args = tuple(stream_args)
        self._open = 0

    def _arguments(self, language: str) -> list[str]:
        iso = "auto" if is_auto_language(language) else to_iso_language(language)
        return [
            self._command,
            "--stream",
            "--stream-json",
            "--vad",
            "-m",
            self._model_path,
            "-l",
            iso,
            "-t",
            str(self._threads),
            "--stream-step",
            str(STREAM_STEP_MS),
            "--stream-final-on-silence-ms",
            str(FINAL_ON_SILENCE_MS),
            *self._stream_args,
        ]

    @contextlib.asynccontextmanager
    async def live(
        self,
        *,
        sample_rate: int,
        language: str,
        phrases: Sequence[str],
        boost: float,
        endpointing_ms: int | None = None,
    ) -> AsyncIterator[CrispasrLiveSession]:
        """Lance ffmpeg et CrispASR pour une session, et les tue à la sortie du bloc `async with`, quelle qu'elle soit.

        `language` est le code de `dictee.modes.to_nemo_language` (`fr-FR` ou `auto`), passé à CrispASR en code ISO.
        CrispASR n'a pas de glossaire en direct : `phrases` et `boost` sont ignorés, comme `endpointing_ms`, que la
        route ne donne pas quand `server_vad` est faux. Lève `EngineOverloaded` au-delà de `max_sessions` sessions
        ouvertes, et `EngineUnavailable` quand ffmpeg ou CrispASR ne se lancent pas.
        """
        if self._open >= self._max_sessions:
            raise EngineOverloaded("all live transcription sessions are in use")
        self._open += 1
        try:
            async with self._session(sample_rate, language) as session:
                yield session
        finally:
            self._open -= 1

    @contextlib.asynccontextmanager
    async def _session(self, rate: int, language: str) -> AsyncIterator[CrispasrLiveSession]:
        read_end, write_end = os.pipe()
        ffmpeg = crispasr = None
        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                # Le format de l'entrée est donné en entier : sans ces options, ffmpeg sonderait l'entrée d'abord.
                # `-fflags nobuffer` n'y est pas : avec lui, ffmpeg perd le début de l'audio reçu par le tube.
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-f",
                "s16le",
                "-ar",
                str(rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ar",
                str(TRANSCRIPTION_RATE),
                "-ac",
                "1",
                "-flush_packets",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=write_end,
                stderr=asyncio.subprocess.DEVNULL,
            )
            crispasr = await asyncio.create_subprocess_exec(
                *self._arguments(language),
                stdin=read_end,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except BaseException as exc:
            # Un lancement qui échoue ou qu'une annulation interrompt ne laisse aucun processus derrière lui.
            with anyio.CancelScope(shield=True):
                await _kill(*(process for process in (ffmpeg, crispasr) if process is not None))
            if isinstance(exc, OSError):
                raise EngineUnavailable("the live transcription process could not start") from exc
            raise
        finally:
            # Les deux bouts du tube appartiennent désormais aux processus : dictee n'en garde aucun.
            os.close(read_end)
            os.close(write_end)
        session = CrispasrLiveSession(ffmpeg, crispasr, rate)
        try:
            yield session
        finally:
            with anyio.CancelScope(shield=True):
                await session.close()
