import dataclasses
import json
import logging

import httpx
import pytest

from dictee.cleanup import (
    LLM_TIMEOUT_S,
    MAX_RATIO,
    MAX_WORDS,
    MIN_RATIO,
    MIN_WORDS,
    CleanResult,
    clean,
)
from dictee.profile import DEFAULT_INSTRUCTIONS, GlossaryEntry, Profile

pytestmark = pytest.mark.anyio

PROFILE = Profile((GlossaryEntry("Nemotron"),), DEFAULT_INSTRUCTIONS, "Sofia")
RAW = "bonjour euh je teste nemo tron"
# 20 caractères : les bornes du rapport de longueur tombent sur des nombres entiers de caractères (12 et 28).
RAW_20_CHARS = "un deux trois quatre"


def completion(content, status: int = 200) -> httpx.Response:
    """Réponse de LiteLLM à une requête de chat."""
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
    return httpx.Response(status, json=body)


def raises(exc: Exception):
    """Gestionnaire du faux LiteLLM qui lève `exc` au lieu de répondre."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


class Recorder:
    """Faux LiteLLM : garde la dernière requête reçue ; chaque test choisit la réponse en remplaçant `respond`."""

    def __init__(self) -> None:
        self.last: httpx.Request | None = None
        self.respond = lambda request: completion("")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.last = request
        return self.respond(request)

    def reply(self, content) -> None:
        self.respond = lambda request: completion(content)


@pytest.fixture
async def recorder():
    fake = Recorder()
    async with fake.client:
        yield fake


async def run(recorder: Recorder, settings, raw: str = RAW, profile: Profile = PROFILE) -> CleanResult:
    return await clean(raw, profile=profile, settings=settings, http=recorder.client)


def test_limits_of_the_old_proxy():
    assert (MIN_WORDS, MAX_WORDS) == (4, 400)
    assert (MIN_RATIO, MAX_RATIO) == (0.6, 1.4)
    assert LLM_TIMEOUT_S == 12.0


async def test_payload(settings, recorder):
    recorder.reply("Bonjour, je teste Nemotron.")
    await run(recorder, settings)
    req = recorder.last
    assert str(req.url) == f"{settings.litellm_url}/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {settings.litellm_api_key}"
    body = json.loads(req.content)
    assert body["model"] == "simple" and body["temperature"] == 0
    assert "reasoning_effort" not in body
    assert body["messages"] == [
        {"role": "system", "content": DEFAULT_INSTRUCTIONS + "\nGlossary:\n- Nemotron"},
        {"role": "user", "content": "<transcript>\nbonjour euh je teste nemo tron\n</transcript>"},
    ]


async def test_system_message_carries_the_profile_instructions_and_hints(settings, recorder):
    recorder.reply("Bonjour, je teste Nemotron.")
    glossary = (GlossaryEntry("Nemotron", ("nemo tron", "némo trône")), GlossaryEntry("Magpie"))
    await run(recorder, settings, profile=Profile(glossary, "Be brief.", "Sofia"))
    system = json.loads(recorder.last.content)["messages"][0]
    assert system["content"] == "Be brief.\nGlossary:\n- Nemotron (often heard as: nemo tron, némo trône)\n- Magpie"


async def test_system_message_is_the_bare_instructions_without_glossary(settings, recorder):
    recorder.reply("Bonjour, je teste Nemotron.")
    await run(recorder, settings, profile=Profile((), "Be brief.", "Sofia"))
    system = json.loads(recorder.last.content)["messages"][0]
    assert system["content"] == "Be brief."


async def test_model_comes_from_the_settings(settings, recorder):
    recorder.reply("Bonjour, je teste Nemotron.")
    await run(recorder, dataclasses.replace(settings, cleanup_model="flash-low"))
    assert json.loads(recorder.last.content)["model"] == "flash-low"


async def test_sets_its_own_timeout(settings, recorder):
    # Le client HTTP partagé de l'app n'a pas de délai de lecture : la requête doit porter celui de `clean`.
    recorder.reply("Bonjour, je teste Nemotron.")
    await run(recorder, settings)
    assert recorder.last.extensions["timeout"]["read"] == LLM_TIMEOUT_S


async def test_returns_the_cleaned_text(settings, recorder):
    recorder.reply("Bonjour, je teste Nemotron.")
    assert await run(recorder, settings) == CleanResult("Bonjour, je teste Nemotron.", "cleaned")


@pytest.mark.parametrize(
    "answer",
    [
        "<transcript>Bonjour, je teste Nemotron.</transcript>",
        "\n<transcript>\nBonjour, je teste Nemotron.\n</transcript>\n",
        "  Bonjour, je teste Nemotron.  \n",
    ],
)
async def test_returns_the_cleaned_text_without_its_wrapper(answer, settings, recorder):
    recorder.reply(answer)
    assert await run(recorder, settings) == CleanResult("Bonjour, je teste Nemotron.", "cleaned")


@pytest.mark.parametrize(
    "raw,decision",
    [("", "short"), ("trois mots seulement", "short"), (" ".join(["mot"] * (MAX_WORDS + 1)), "long")],
)
async def test_lengths_out_of_range_skip_the_llm(raw, decision, settings, recorder):
    assert await run(recorder, settings, raw) == CleanResult(raw, decision)
    assert recorder.last is None


@pytest.mark.parametrize("count", [MIN_WORDS, MAX_WORDS])
async def test_word_bounds_are_inclusive(count, settings, recorder):
    raw = " ".join(["mot"] * count)
    recorder.reply(raw)
    assert await run(recorder, settings, raw) == CleanResult(raw, "cleaned")
    assert recorder.last is not None


@pytest.mark.parametrize("length", [1, 11, 29, 200])
async def test_ratio_guard_gives_back_the_raw_text(length, settings, recorder):
    recorder.reply("x" * length)
    assert await run(recorder, settings, RAW_20_CHARS) == CleanResult(RAW_20_CHARS, "ratio")


@pytest.mark.parametrize("length", [12, 20, 28])
async def test_ratio_bounds_are_inclusive(length, settings, recorder):
    recorder.reply("x" * length)
    assert await run(recorder, settings, RAW_20_CHARS) == CleanResult("x" * length, "cleaned")


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
async def test_timeout(error, settings, recorder):
    recorder.respond = raises(error)
    assert await run(recorder, settings) == CleanResult(RAW, "llm_timeout")


async def test_connection_error(settings, recorder):
    recorder.respond = raises(httpx.ConnectError("refused"))
    assert await run(recorder, settings) == CleanResult(RAW, "llm_failed")


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_http_error(status, settings, recorder):
    recorder.respond = lambda request: httpx.Response(status, json={"error": {"message": "nope"}})
    assert await run(recorder, settings) == CleanResult(RAW, "llm_failed")


async def test_error_status_wins_over_a_well_formed_body(settings, recorder):
    recorder.respond = lambda request: completion("Bonjour, je teste Nemotron.", status=500)
    assert await run(recorder, settings) == CleanResult(RAW, "llm_failed")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>bad gateway</html>"),
        httpx.Response(200, content=b"\xff\xfe"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json="text"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{}]}),
        httpx.Response(200, json={"choices": [{"message": {}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": ["Bonjour."]}}]}),
    ],
)
async def test_unreadable_or_malformed_answer(response, settings, recorder):
    recorder.respond = lambda request: response
    assert await run(recorder, settings) == CleanResult(RAW, "llm_failed")


@pytest.mark.parametrize("answer", ["", " \n ", "<transcript></transcript>", "<transcript>\n \n</transcript>"])
async def test_empty_answer(answer, settings, recorder):
    recorder.reply(answer)
    assert await run(recorder, settings) == CleanResult(RAW, "llm_empty")


async def test_never_logs_the_transcript(settings, recorder, caplog):
    caplog.set_level(logging.DEBUG)
    recorder.reply("Bonjour, je teste Nemotron.")
    await run(recorder, settings)
    recorder.respond = raises(httpx.ReadTimeout("slow"))
    await run(recorder, settings)
    recorder.reply("x")
    await run(recorder, settings)
    assert "nemo tron" not in caplog.text and "Nemotron" not in caplog.text
