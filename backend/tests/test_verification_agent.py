"""Tests du Verification Agent (branche 16) sur les vrais datasets, avec FakeLLM."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents import verification_agent as va
from app.schemas.diagnosis import Evidence, Finding, Hypothesis
from app.schemas.enums import Verdict
from app.schemas.telemetry import TimeWindow
from app.services.llm.fake import FakeLLM
from app.tools.datasources.file_source import FileDataSource

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=T0, end=T0 + timedelta(minutes=30))
CAUSAL = FakeLLM({va.Judgment: {"causal": True, "comment": "ok", "to_verify": ["taille du pool"]}})


def ev(eid: str, source: str, observation: str, value: Any, minutes: int = 25) -> Evidence:
    ts = T0 + timedelta(minutes=minutes)
    return Evidence(
        id=eid,
        source=source,
        service="orders-service",
        period_start=ts,
        period_end=ts,
        observation=observation,
        value=value,
    )


FINDINGS = [
    Finding(agent="logs", summary="s", evidences=[ev("log-001", "logs", "QueuePool", 12)]),
    Finding(
        agent="metrics",
        summary="s",
        evidences=[ev("met-001", "metrics", "db_connections_idle : baisse", 0)],
    ),
    Finding(
        agent="traces",
        summary="s",
        evidences=[ev("trc-001", "traces", "db.acquire_connection", 5000)],
    ),
]


def hypothesis(cause: str, *ids: str) -> Hypothesis:
    return Hypothesis(cause=cause, rank=1, justification="j", supporting_evidence_ids=list(ids))


def verify(
    dataset: str, hypo: Hypothesis, findings: list[Finding] = FINDINGS, llm: FakeLLM = CAUSAL
) -> Any:
    return va.run([hypo], findings, FileDataSource.from_name(dataset), WINDOW, llm)


def test_well_supported_hypothesis_is_accepted() -> None:
    result = verify(
        "db-pool-exhaustion", hypothesis("Pool saturé", "log-001", "met-001", "trc-001")
    )
    assert result.verdict is Verdict.ACCEPTED
    assert result.confidence == 1.0  # 0,5 + 0,3 causal + 0,2 (trois sources)
    assert result.issues == [] and result.to_verify == ["taille du pool"]


def test_pool_hypothesis_is_contradicted_on_slow_query() -> None:
    """Sur S03, idle ne tombe jamais à 0 : « pool saturé » doit être renvoyé au RCA."""
    result = verify("slow-query", hypothesis("Pool de connexions saturé", "log-001", "trc-001"))
    assert result.verdict is Verdict.REVISION_REQUESTED
    assert any("le pool n'est jamais plein" in i for i in result.issues)


def test_cpu_hypothesis_is_contradicted_when_cpu_is_low() -> None:
    result = verify("db-pool-exhaustion", hypothesis("Saturation CPU", "log-001"))
    assert any("CPU n'est pas saturé" in i for i in result.issues)


def test_invented_value_and_unknown_evidence_are_rejected() -> None:
    findings = [
        Finding(
            agent="metrics",
            summary="s",
            evidences=[ev("met-001", "metrics", "db_connections_idle : x", 999)],
        )
    ]
    result = verify("db-pool-exhaustion", hypothesis("x", "met-001", "trc-777"), findings)
    assert result.verdict is Verdict.REVISION_REQUESTED
    assert "met-001 : valeur 999.0 absente des données" in result.issues
    assert "trc-777 : preuve inconnue" in result.issues


def test_evidence_outside_the_window_is_reported() -> None:
    findings = [
        Finding(agent="logs", summary="s", evidences=[ev("log-001", "logs", "x", 1, minutes=45)])
    ]
    result = verify("db-pool-exhaustion", hypothesis("x", "log-001"), findings)
    assert "log-001 : période hors de la fenêtre analysée" in result.issues


def test_correlation_with_missing_data_is_insufficient() -> None:
    llm = FakeLLM({va.Judgment: {"causal": False, "comment": "corrélation"}})
    findings = [
        f.model_copy(update={"missing_data": ["metrics : métrique absente : cpu_usage"]})
        for f in FINDINGS[:1]
    ]
    result = verify(
        "trap-missing-metrics", hypothesis("Event loop bloquée", "log-001"), findings, llm
    )
    assert result.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert result.confidence < 0.5


def test_no_hypothesis_or_llm_down() -> None:
    empty = va.run([], FINDINGS, FileDataSource.from_name("slow-query"), WINDOW, CAUSAL)
    assert empty.verdict is Verdict.INSUFFICIENT_EVIDENCE and empty.confidence == 0
    down = verify("db-pool-exhaustion", hypothesis("Pool saturé", "log-001"), llm=FakeLLM({}))
    assert down.to_verify == ["jugement du LLM indisponible"]
    assert down.confidence == 0.5
