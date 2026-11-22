"""Tests du client Groq avec un faux transport HTTP : aucun réseau, aucune vraie clé."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.services.llm.errors import LLMError, LLMRefusalError
from app.services.llm.groq import DEFAULT_GROQ_MODEL, GroqLLM
from app.services.llm.provider import get_llm


class Answer(BaseModel):
    summary: str


def groq_reply(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 8},
        },
    )


def make(*responses: httpx.Response) -> tuple[GroqLLM, list[httpx.Request], list[float]]:
    queue, requests, sleeps = list(responses), [], []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queue.pop(0)

    settings = Settings(_env_file=None, llm_provider="groq", llm_api_key="gsk-test")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GroqLLM(settings, client=client, sleep=sleeps.append), requests, sleeps


def test_valid_answer_and_request_shape() -> None:
    llm, requests, _ = make(groq_reply('{"summary": "pool saturé"}'))
    assert llm.complete_structured("Tu es un agent.", "données", Answer).summary == "pool saturé"
    request = requests[0]
    assert request.url == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer gsk-test"
    body = json.loads(request.content)
    assert body["model"] == DEFAULT_GROQ_MODEL
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert '"summary"' in body["messages"][1]["content"]  # le schéma est dans le prompt


def test_invalid_json_is_asked_again_once() -> None:
    llm, requests, _ = make(groq_reply("{}"), groq_reply('{"summary": "ok"}'))
    assert llm.complete_structured("s", "u", Answer).summary == "ok"
    assert len(requests) == 2


def test_rate_limit_is_retried() -> None:
    llm, _, sleeps = make(httpx.Response(429), groq_reply('{"summary": "ok"}'))
    assert llm.complete_structured("s", "u", Answer).summary == "ok"
    assert sleeps == [15.0]


def test_bad_key_and_content_filter() -> None:
    llm, _, _ = make(httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    with pytest.raises(LLMError, match="401"):
        llm.complete_structured("s", "u", Answer)
    llm, _, _ = make(groq_reply("", finish="content_filter"))
    with pytest.raises(LLMRefusalError):
        llm.complete_structured("s", "u", Answer)


def test_get_llm_picks_groq() -> None:
    assert isinstance(
        get_llm(Settings(_env_file=None, llm_provider="groq", llm_api_key="k")), GroqLLM
    )
    with pytest.raises(LLMError, match="clé Groq"):
        get_llm(Settings(_env_file=None, llm_provider="groq"))
