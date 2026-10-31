"""Conversion audio par ffmpeg : décodage des envois et encodage de la synthèse."""

import asyncio
import io
import wave
from collections.abc import AsyncIterator
from pathlib import Path

# NeMo transcrit du PCM16 mono à 16 kHz.
TRANSCRIPTION_RATE = 16000
# Fréquence du format `pcm` d'OpenAI : tous les formats de sortie partent de ce rééchantillonnage.
SPEECH_RATE = 24000
# Taille des lectures sur la sortie de ffmpeg : de petits blocs, pour que le client reçoive le son au fil de l'eau.
READ_SIZE = 4096
# Fin de la sortie d'erreur de ffmpeg conservée pour les messages.
STDERR_TAIL = 300

# atempo accepte un facteur entre 0,5 et 2 par étage ; la chaîne en enchaîne deux pour couvrir [0,25 ; 4].
MIN_SPEED = 0.25
MAX_SPEED = 4.0

OUTPUT_FORMATS: dict[str, str] = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}

# Codec et conteneur de chaque format, en options de sortie de ffmpeg.
_ENCODERS: dict[str, tuple[str, ...]] = {
    "mp3": ("-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"),
    "opus": ("-c:a", "libopus", "-b:a", "32k", "-f", "ogg"),
    "aac": ("-c:a", "aac", "-b:a", "64k", "-f", "adts"),
    "flac": ("-c:a", "flac", "-f", "flac"),
    "wav": ("-c:a", "pcm_s16le", "-f", "wav"),
    "pcm": ("-c:a", "pcm_s16le", "-f", "s16le"),
}


class AudioDecodeError(Exception):
    """Le fichier n'est pas un audio que ffmpeg sait lire, ou il ne contient aucun échantillon."""


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()


def _tail(stderr: bytes) -> str:
    return stderr.decode(errors="replace").strip()[-STDERR_TAIL:]


async def to_wav16k(src: Path) -> bytes:
    """Décode n'importe quel audio en WAV PCM16 mono 16 kHz.

    ffmpeg lit le chemin et non un tube : un m4a d'Android range souvent son index en fin de fichier, que ffmpeg
    ne peut atteindre qu'en se déplaçant dans le fichier. Il rend du PCM brut, et l'en-tête WAV vient du module `wave`.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        str(TRANSCRIPTION_RATE),
        "-f",
        "s16le",
        "pipe:1",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        pcm, stderr = await process.communicate()
    except BaseException:
        # Annulation de l'appelant : ffmpeg ne doit pas finir de décoder pour personne.
        _kill(process)
        await process.wait()
        raise
    if process.returncode != 0:
        raise AudioDecodeError(f"ffmpeg could not decode the file (exit {process.returncode}): {_tail(stderr)}")
    if not pcm:
        raise AudioDecodeError("the file contains no audio samples")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TRANSCRIPTION_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_duration(wav: bytes) -> float:
    """Durée en secondes d'un WAV."""
    with wave.open(io.BytesIO(wav)) as reader:
        return reader.getnframes() / reader.getframerate()


def wav_pcm(wav: bytes) -> bytes:
    """Les échantillons d'un WAV, sans l'en-tête."""
    with wave.open(io.BytesIO(wav)) as reader:
        return reader.readframes(reader.getnframes())


def atempo_chain(speed: float) -> str:
    """Filtre ffmpeg qui change la vitesse sans changer la hauteur ; vide pour la vitesse 1.

    Lève `ValueError` hors de [0,25 ; 4].
    """
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}, got {speed}")
    speed = float(speed)
    if speed == 1.0:
        return ""
    if speed > 2.0:
        stages = [2.0, speed / 2.0]
    elif speed < 0.5:
        stages = [0.5, speed / 0.5]
    else:
        stages = [speed]
    return ",".join(f"atempo={stage}" for stage in stages)


async def _feed(stdin: asyncio.StreamWriter, pcm_chunks: AsyncIterator[bytes]) -> None:
    """Écrit l'entrée dans ffmpeg, puis ferme son entrée pour qu'il termine l'encodage."""
    try:
        async for chunk in pcm_chunks:
            stdin.write(chunk)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # ffmpeg s'est arrêté : la lecture de sa sortie en rend compte avec son code de sortie
    finally:
        stdin.close()


async def _drain(stream: asyncio.StreamReader) -> bytes:
    """Lit la sortie d'erreur de ffmpeg jusqu'à la fin, pour que son tube ne se remplisse pas, et en garde la fin."""
    tail = b""
    while chunk := await stream.read(READ_SIZE):
        tail = (tail + chunk)[-STDERR_TAIL:]
    return tail


async def encode_stream(
    pcm_chunks: AsyncIterator[bytes], *, fmt: str, speed: float, input_rate: int
) -> AsyncIterator[bytes]:
    """Encode du PCM16 mono à `input_rate` dans le format demandé, au fur et à mesure que l'entrée arrive.

    Un seul processus ffmpeg rééchantillonne à 24 kHz, applique la vitesse puis encode. L'entrée lui est écrite par
    une tâche à part, car lire sa sortie et écrire son entrée dans la même boucle se bloquerait mutuellement dès
    que l'un des tubes se remplit. Si le consommateur s'arrête avant la fin (générateur fermé ou annulé), ffmpeg est
    tué et la tâche d'écriture annulée avant le retour : l'appelant peut alors fermer son propre itérateur d'entrée.

    Lève `ValueError` pour un format ou une vitesse inconnus. Une erreur de l'itérateur d'entrée est relevée après le
    dernier octet encodé ; un échec de ffmpeg lève `RuntimeError`.
    """
    if fmt not in _ENCODERS:
        raise ValueError(f"unknown output format: {fmt}")
    filters = ",".join(part for part in (f"aresample={SPEECH_RATE}", atempo_chain(speed)) if part)
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        # Le format de l'entrée est donné en entier : sans ces deux options, ffmpeg lit environ cinq secondes d'audio
        # pour sonder l'entrée avant d'écrire le premier octet, et le client attend ce délai pour rien.
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-f",
        "s16le",
        "-ar",
        str(input_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-af",
        filters,
        *_ENCODERS[fmt],
        "-flush_packets",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    feeder = asyncio.create_task(_feed(process.stdin, pcm_chunks))
    stderr_tail = asyncio.create_task(_drain(process.stderr))
    try:
        while chunk := await process.stdout.read(READ_SIZE):
            yield chunk
        returncode = await process.wait()
        if returncode != 0:
            raise RuntimeError(f"ffmpeg failed to encode (exit {returncode}): {_tail(await stderr_tail)}")
        await feeder  # ffmpeg ne termine qu'après la fin de l'entrée ; relève l'erreur de l'itérateur, s'il en a eu une
    finally:
        # Tout ce qui ne demande pas d'attendre passe en premier : une annulation peut interrompre les attentes.
        _kill(process)
        feeder.cancel()
        stderr_tail.cancel()
        await asyncio.gather(feeder, stderr_tail, return_exceptions=True)
        # Quand le consommateur a cessé de lire, asyncio a mis la lecture du tube en pause : il ne voit la fin du
        # tube, ni ne le ferme, tant que personne ne lit. Le processus est mort : il ne reste à lire que ce que
        # les tubes retiennent.
        while await process.stdout.read(READ_SIZE):
            pass
        await process.wait()
