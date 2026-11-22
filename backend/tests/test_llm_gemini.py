"""Tests du client Gemini avec un faux transport HTTP : aucun réseau, aucune vraie clé."""

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.services.llm.errors import LLMError, LLMRefusalError
from app.services.llm.gemini import DEFAULT_GEMINI_MODEL, GeminiLLM
from app.services.llm.provider import get_llm


class Answer(BaseModel):
    summary: str


def gemini_reply(text: str, finish: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10},
        },
    )


def make(
    *responses: httpx.Response, retries: int = 2
) -> tuple[GeminiLLM, list[httpx.Request], list[float]]:
    queue, requests, sleeps = list(responses), [], []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queue.pop(0)

    settings = Settings(
        _env_file=None, llm_provider="gemini", llm_api_key="cle-test", llm_max_retries=retries
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GeminiLLM(settings, client=client, sleep=sleeps.append), requests, sleeps


def test_valid_answer_and_request_shape() -> None:
    llm, requests, _ = make(gemini_reply('{"summary": "pool saturé"}'))
    assert (
        llm.complete_structured("Tu es un agent.", "<telemetry>{}</telemetry>", Answer).summary
        == "pool saturé"
    )
    request = requests[0]
    assert request.url.path.endswith(f"/models/{DEFAULT_GEMINI_MODEL}:generateContent")
    assert request.headers["x-goog-api-key"] == "cle-test"
    body: dict[str, Any] = json.loads(request.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert '"summary"' in body["contents"][0]["parts"][0]["text"]  # le schéma est dans le prompt


def test_invalid_json_is_asked_again_once() -> None:
    llm, requests, _ = make(gemini_reply("pas du json"), gemini_reply('{"summary": "ok"}'))
    assert llm.complete_structured("s", "u", Answer).summary == "ok"
    assert len(requests) == 2


def test_gives_up_after_two_invalid_answers() -> None:
    llm, _, _ = make(gemini_reply("{}"), gemini_reply("{}"))
    with pytest.raises(LLMError, match="aucune réponse valide"):
        llm.complete_structured("s", "u", Answer)


def test_quota_errors_are_retried_with_growing_delay() -> None:
    llm, requests, sleeps = make(
        httpx.Response(429), httpx.Response(503), gemini_reply('{"summary": "ok"}')
    )
    assert llm.complete_structured("s", "u", Answer).summary == "ok"
    assert len(requests) == 3 and sleeps == [15.0, 4.0]  # 429 : attente longue ; 503 : courte


def test_stops_after_max_retries() -> None:
    llm, requests, _ = make(httpx.Response(429), httpx.Response(429), retries=1)
    with pytest.raises(LLMError, match="HTTP 429"):
        llm.complete_structured("s", "u", Answer)
    assert len(requests) == 2


def test_bad_key_is_not_retried() -> None:
    llm, requests, _ = make(httpx.Response(400, json={"error": {"message": "API key not valid"}}))
    with pytest.raises(LLMError, match="400"):
        llm.complete_structured("s", "u", Answer)
    assert len(requests) == 1


def test_safety_block_is_a_refusal() -> None:
    llm, _, _ = make(gemini_reply("", finish="SAFETY"))
    with pytest.raises(LLMRefusalError):
        llm.complete_structured("s", "u", Answer)


def test_get_llm_and_missing_key() -> None:
    assert isinstance(
        get_llm(Settings(_env_file=None, llm_provider="gemini", llm_api_key="k")), GeminiLLM
    )
    with pytest.raises(LLMError, match="LLM_API_KEY est vide"):
        get_llm(Settings(_env_file=None, llm_provider="gemini"))


def test_get_llm_fake_and_unknown_provider() -> None:
    from app.services.llm.fake import FakeLLM

    assert isinstance(get_llm(Settings(_env_file=None, llm_provider="fake")), FakeLLM)
    with pytest.raises(ValueError, match="anthropic"):
        get_llm(Settings(_env_file=None, llm_provider="anthropic"))
