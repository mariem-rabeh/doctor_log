import pytest
from pydantic import BaseModel, Field, ValidationError

from app.services.llm.base import LLMClient
from app.services.llm.fake import FakeLLM


class Interpretation(BaseModel):
    """Schéma d'exemple, propre au test."""

    summary: str
    confidence: float = Field(ge=0, le=1)


class Other(BaseModel):
    value: int


def test_fake_llm_satisfies_llm_client_protocol() -> None:
    llm: LLMClient = FakeLLM({})  # vérifié aussi par mypy
    assert isinstance(llm, LLMClient)


def test_returns_valid_object_of_requested_schema() -> None:
    llm = FakeLLM({Interpretation: {"summary": "CPU saturé", "confidence": 0.8}})

    result = llm.complete_structured("system", "user", Interpretation)

    assert isinstance(result, Interpretation)
    assert result == Interpretation(summary="CPU saturé", confidence=0.8)


def test_invalid_canned_response_raises_validation_error() -> None:
    llm = FakeLLM({Interpretation: {"summary": "x", "confidence": 1.5}})
    with pytest.raises(ValidationError):
        llm.complete_structured("system", "user", Interpretation)


def test_unknown_schema_raises_lookup_error() -> None:
    llm = FakeLLM({Interpretation: {"summary": "x", "confidence": 0.5}})
    with pytest.raises(LookupError):
        llm.complete_structured("system", "user", Other)


def test_list_of_responses_is_consumed_in_order_then_last_repeats() -> None:
    llm = FakeLLM(
        {
            Interpretation: [
                Interpretation(summary="première", confidence=0.3),
                Interpretation(summary="révisée", confidence=0.7),
            ]
        }
    )
    summaries = [llm.complete_structured("s", "u", Interpretation).summary for _ in range(3)]
    assert summaries == ["première", "révisée", "révisée"]


def test_calls_are_recorded() -> None:
    llm = FakeLLM({Other: {"value": 1}})

    llm.complete_structured("règles", "données", Other)

    assert len(llm.calls) == 1
    assert llm.calls[0].system == "règles"
    assert llm.calls[0].user == "données"
    assert llm.calls[0].schema is Other
