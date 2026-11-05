"""Rendu des réponses de transcription (json, text, srt, vtt, verbose_json, diarized_json) et événements SSE."""

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass

from starlette.responses import JSONResponse, PlainTextResponse, Response

from dictee.errors import ApiError, envelope
from dictee.modes import to_iso_language, unsupported_format
from dictee.types import Segment, Transcript, Word

# Un sous-titre se ferme après un mot qui finit ainsi, ou avant le mot qui dépasserait l'une des deux limites.
SENTENCE_ENDINGS = (".", "?", "!", "…")
MAX_CUE_SECONDS = 7.0
MAX_CUE_CHARACTERS = 84


@dataclass(frozen=True)
class Cue:
    """Un sous-titre : les mots qu'il porte, de l'instant du premier à la fin du dernier."""

    start: float
    end: float
    text: str
    speaker: str | None


def _cue(words: Sequence[Word], split_on_speaker: bool) -> Cue:
    # Sans découpe par locuteur, un sous-titre peut en couvrir plusieurs : il n'en désigne aucun.
    speaker = words[0].speaker if split_on_speaker else None
    return Cue(words[0].start, words[-1].end, " ".join(word.text for word in words), speaker)


def _would_overflow(current: Sequence[Word], size: int, word: Word, split_on_speaker: bool) -> bool:
    """Dit si `word` ne tient plus dans le sous-titre en cours, de `size` caractères."""
    return (
        word.end - current[0].start > MAX_CUE_SECONDS
        or size + 1 + len(word.text) > MAX_CUE_CHARACTERS
        or (split_on_speaker and word.speaker != current[-1].speaker)
    )


def build_cues(words: Sequence[Word], *, split_on_speaker: bool) -> list[Cue]:
    """Regroupe les mots en sous-titres.

    Un sous-titre se ferme après un mot qui finit une phrase, avant le mot qui le ferait durer plus de 7 s ou
    dépasser 84 caractères et, si `split_on_speaker` est vrai, quand le locuteur change. Un mot seul qui dépasse
    déjà une limite forme son propre sous-titre.
    """
    cues: list[Cue] = []
    current: list[Word] = []
    size = 0  # Caractères du texte du sous-titre en cours, espaces comprises.
    for word in words:
        if current and _would_overflow(current, size, word, split_on_speaker):
            cues.append(_cue(current, split_on_speaker))
            current, size = [], 0
        size += len(word.text) + (1 if current else 0)
        current.append(word)
        if word.text.endswith(SENTENCE_ENDINGS):
            cues.append(_cue(current, split_on_speaker))
            current, size = [], 0
    if current:
        cues.append(_cue(current, split_on_speaker))
    return cues


def _cues(transcript: Transcript) -> list[Cue]:
    # Une transcription avec segments vient de la séparation des locuteurs : ses sous-titres les suivent.
    return build_cues(transcript.words, split_on_speaker=transcript.segments is not None)


def _round(seconds: float) -> float:
    return round(seconds, 3)


def _timestamp(seconds: float, separator: str) -> str:
    """Formate un instant en `HH:MM:SS` suivi des millisecondes, séparées par `,` (srt) ou `.` (vtt)."""
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def _plain_text(transcript: Transcript) -> str:
    """Le texte seul, ou un paragraphe préfixé par son locuteur à chaque tour de parole."""
    if transcript.segments is None:
        return transcript.text
    return "\n\n".join(f"{segment.speaker} : {segment.text}" for segment in transcript.segments)


def _srt(transcript: Transcript) -> str:
    blocks = []
    for number, cue in enumerate(_cues(transcript), start=1):
        text = cue.text if cue.speaker is None else f"{cue.speaker} : {cue.text}"
        blocks.append(f"{number}\n{_timestamp(cue.start, ',')} --> {_timestamp(cue.end, ',')}\n{text}\n")
    return "\n".join(blocks)


def _vtt(transcript: Transcript) -> str:
    blocks = []
    for cue in _cues(transcript):
        # `&` et `<` ouvriraient une entité ou une balise dans le texte d'un sous-titre.
        text = html.escape(cue.text, quote=False)
        if cue.speaker is not None:
            text = f"<v {cue.speaker}>{text}"
        blocks.append(f"{_timestamp(cue.start, '.')} --> {_timestamp(cue.end, '.')}\n{text}\n")
    return "WEBVTT\n\n" + "\n".join(blocks)


def _verbose_json(transcript: Transcript, granularities: frozenset[str]) -> dict:
    body: dict = {
        "task": "transcribe",
        "language": to_iso_language(transcript.language),
        "duration": _round(transcript.duration),
        "text": transcript.text,
    }
    if "word" in granularities:
        body["words"] = [
            {"word": word.text, "start": _round(word.start), "end": _round(word.end)} for word in transcript.words
        ]
    if "segment" in granularities:
        # Les champs à zéro n'ont pas de sens ici, mais les clients stricts (le SDK d'OpenAI) les exigent.
        body["segments"] = [
            {
                "id": index,
                "seek": 0,
                "start": _round(cue.start),
                "end": _round(cue.end),
                "text": cue.text,
                "tokens": [],
                "temperature": 0.0,
                "avg_logprob": 0.0,
                "compression_ratio": 0.0,
                "no_speech_prob": 0.0,
            }
            for index, cue in enumerate(_cues(transcript))
        ]
    return body


def _segment_json(segment: Segment) -> dict:
    return {
        "type": "transcript.text.segment",
        "id": segment.id,
        "start": _round(segment.start),
        "end": _round(segment.end),
        "speaker": segment.speaker,
        "text": segment.text,
    }


def _diarized_json(transcript: Transcript) -> dict:
    # Sans locuteurs (`segments` absent), il n'y a aucun segment à rendre.
    return {
        "task": "transcribe",
        "duration": _round(transcript.duration),
        "text": transcript.text,
        "segments": [_segment_json(segment) for segment in transcript.segments or ()],
    }


def render_body(transcript: Transcript, fmt: str, *, granularities: frozenset[str]) -> dict | str:
    """Le corps de la réponse d'une transcription dans le format demandé.

    Un objet pour `json`, `verbose_json` et `diarized_json`, une chaîne pour `text`, `srt` et `vtt`. Quand
    `transcript.segments` n'est pas `None`, le rendu porte les locuteurs. `granularities` ne sert qu'à `verbose_json`
    (`word` ajoute les mots, `segment` ajoute les sous-titres). Un format inconnu lève une erreur 400.
    """
    match fmt:
        case "json":
            return {"text": _plain_text(transcript)}
        case "text":
            return _plain_text(transcript)
        case "srt":
            return _srt(transcript)
        case "vtt":
            return _vtt(transcript)
        case "verbose_json":
            return _verbose_json(transcript, granularities)
        case "diarized_json":
            return _diarized_json(transcript)
        case _:
            raise unsupported_format(fmt)


def render(transcript: Transcript, fmt: str, *, granularities: frozenset[str]) -> Response:
    """Rend une transcription dans le format demandé : le corps de `render_body` dans sa réponse HTTP.

    Un objet part en JSON, une chaîne en texte brut, sauf `vtt` qui a son propre type de contenu. Un format inconnu
    lève une erreur 400.
    """
    body = render_body(transcript, fmt, granularities=granularities)
    if isinstance(body, dict):
        return JSONResponse(body)
    if fmt == "vtt":
        return Response(body, media_type="text/vtt; charset=utf-8")
    return PlainTextResponse(body)


def _event(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def delta_event(text: str) -> str:
    return _event({"type": "transcript.text.delta", "delta": text})


def segment_event(segment: Segment) -> str:
    return _event(_segment_json(segment))


def done_event(text: str) -> str:
    return _event({"type": "transcript.text.done", "text": text})


def error_event(failure: ApiError) -> str:
    """Événement d'erreur d'une réponse en continu déjà commencée : le corps de l'erreur de l'API, sous `error`."""
    error = envelope(failure.message, type=failure.type, param=failure.param, code=failure.code)["error"]
    return _event({"type": "error", "error": error})
