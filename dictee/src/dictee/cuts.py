"""Points de coupe d'un audio long : silences repérés par ffmpeg, puis morceaux d'environ 30 s coupés dedans."""

import asyncio
import bisect
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from dictee.audio import READ_SIZE, AudioDecodeError, _drain, _kill, _reap

# Réglage de `silencedetect`, vérifié sur de vrais enregistrements : un silence est un passage sous -35 dB
# d'au moins 0,3 s.
SILENCE_DB = -35.0
MIN_SILENCE_S = 0.3
# Un morceau vise 30 s : sa fin se place entre 20 et 40 s après son début.
TARGET_CHUNK_S = 30.0
MIN_CHUNK_S = 20.0
MAX_CHUNK_S = 40.0

# Une ligne de ffmpeg plus longue que cela n'est jamais une ligne de `silencedetect` : seule sa fin est gardée.
_MAX_LINE = 4096
_SILENCE_LINE = re.compile(rb"\[silencedetect @ [^\]]*\] silence_(start|end): ([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


@dataclass(frozen=True)
class Silence:
    """Un silence de `start` à `end` secondes."""

    start: float
    end: float

    @property
    def middle(self) -> float:
        """Le milieu du silence : une coupe y est le plus loin possible de la parole des deux côtés."""
        return (self.start + self.end) / 2


async def detect_silences(wav: Path, duration: float, *, timeout_s: float = 600.0) -> list[Silence]:
    """Les silences d'un WAV, dans l'ordre, repérés par une seule passe de ffmpeg avec `silencedetect`.

    La sortie d'erreur de ffmpeg se lit au fur et à mesure et seules les lignes du filtre sont gardées. Un silence
    encore ouvert à la fin du fichier se ferme à `duration`. Un fichier que ffmpeg ne lit pas lève `AudioDecodeError`,
    comme une passe qui dépasse `timeout_s` secondes, après avoir tué ffmpeg. Les messages ne contiennent ni le chemin
    ni le contenu du fichier.
    """
    silences: list[Silence] = []
    start: float | None = None  # début du silence en cours
    pending = b""  # fin de la sortie d'erreur, d'une ligne qui n'est pas finie
    process = None
    try:
        async with asyncio.timeout(timeout_s):
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-nostats",
                "-loglevel",
                "info",  # `silencedetect` écrit ses résultats au niveau info
                "-protocol_whitelist",
                "file",
                "-i",
                str(wav),
                "-af",
                f"silencedetect=noise={SILENCE_DB}dB:d={MIN_SILENCE_S}",
                "-f",
                "null",
                "-",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,  # le muxer `null` n'y écrit rien, mais `_reap` le lit
                stderr=asyncio.subprocess.PIPE,
            )
            while chunk := await process.stderr.read(READ_SIZE):
                *lines, pending = (pending + chunk).split(b"\n")
                pending = pending[-_MAX_LINE:]
                for line in lines:
                    if match := _SILENCE_LINE.match(line):
                        seconds = float(match[2])
                        if match[1] == b"start":
                            start = seconds
                        elif start is not None:  # une fin sans début est ignorée
                            silences.append(Silence(start, seconds))
                            start = None
            returncode = await process.wait()
    except TimeoutError:
        raise AudioDecodeError(f"scanning for silences took longer than {timeout_s:g} seconds") from None
    finally:
        if process is not None:
            # Une lecture interrompue peut laisser le tube d'erreur en pause, avec des Ko en attente : asyncio n'en voit
            # alors pas la fin et ne ferme pas son transport. ffmpeg mort, le tube se lit donc jusqu'à sa fin, comme
            # `_reap` le fait pour la sortie.
            _kill(process)
            await _drain(process.stderr)
            await _reap(process)
    if returncode != 0:
        raise AudioDecodeError(f"ffmpeg could not scan the file for silences (exit {returncode})")
    if start is not None:
        silences.append(Silence(start, duration))
    return silences


def _next_cut(middles: Sequence[float], start: float) -> float:
    """La coupe du morceau qui commence à `start`, parmi les milieux de silence `middles`, triés.

    C'est le milieu le plus proche de 30 s après `start` parmi ceux qui tombent entre 20 et 40 s après `start`, le
    plus tôt à égalité ; sans milieu dans cette fenêtre, c'est `start` plus 30 s.
    """

    # La fenêtre se mesure par la soustraction que l'appelant fera pour la durée du morceau (`fin - début`) : une
    # coupe retenue donne exactement un morceau de 20 à 40 s, sans l'arrondi qu'ajouterait un calcul de `start + 40`.
    def since_start(middle: float) -> float:
        return middle - start

    first = bisect.bisect_left(middles, MIN_CHUNK_S, key=since_start)
    last = bisect.bisect_right(middles, MAX_CHUNK_S, key=since_start)
    # `min` garde le premier des ex aequo, donc le plus tôt, puisque les milieux sont triés.
    return min(
        middles[first:last],
        key=lambda middle: abs(since_start(middle) - TARGET_CHUNK_S),
        default=start + TARGET_CHUNK_S,
    )


def plan_cuts(duration: float, silences: Sequence[Silence]) -> list[float]:
    """Les points de coupe, en secondes, d'un audio de `duration` secondes : aucun morceau ne dépasse 40 s.

    Tant que le reste de l'audio dépasse 40 s, le morceau en cours se coupe dans le silence dont le milieu est le plus
    proche de 30 s après son début, ou à 30 s sans silence entre 20 et 40 s. Un audio de 40 s ou moins n'a aucune coupe.
    Chaque morceau fait de 20 à 40 s, sauf le dernier, qui prend ce qui reste et peut être plus court.
    """
    middles = sorted(silence.middle for silence in silences)
    cuts: list[float] = []
    start = 0.0
    while duration - start > MAX_CHUNK_S:
        start = _next_cut(middles, start)
        cuts.append(start)
    return cuts


def chunk_bounds(duration: float, cuts: Sequence[float]) -> list[tuple[float, float]]:
    """Les morceaux, de `(début, fin)` en secondes, que forment les coupes : ils couvrent tout l'audio, bout à bout."""
    return list(pairwise([0.0, *cuts, duration]))
