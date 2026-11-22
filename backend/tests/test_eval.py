"""Tests du calcul des scores de l'évaluation (branche 22), sans LLM."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas.diagnosis import DiagnosisReport, Evidence, Hypothesis
from eval.run_eval import main, score_run, summarize

T = datetime(2026, 11, 18, 10, 25, tzinfo=UTC)
S01 = {
    "cause_keywords": ["pool", "connection"],
    "must_not_conclude": ["slow_query", "cpu_saturation"],
    "key_evidence": [{"source": "logs", "signal": "QueuePool limit"}],
}
TRAP = {"acceptable_conclusions": ["insufficient_evidence", "probable"], "max_confidence": 0.6}


def report(
    cause: str | None,
    conclusion: str = "confirmed",
    confidence: float = 0.9,
    cited: tuple[str, ...] = ("log-001",),
) -> DiagnosisReport:
    evidence = Evidence(
        id="log-001",
        source="logs",
        service="orders-service",
        period_start=T,
        period_end=T,
        observation="WARNING « QueuePool limit reached »",
        value=12,
    )
    hypothesis = Hypothesis(
        cause=cause or "x", rank=1, justification="j", supporting_evidence_ids=list(cited)
    )
    return DiagnosisReport(
        incident_id="x",
        conclusion=conclusion,
        probable_cause=cause,
        confidence=confidence,
        hypotheses=[hypothesis],
        evidences=[evidence],
        verification=None,
        iteration_count=1,
        generated_at=T,
    )


def test_right_cause_is_correct() -> None:
    score = score_run(report("Pool de connexions saturé"), S01, 42.0)
    assert score["correct"] and score["grounding"] == 1.0 and score["key_evidence"] == 1.0
    assert score["seconds"] == 42.0


def test_confusion_with_a_forbidden_cause_is_wrong() -> None:
    score = score_run(report("Pool saturé à cause d'une requête lente sans index"), S01, 1)
    assert not score["correct"] and score["confusions"] == ["slow_query"]


def test_no_cause_is_wrong_and_invented_ids_lower_grounding() -> None:
    assert not score_run(report(None, conclusion="insufficient_evidence"), S01, 1)["correct"]
    score = score_run(report("pool", cited=("log-001", "met-999")), S01, 1)
    assert score["grounding"] == 0.5


def test_trap_requires_a_cautious_conclusion() -> None:
    assert score_run(report(None, "insufficient_evidence", 0.3), TRAP, 1)["correct"]
    assert not score_run(report("CPU", "confirmed", 0.9), TRAP, 1)["correct"]
    assert not score_run(report("CPU", "probable", 0.8), TRAP, 1)["correct"]  # trop confiant


def test_summarize_accuracy_and_consistency() -> None:
    runs: list[dict[str, Any]] = [
        {
            "correct": c,
            "grounding": 1.0,
            "key_evidence": 1.0,
            "seconds": 10,
            "conclusion": "confirmed",
        }
        for c in (True, True, False)
    ]
    summary = summarize(runs)
    assert round(summary["accuracy"], 2) == 0.67 and round(summary["consistency"], 2) == 0.67
    assert summary["conclusions"] == {"confirmed": 3}


def test_fake_run_writes_both_files(tmp_path: Path) -> None:
    assert main(["--fake", "--runs", "1", "--out", str(tmp_path), "slow-query"]) == 0
    assert "| slow-query | 1 |" in (tmp_path / "results.md").read_text(encoding="utf-8")
    assert (tmp_path / "results.json").exists()
