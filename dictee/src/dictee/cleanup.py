"""Reformulation d'une transcription brute par LiteLLM, avec les garde-fous de l'ancien proxy.

`clean` ne lève jamais d'exception à cause du LLM : quand il ne répond pas ou répond mal, elle rend le texte brut et la
décision qui l'explique. Rien n'est journalisé ici, ni le texte brut ni le texte reformulé ; la route journalise la
décision seule.
"""

from dataclasses import dataclass

import httpx

from dictee.profile import Profile, glossary_prompt
from dictee.settings import Settings

# Délai en secondes accordé à LiteLLM. Le client HTTP partagé de l'app n'a pas de délai de lecture : chaque appel fixe
# le sien.
LLM_TIMEOUT_S = 12.0
# Bornes incluses : seuls les textes de 4 à 400 mots partent au LLM, et sa réponse doit garder entre 0,6 et 1,4 fois
# la longueur du texte brut, comptée en caractères.
MIN_WORDS = 4
MAX_WORDS = 400
MIN_RATIO = 0.6
MAX_RATIO = 1.4

_OPENING_TAG = "<transcript>"
_CLOSING_TAG = "</transcript>"


@dataclass(frozen=True)
class CleanResult:
    """Texte à rendre et décision qui l'explique.

    `decision` vaut `cleaned` (le texte est celui du LLM) ou l'une des raisons pour lesquelles il est brut : `short`,
    `long`, `llm_timeout`, `llm_failed`, `llm_empty`, `ratio`.
    """

    text: str
    decision: str


def _answer(response: httpx.Response) -> str | None:
    """Texte de la première réponse du LLM ; `None` si le statut ou la forme du corps n'est pas celui d'une réponse."""
    if not response.is_success:
        return None
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _unwrap(answer: str) -> str:
    """Texte de la réponse sans espaces autour ni balises `<transcript>` qui l'entourent."""
    answer = answer.strip()
    if answer.startswith(_OPENING_TAG) and answer.endswith(_CLOSING_TAG):
        answer = answer[len(_OPENING_TAG) : -len(_CLOSING_TAG)]
    return answer.strip()


async def clean(raw: str, *, profile: Profile, settings: Settings, http: httpx.AsyncClient) -> CleanResult:
    """Fait reformuler `raw` par LiteLLM ; rend `raw` tel quel quand un garde-fou l'écarte.

    Le message système réunit les consignes du profil et, s'il en a un, son glossaire.
    """
    words = len(raw.split())
    if words < MIN_WORDS:
        return CleanResult(raw, "short")
    if words > MAX_WORDS:
        return CleanResult(raw, "long")

    system = profile.instructions
    glossary = glossary_prompt(profile.glossary)
    if glossary:
        system += "\n" + glossary
    try:
        response = await http.post(
            settings.litellm_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
            json={
                "model": settings.cleanup_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{_OPENING_TAG}\n{raw}\n{_CLOSING_TAG}"},
                ],
            },
            timeout=LLM_TIMEOUT_S,
        )
    except httpx.TimeoutException:
        return CleanResult(raw, "llm_timeout")
    except httpx.HTTPError:
        return CleanResult(raw, "llm_failed")

    answer = _answer(response)
    if answer is None:
        return CleanResult(raw, "llm_failed")
    cleaned = _unwrap(answer)
    if not cleaned:
        return CleanResult(raw, "llm_empty")
    if not MIN_RATIO <= len(cleaned) / len(raw) <= MAX_RATIO:
        return CleanResult(raw, "ratio")
    return CleanResult(cleaned, "cleaned")
