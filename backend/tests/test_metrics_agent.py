"""Tests du Metrics Agent (branche 13) avec FakeLLM, sur les datasets et des séries à la main."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from app.agents import metrics_agent
from app.agents.metrics_agent import MetricsInterpretation
from app.schemas.diagnosis import Finding
from app.schemas.enums import SignalSource
from app.schemas.telemetry import LogRecord, MetricPoint, MetricSeries, Span, TimeWindow
from app.services.llm.fake import FakeLLM

DATASETS = Path(__file__).resolve().parents[2] / "datasets"
T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=T0, end=T0 + timedelta(minutes=30))


class MetricsOnlySource:
    """DataSource minimale pour les tests (FileDataSource arrive avec la branche 7)."""

    def __init__(self, series: Sequence[MetricSeries]) -> None:
        self.series = list(series)

    @classmethod
    def from_dataset(cls, name: str) -> "MetricsOnlySource":
        raw = (DATASETS / name / "metrics.json").read_bytes()
        return cls(TypeAdapter(list[MetricSeries]).validate_json(raw))

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        return []

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        result = []
        for s in self.series:
            points = [p for p in s.points if window.contains(p.ts)]
            if (names is None or s.name in names) and points:
                result.append(s.model_copy(update={"points": points}))
        return result

    def get_traces(self, window: TimeWindow) -> list[Span]:
        return []


def fake_llm(**overrides: Any) -> FakeLLM:
    answer: dict[str, Any] = {
        "summary": "Les métriques montrent une anomalie (met-001).",
        "anomalies": ["Hausse nette (met-001)."],
        "noise_evidence_ids": [],
    }
    return FakeLLM({MetricsInterpretation: {**answer, **overrides}})


def run(name: str, llm: FakeLLM | None = None) -> Finding:
    return metrics_agent.run(MetricsOnlySource.from_dataset(name), WINDOW, llm or fake_llm())


def evidence_for(finding: Finding, label: str) -> str:
    return next(e.observation for e in finding.evidences if e.observation.startswith(label + " :"))


# --- Les scénarios -----------------------------------------------------------------------------


def test_s01_pool_is_full_and_cpu_is_stable() -> None:
    finding = run("db-pool-exhaustion")
    assert finding.agent is SignalSource.METRICS
    idle = evidence_for(finding, "db_connections_idle")
    assert "baisse anormale" in idle and "min 0," in idle
    assert "hausse anormale" in evidence_for(finding, "db_connections_active")
    assert ": stable" in evidence_for(finding, "cpu_usage")


def test_s03_pool_is_busier_but_never_empty() -> None:
    """Ce qui distingue S03 de S01 : idle baisse, mais son minimum reste au-dessus de 0."""
    finding = run("slow-query")
    idle = evidence_for(finding, "db_connections_idle")
    assert "min 0," not in idle
    assert ": stable" in evidence_for(finding, "http_request_duration_p95{route=/orders}")
    assert "hausse anormale" in evidence_for(
        finding, "http_request_duration_p95{route=/orders/search}"
    )


def test_s02_cpu_saturates_and_pool_stays_healthy() -> None:
    finding = run("cpu-overload")
    assert "hausse anormale" in evidence_for(finding, "cpu_usage")
    assert ": stable" in evidence_for(finding, "db_connections_idle")


def test_s04_dependency_goes_down() -> None:
    finding = run("dependency-down")
    assert "baisse anormale" in evidence_for(finding, "up")


def test_trap_missing_cpu_is_reported_not_invented() -> None:
    finding = run("trap-missing-metrics")
    assert finding.missing_data == ["metrics : métrique absente : cpu_usage"]
    assert not any(e.observation.startswith("cpu_usage") for e in finding.evidences)


@pytest.mark.parametrize(
    "name",
    ["db-pool-exhaustion", "slow-query", "cpu-overload", "dependency-down", "trap-missing-metrics"],
)
def test_onset_is_found_around_10_20(name: str) -> None:
    onset = metrics_agent.detect_onset(MetricsOnlySource.from_dataset(name).series)
    assert onset is not None
    assert T0 + timedelta(minutes=19) <= onset <= T0 + timedelta(minutes=22)


# --- Les chiffres viennent de Python -----------------------------------------------------------


def test_anomaly_value_is_a_real_data_point() -> None:
    finding = run("db-pool-exhaustion")
    source = MetricsOnlySource.from_dataset("db-pool-exhaustion")
    idle = next(e for e in finding.evidences if e.observation.startswith("db_connections_idle"))
    series = next(s for s in source.series if s.name == "db_connections_idle")
    assert idle.value in {p.value for p in series.points}
    assert idle.unit == "connections"


def test_evidence_order_anomalies_then_stable() -> None:
    finding = run("slow-query")
    kinds = [": stable" in e.observation for e in finding.evidences]
    assert kinds == sorted(kinds)  # toutes les anomalies avant toutes les stables
    assert [e.id for e in finding.evidences] == [
        f"met-{i:03d}" for i in range(1, len(finding.evidences) + 1)
    ]


def test_prompt_is_compact_and_lists_missing_metrics() -> None:
    llm = fake_llm()
    run("trap-missing-metrics", llm)
    (call,) = llm.calls
    assert call.schema is MetricsInterpretation
    assert '"missing_metrics": [\n  "cpu_usage"' in call.user
    assert len(call.user) < 6000


# --- Contrôle de la réponse du LLM -------------------------------------------------------------


def test_unknown_ids_are_dropped() -> None:
    llm = fake_llm(anomalies=["Vraie (met-001).", "Inventée (met-099)."])
    finding = run("db-pool-exhaustion", llm)
    assert finding.anomalies == ["Vraie (met-001)."]
    assert finding.missing_data == [
        "metrics : 1 élément(s) du LLM écarté(s), preuve citée inconnue"
    ]


def test_llm_failure_keeps_evidences_and_missing_metrics() -> None:
    finding = run("trap-missing-metrics", FakeLLM({}))
    assert finding.evidences
    assert "Métriques absentes : cpu_usage." in finding.summary
    assert finding.missing_data == [
        "metrics : métrique absente : cpu_usage",
        "metrics : interprétation LLM indisponible (LookupError)",
    ]


# --- Cas limites -------------------------------------------------------------------------------


def flat(name: str, value: float) -> MetricSeries:
    points = [MetricPoint(ts=T0 + timedelta(seconds=15 * i), value=value) for i in range(120)]
    return MetricSeries(name=name, labels={"service": "svc"}, points=points)


def test_no_metrics_at_all() -> None:
    llm = fake_llm()
    finding = metrics_agent.run(MetricsOnlySource([]), WINDOW, llm)
    assert finding.evidences == [] and llm.calls == []
    assert "metrics : métrique absente : cpu_usage" in finding.missing_data


def test_flat_metrics_mean_no_onset_and_only_stable_evidence() -> None:
    series = [flat(name, 1.0) for name in metrics_agent.EXPECTED_METRICS]
    finding = metrics_agent.run(MetricsOnlySource(series), WINDOW, fake_llm())
    assert metrics_agent.detect_onset(series) is None
    assert len(finding.evidences) == 5
    assert all("moyenne 1," in e.observation for e in finding.evidences)


def test_custom_expected_metrics() -> None:
    finding = metrics_agent.run(
        MetricsOnlySource([flat("cpu_usage", 0.3)]),
        WINDOW,
        fake_llm(),
        expected=["cpu_usage", "up"],
    )
    assert finding.missing_data == ["metrics : métrique absente : up"]


def test_huge_z_score_is_capped_in_text() -> None:
    from app.agents.metrics_agent import _fmt_z

    assert _fmt_z(12.34) == "z=+12.3"
    assert _fmt_z(5e9) == "z>1000" and _fmt_z(-5e9) == "z<-1000"
