"""Conversion audio par ffmpeg : décodage des envois et encodage de la synthèse."""

import asyncio
import struct
from collections.abc import AsyncIterator, Buffer, Callable, Iterator
from pathlib import Path

# NeMo transcrit du PCM16 mono à 16 kHz.
TRANSCRIPTION_RATE = 16000
SAMPLE_WIDTH = 2  # octets par échantillon
_BYTES_PER_SECOND = TRANSCRIPTION_RATE * SAMPLE_WIDTH  # octets de PCM par seconde d'audio
# En-tête d'un WAV PCM : RIFF, format et début du chunk de données.
WAV_HEADER_SIZE = 44
_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
# Taille des lectures pendant un décodage : le plafond de durée est dépassé de cette taille au plus avant l'arrêt.
DECODE_READ_SIZE = 65536
# Fréquence du format `pcm` d'OpenAI : tous les formats de sortie partent de ce rééchantillonnage.
SPEECH_RATE = 24000
# Taille des lectures sur la sortie de ffmpeg : de petits blocs, pour que le client reçoive le son au fil de l'eau.
READ_SIZE = 4096
# Fin de la sortie d'erreur de ffmpeg conservée pour les messages.
STDERR_TAIL = 500

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

# Démuxeurs acceptés pour un envoi, sous leur nom dans `ffmpeg -demuxers` : `mov` couvre mp4, m4a et 3gp, `matroska`
# couvre webm. L'envoi vient d'un client et ffmpeg devine le format d'après le contenu : tout démuxeur absent de cette
# liste est refusé, en particulier ceux qui lisent d'autres fichiers ou d'autres adresses que le fichier envoyé
# (playlists HLS, listes concat).
INPUT_DEMUXERS = ("mp3", "mov", "ogg", "wav", "w64", "flac", "matroska", "aac", "aiff", "caf", "amr")

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
    """Fichier que ffmpeg ne sait pas lire, sans aucun échantillon, ou dont le décodage dure trop longtemps."""


class AudioTooLong(AudioDecodeError):
    """L'audio décodé dépasse la durée maximale."""


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()


def _tail(stderr: bytes, hide: tuple[str, ...] = ()) -> str:
    """Fin de la sortie d'erreur de ffmpeg, où les chemins de `hide` sont remplacés : un message ne les révèle pas."""
    text = stderr.decode(errors="replace").strip()
    for path in hide:
        text = text.replace(path, "<path>")
    return text[-STDERR_TAIL:]


async def _reap(process: asyncio.subprocess.Process, *tasks: asyncio.Task) -> None:
    """Tue ffmpeg s'il tourne encore, arrête les tâches qui le servent et attend la fin du processus et de ses tubes."""
    # Tout ce qui ne demande pas d'attendre passe en premier : une annulation peut interrompre les attentes.
    _kill(process)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Quand le lecteur a cessé de lire, asyncio a mis la lecture du tube en pause : il ne voit la fin du tube, ni ne
    # le ferme, tant que personne ne lit. Le processus est mort : il ne reste à lire que ce que les tubes retiennent.
    while await process.stdout.read(READ_SIZE):
        pass
    await process.wait()


def _wav_header(pcm_size: int) -> bytes:
    """En-tête canonique de 44 octets d'un WAV PCM16 mono 16 kHz dont les données font `pcm_size` octets."""
    return _WAV_HEADER.pack(
        b"RIFF",
        WAV_HEADER_SIZE - 8 + pcm_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM entier
        1,  # mono
        TRANSCRIPTION_RATE,
        _BYTES_PER_SECOND,
        SAMPLE_WIDTH,
        SAMPLE_WIDTH * 8,
        b"data",
        pcm_size,
    )


async def _decode_pcm(
    src: Path,
    sink: Callable[[bytes], object],
    *,
    max_seconds: float,
    timeout_s: float,
    long_seconds: float | None,
    on_long: Callable[[], None] | None,
) -> int:
    """Décode n'importe quel audio en PCM16 mono 16 kHz, que `sink` reçoit bloc par bloc ; rend la taille du PCM.

    ffmpeg lit le chemin et non un tube : un m4a d'Android range souvent son index en fin de fichier, que ffmpeg
    ne peut atteindre qu'en se déplaçant dans le fichier. Il rend du PCM brut, que `sink` range là où l'appelant le
    veut (un tampon, un fichier) : rien d'autre n'est gardé ici. Le poids du décodé n'a pas de rapport avec celui de
    l'envoi (une heure de silence en FLAC pèse quelques centaines de Ko et décode en 115 Mo), donc la taille décodée
    est bornée ici et nulle part ailleurs : au-delà de `max_seconds` d'audio, ffmpeg est tué et `AudioTooLong` est
    levée, après avoir lu au plus `DECODE_READ_SIZE` octets de plus que le plafond ; après `timeout_s` secondes,
    ffmpeg est tué et `AudioDecodeError` est levée. Le plafond n'a pas de valeur par défaut : chaque appelant a le
    sien.

    `long_seconds` et `on_long` vont ensemble. `on_long()` est appelé une fois, de façon synchrone, juste après la
    lecture qui fait passer l'audio décodé au-dessus de `long_seconds`, donc après au plus `DECODE_READ_SIZE` octets
    de plus que le seuil. Il sert à réserver une ressource rare avant de continuer : s'il lève une exception, ffmpeg
    est tué et l'exception remonte telle quelle, sans que le reste de l'audio soit décodé. Un audio qui n'atteint
    pas le seuil n'appelle rien. Si la même lecture dépasse aussi `max_seconds`, `AudioTooLong` passe d'abord.
    Une exception de `sink` arrête de même ffmpeg et remonte telle quelle.

    Un fichier dont le format n'est pas dans `INPUT_DEMUXERS` lève `AudioDecodeError`, comme un fichier illisible. Les
    messages ne contiennent ni le contenu du fichier ni son chemin.
    """
    if (long_seconds is None) != (on_long is None):
        raise ValueError("long_seconds and on_long go together: give both or neither")
    max_pcm = int(max_seconds * _BYTES_PER_SECOND)
    long_pcm = None if long_seconds is None else int(long_seconds * _BYTES_PER_SECOND)
    pcm_size = 0
    process = None
    stderr_tail = None
    try:
        async with asyncio.timeout(timeout_s):
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                # Options d'entrée, avant `-i` : seul le protocole `file` et seuls les démuxeurs de `INPUT_DEMUXERS`.
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                ",".join(INPUT_DEMUXERS),
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
            stderr_tail = asyncio.create_task(_drain(process.stderr))
            while chunk := await process.stdout.read(DECODE_READ_SIZE):
                sink(chunk)
                pcm_size += len(chunk)
                if pcm_size > max_pcm:
                    raise AudioTooLong(f"the audio is longer than {max_seconds:g} seconds")
                if on_long is not None and pcm_size > long_pcm:
                    on_long()
                    on_long = None  # un seul appel
            returncode = await process.wait()
            stderr = await stderr_tail
    except TimeoutError:
        raise AudioDecodeError(f"decoding took longer than {timeout_s:g} seconds") from None
    finally:
        if process is not None:
            await _reap(process, *([stderr_tail] if stderr_tail else []))
    if returncode != 0:
        hidden = (str(src), str(src.parent))
        raise AudioDecodeError(f"ffmpeg could not decode the file (exit {returncode}): {_tail(stderr, hidden)}")
    if pcm_size == 0:
        raise AudioDecodeError("the file contains no audio samples")
    return pcm_size


async def to_wav16k(
    src: Path,
    *,
    max_seconds: float,
    timeout_s: float = 600.0,
    long_seconds: float | None = None,
    on_long: Callable[[], None] | None = None,
) -> bytearray:
    """Décode n'importe quel audio en WAV PCM16 mono 16 kHz, dans un seul tampon.

    Le PCM s'ajoute au fur et à mesure à un `bytearray` dont les 44 premiers octets sont réservés à l'en-tête WAV,
    écrit en place à la fin : ni copie, ni second tampon. Les limites, `long_seconds`, `on_long` et les erreurs sont
    celles de `_decode_pcm`.
    """
    wav = bytearray(WAV_HEADER_SIZE)
    pcm_size = await _decode_pcm(
        src, wav.extend, max_seconds=max_seconds, timeout_s=timeout_s, long_seconds=long_seconds, on_long=on_long
    )
    wav[:WAV_HEADER_SIZE] = _wav_header(pcm_size)
    return wav


async def decode_wav16k(
    src: Path,
    dst: Path,
    *,
    max_seconds: float,
    timeout_s: float = 600.0,
    long_seconds: float | None = None,
    on_long: Callable[[], None] | None = None,
) -> float:
    """Décode n'importe quel audio dans le fichier `dst`, en WAV PCM16 mono 16 kHz, et rend sa durée en secondes.

    Le fichier commence par 44 octets réservés, chaque bloc décodé s'y ajoute, puis l'en-tête canonique s'écrit en
    place à la fin : le PCM ne passe jamais en mémoire au-delà du bloc en cours. Les limites, `long_seconds`,
    `on_long` et les erreurs sont celles de `_decode_pcm`. Toute exception, l'annulation comprise, efface `dst` :
    un `dst` qui existe est un WAV complet. Le dossier parent de `dst` doit exister.
    """
    try:
        with dst.open("wb") as out:
            out.write(bytes(WAV_HEADER_SIZE))
            pcm_size = await _decode_pcm(
                src, out.write, max_seconds=max_seconds, timeout_s=timeout_s, long_seconds=long_seconds, on_long=on_long
            )
            out.seek(0)
            out.write(_wav_header(pcm_size))
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    return pcm_size / _BYTES_PER_SECOND


def _parse_wav(wav: Buffer) -> tuple[int, int, memoryview]:
    """Cadence, taille d'une trame et données d'un WAV PCM, sans copier ses échantillons.

    Les chunks sont parcourus un à un : un enregistreur peut en placer d'autres (LIST, par exemple) avant les données.
    Une taille de données qui dépasse la fin du tampon, comme en écrit un WAV diffusé en continu, est ramenée à
    cette fin.
    """
    view = memoryview(wav)
    if len(view) < 12 or view[:4] != b"RIFF" or view[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    frame = None
    position = 12
    while position + 8 <= len(view):
        chunk_id = bytes(view[position : position + 4])
        (size,) = struct.unpack_from("<I", view, position + 4)
        body = position + 8
        if chunk_id == b"fmt ":
            _, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", view, body)
            frame = (rate, channels * bits // 8)
        elif chunk_id == b"data":
            if frame is None:
                raise ValueError("WAV data before its format")
            return frame[0], frame[1], view[body : body + size]
        position = body + size + size % 2
    raise ValueError("WAV without data")


def wav_duration(wav: Buffer) -> float:
    """Durée en secondes d'un WAV, lue dans son en-tête."""
    rate, frame_size, data = _parse_wav(wav)
    return len(data) / frame_size / rate


def wav_pcm(wav: Buffer) -> memoryview:
    """Les échantillons d'un WAV, sans l'en-tête : une vue sur le tampon, sans copie."""
    return _parse_wav(wav)[2]


def read_wav_slice(path: Path, start: float, end: float) -> bytes:
    """Un WAV canonique des échantillons de `start` à `end` secondes d'un fichier écrit par `decode_wav16k`.

    Les bornes deviennent les échantillons `[round(start * 16000), round(end * 16000))`, et la fin est ramenée à
    celle du fichier. Seule la tranche est lue, jamais le fichier entier.
    """
    total = (path.stat().st_size - WAV_HEADER_SIZE) // SAMPLE_WIDTH
    last = min(round(end * TRANSCRIPTION_RATE), total)
    first = min(round(start * TRANSCRIPTION_RATE), last)
    with path.open("rb") as wav:
        wav.seek(WAV_HEADER_SIZE + first * SAMPLE_WIDTH)
        pcm = wav.read((last - first) * SAMPLE_WIDTH)
    return _wav_header(len(pcm)) + pcm


def pcm_frames(path: Path, frame_bytes: int) -> Iterator[bytes]:
    """Les échantillons d'un WAV écrit par `decode_wav16k`, après l'en-tête de 44 octets, par blocs de `frame_bytes`.

    Le dernier bloc est plus court quand le total n'est pas un multiple de `frame_bytes`. Un seul bloc est en
    mémoire à la fois.
    """
    with path.open("rb") as wav:
        wav.seek(WAV_HEADER_SIZE)
        while block := wav.read(frame_bytes):
            yield block


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
        await _reap(process, feeder, stderr_tail)
