"""Choix du traitement d'une transcription d'après le modèle et le format demandés, et codes de langue."""

import re
from enum import Enum

from dictee.errors import ApiError

MODELS = ("dictee", "dictee-clean", "dictee-diarize")
RESPONSE_FORMATS = ("json", "text", "srt", "vtt", "verbose_json", "diarized_json")

DEFAULT_LANGUAGE = "fr"

# Les modèles qui imposent les locuteurs, quel que soit le format.
_DIARIZE_MODELS = frozenset({"dictee-diarize", "gpt-4o-transcribe-diarize"})
# La reformulation ne s'applique qu'à ces formats : un sous-titre ou un JSON détaillé garde les mots reconnus.
_CLEAN_FORMATS = frozenset({"json", "text"})
# Région de NeMo pour une langue seule. Les autres langues prennent la région du même nom (fr-FR, de-DE).
_REGIONS = {"en": "US"}
# Une langue seule (`fr`) ou une langue et sa région (`fr-FR`), dans n'importe quelle casse.
_LANGUAGE_CODE = re.compile(r"([A-Za-z]{2})(?:-([A-Za-z]{2}))?")


class Mode(Enum):
    RAW = "raw"
    CLEAN = "clean"
    DIARIZE = "diarize"


def unsupported_format(response_format: str) -> ApiError:
    """Erreur 400 d'un format de réponse inconnu, partagée par `resolve_mode` et `render`."""
    return ApiError(
        400,
        f"Unsupported response_format '{response_format}'. Use one of: {', '.join(RESPONSE_FORMATS)}.",
        param="response_format",
    )


def resolve_mode(model: str, response_format: str) -> Mode:
    """Désigne le traitement d'une transcription.

    Le format `diarized_json` et les modèles `dictee-diarize` et `gpt-4o-transcribe-diarize` donnent les locuteurs.
    `dictee-clean` reformule en `json` et `text` seulement. Tout autre nom de modèle donne la transcription brute.
    Un format inconnu lève une erreur 400.
    """
    if response_format not in RESPONSE_FORMATS:
        raise unsupported_format(response_format)
    if response_format == "diarized_json" or model in _DIARIZE_MODELS:
        return Mode.DIARIZE
    if model == "dictee-clean" and response_format in _CLEAN_FORMATS:
        return Mode.CLEAN
    return Mode.RAW


def _split_language(code: str | None) -> tuple[str, str | None]:
    """Sépare un code en langue (minuscules) et région (majuscules, ou `None`). Une langue absente vaut le français."""
    if not code:
        return DEFAULT_LANGUAGE, None
    match = _LANGUAGE_CODE.fullmatch(code)
    if match is None:
        raise ApiError(
            400,
            f"Unsupported language '{code}'. Send an ISO-639-1 code such as 'fr' or 'en'.",
            param="language",
        )
    language, region = match.groups()
    return language.lower(), region.upper() if region else None


def to_nemo_language(code: str | None) -> str:
    """Convertit un code ISO-639-1, ou un code déjà régional, en code NeMo : `fr` donne `fr-FR`, `en` donne `en-US`."""
    language, region = _split_language(code)
    return f"{language}-{region or _REGIONS.get(language, language.upper())}"


def to_iso_language(code: str | None) -> str:
    """Convertit un code de langue, régional ou non, en code ISO-639-1 : `fr-FR` donne `fr`."""
    return _split_language(code)[0]
