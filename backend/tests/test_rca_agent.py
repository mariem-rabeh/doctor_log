"""Tests du RCA Agent (branche 15) avec des findings fixes et FakeLLM."""

from datetime import UTC, datetime
from typing import Any

from app.agents import rca_agent
from app.agents.rca_agent import RcaAnswer
from app.schemas.diagnosis import Evidence, Finding
from app.services.llm.fake import FakeLLM

T = datetime(2026, 11, 18, 10, 25, tzinfo=UTC)


def evidence(eid: str, source: str) -> Evidence:
    return Evidence(
        id=eid,
        source=source,
        service="orders-service",
        period_start=T,
        period_end=T,
        observation="x",
        value=1,
    )


FINDINGS = [
    Finding(agent="logs", summary="s", evidences=[evidence("log-001", "logs")]),
    Finding(agent="metrics", summary="s", evidences=[evidence("met-001", "metrics")]),
    Finding(agent="traces", summary="s", evidences=[evidence("trc-001", "traces")]),
]


def hypo(rank: int, *ids: str, against: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "cause": f"cause {rank}",
        "rank": rank,
        "justification": "j",
        "supporting_evidence_ids": list(ids),
        "contradicting_evidence_ids": list(against),
    }


def test_valid_hypotheses_are_kept_and_ranked() -> None:
    llm = FakeLLM({RcaAnswer: {"hypotheses": [hypo(2, "met-001"), hypo(1, "log-001", "trc-001")]}})
    result = rca_agent.run(FINDINGS, llm)
    assert [(h.rank, h.cause) for h in result] == [(1, "cause 1"), (2, "cause 2")]


def test_hypothesis_citing_unknown_evidence_is_rejected() -> None:
    answer = {
        "hypotheses": [
            hypo(1, "log-999"),
            hypo(2, "met-001", against=("trc-404",)),
            hypo(3, "trc-001"),
        ]
    }
    result = rca_agent.run(FINDINGS, FakeLLM({RcaAnswer: answer}))
    assert [(h.rank, h.cause) for h in result] == [(1, "cause 3")]


def test_feedback_and_evidences_are_sent_to_the_llm() -> None:
    llm = FakeLLM({RcaAnswer: {"hypotheses": [hypo(1, "log-001")]}})
    rca_agent.run(FINDINGS, llm, feedback=["met-001 contredit la saturation du pool"])
    prompt = llm.calls[0].user
    assert "<telemetry>" in prompt and "trc-001" in prompt
    assert "met-001 contredit la saturation du pool" in prompt


def test_llm_failure_or_no_evidence_gives_no_hypothesis() -> None:
    assert rca_agent.run(FINDINGS, FakeLLM({})) == []
    llm = FakeLLM({RcaAnswer: {"hypotheses": []}})
    assert rca_agent.run([Finding(agent="logs", summary="rien")], llm) == []
    assert llm.calls == []
