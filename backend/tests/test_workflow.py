"""Tests de bout en bout du graphe (branche 17) avec FakeLLM et les vrais datasets."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.log_agent import LogInterpretation
from app.agents.metrics_agent import MetricsInterpretation
from app.agents.rca_agent import RcaAnswer
from app.agents.trace_agent import TraceInterpretation
from app.agents.verification_agent import Judgment
from app.cli import main
from app.graph.state import IncidentState
from app.graph.workflow import run_analysis
from app.schemas.enums import AgentName, AgentStatus, Conclusion, Verdict
from app.schemas.telemetry import LogRecord, TimeWindow
from app.services.llm.fake import FakeLLM
from app.tools.datasources.file_source import FileDataSource

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=T0, end=T0 + timedelta(minutes=31))


def hypo(cause: str, *ids: str) -> dict[str, Any]:
    return {
        "cause": cause,
        "rank": 1,
        "justification": "j",
        "supporting_evidence_ids": list(ids),
        "verification_actions": [f"vérifier {cause}"],
    }


def llm(*rca_answers: list[dict[str, Any]]) -> FakeLLM:
    text = {"summary": "résumé"}
    return FakeLLM(
        {
            LogInterpretation: text,
            TraceInterpretation: text,
            MetricsInterpretation: text,
            RcaAnswer: [{"hypotheses": h} for h in rca_answers],
            Judgment: {"causal": True, "comment": "lien établi"},
        }
    )


def test_s01_is_confirmed_in_one_iteration() -> None:
    updates: list[IncidentState] = []
    report = run_analysis(
        FileDataSource.from_name("db-pool-exhaustion"),
        WINDOW,
        llm([hypo("Pool de connexions saturé", "log-001", "met-005", "trc-001")]),
        incident_id="inc-1",
        on_update=updates.append,
    )
    assert report.conclusion is Conclusion.CONFIRMED
    assert report.probable_cause == "Pool de connexions saturé"
    assert report.iteration_count == 1 and report.confidence >= 0.8
    assert report.recommendations == ["vérifier Pool de connexions saturé"]
    assert {e.id[:3] for e in report.evidences} == {"log", "met", "trc"}
    assert report.documents[0].path == "pool-connexions.md"  # RAG : le bon runbook
    assert all(s is AgentStatus.COMPLETED for s in updates[-1]["agent_statuses"].values())
    assert set(updates[-1]["agent_statuses"]) == set(AgentName)


def test_s03_wrong_pool_hypothesis_is_revised() -> None:
    """La vérification contredit « pool saturé » (idle > 0) : le RCA corrige au 2e tour."""
    first = [hypo("Pool de connexions saturé", "log-001")]
    second = [hypo("Requête SQL lente sur orders", "trc-001", "log-001")]
    fake = llm(first, second)
    report = run_analysis(FileDataSource.from_name("slow-query"), WINDOW, fake, incident_id="s03")
    assert report.iteration_count == 2
    assert report.probable_cause == "Requête SQL lente sur orders"
    assert report.verification is not None and report.verification.verdict is Verdict.ACCEPTED
    rca_prompts = [c.user for c in fake.calls if c.schema is RcaAnswer]
    assert "le pool n'est jamais plein" in rca_prompts[1]


def test_revision_stops_after_two_iterations() -> None:
    wrong = [hypo("Pool de connexions saturé", "log-001")]
    report = run_analysis(
        FileDataSource.from_name("slow-query"), WINDOW, llm(wrong), incident_id="x"
    )
    assert report.iteration_count == 2
    assert report.conclusion is Conclusion.INSUFFICIENT_EVIDENCE
    assert report.probable_cause is None


class BrokenLogs(FileDataSource):
    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        raise OSError("disque illisible")


def test_failing_agent_does_not_stop_the_graph() -> None:
    updates: list[IncidentState] = []
    source = BrokenLogs(FileDataSource.from_name("db-pool-exhaustion").path)
    report = run_analysis(
        source, WINDOW, llm([hypo("Pool saturé", "trc-001")]), "x", updates.append
    )
    assert "logs : agent en échec (OSError)" in report.missing_data
    assert updates[-1]["agent_statuses"][AgentName.LOG] is AgentStatus.FAILED
    assert report.evidences  # les métriques et les traces ont quand même produit


def test_cli_prints_a_report(capsys: Any) -> None:
    assert main(["run", str(FileDataSource.from_name("slow-query").path), "--fake"]) == 0
    assert '"conclusion": "insufficient_evidence"' in capsys.readouterr().out
