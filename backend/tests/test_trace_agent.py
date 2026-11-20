"""Tests du Trace Agent (branche 12) avec FakeLLM, sur les datasets et des traces à la main."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from app.agents import trace_agent
from app.agents.common import detect_change_point
from app.agents.log_agent import LogInterpretation
from app.agents.trace_agent import TraceInterpretation
from app.schemas.enums import SignalSource
from app.schemas.telemetry import LogRecord, MetricSeries, Span, TimeWindow
from app.services.llm.fake import FakeLLM
from app.tools.trace_tools import build_trace_tree

DATASETS = Path(__file__).resolve().parents[2] / "datasets"
T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=T0, end=T0 + timedelta(minutes=30))


class TracesOnlySource:
    """DataSource minimale pour les tests (la vraie FileDataSource arrive en branche 7)."""

    def __init__(self, spans: Sequence[Span]) -> None:
        self.spans = list(spans)

    @classmethod
    def from_dataset(cls, name: str) -> "TracesOnlySource":
        raw = (DATASETS / name / "traces.json").read_bytes()
        return cls(TypeAdapter(list[Span]).validate_json(raw))

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        return []

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        return []

    def get_traces(self, window: TimeWindow) -> list[Span]:
        return [s for s in self.spans if window.contains(s.start)]


def fake_llm(**overrides: Any) -> FakeLLM:
    answer: dict[str, Any] = {
        "summary": "Le temps se concentre dans une opération (trc-001).",
        "anomalies": ["Opération dégradée (trc-001)."],
        "noise_evidence_ids": [],
    }
    return FakeLLM({TraceInterpretation: {**answer, **overrides}})


def run(name: str, llm: FakeLLM | None = None) -> Any:
    return trace_agent.run(TracesOnlySource.from_dataset(name), WINDOW, llm or fake_llm())


# --- Les trois scénarios : le vrai coupable passe en premier -----------------------------------


@pytest.mark.parametrize(
    ("name", "service", "operation"),
    [
        ("db-pool-exhaustion", "orders-service", "db.acquire_connection"),
        ("slow-query", "postgres", "SELECT orders"),
        ("trap-misleading-logs", "inventory-service", "GET /stock"),
    ],
)
def test_real_culprit_is_the_first_evidence(name: str, service: str, operation: str) -> None:
    finding = run(name)
    first = finding.evidences[0]
    assert finding.agent is SignalSource.TRACES
    assert first.id == "trc-001"
    assert first.service == service
    assert first.observation.startswith(operation)
    assert first.unit == "ms" and float(first.value) > 4000


def test_parents_that_only_wait_are_not_accused() -> None:
    """Sur S03, la gateway et le handler durent 5 s mais ne font qu'attendre le SELECT."""
    degraded = [
        e.observation.split(" :")[0]
        for e in run("slow-query").evidences
        if "temps propre" in e.observation
    ]
    assert degraded == ["SELECT orders"]


def test_s01_and_s03_error_origins_differ() -> None:
    s01 = [e for e in run("db-pool-exhaustion").evidences if e.unit == "error spans"]
    s03 = [e for e in run("slow-query").evidences if e.unit == "error spans"]
    assert "db.acquire_connection (TimeoutError)" in s01[0].observation
    assert "SELECT orders (QueryCanceled)" in s03[0].observation


def test_trap_has_no_error_evidence_but_onset_is_found() -> None:
    finding = run("trap-misleading-logs")
    assert not any(e.unit == "error spans" for e in finding.evidences)
    onset = trace_agent.detect_onset(
        build_trace_tree(TracesOnlySource.from_dataset("trap-misleading-logs").spans), WINDOW
    )
    assert onset is not None and T0 + timedelta(minutes=19) <= onset <= T0 + timedelta(minutes=23)


def test_end_to_end_latency_is_the_last_evidence() -> None:
    last = run("db-pool-exhaustion").evidences[-1]
    assert last.service == "api-gateway"
    assert last.observation.startswith("Latence de bout en bout p95")


# --- Les chiffres viennent de Python -----------------------------------------------------------


def test_evidence_value_is_the_real_p95() -> None:
    from app.tools.trace_tools import percentile

    finding = run("slow-query")
    onset = trace_agent.detect_onset(
        build_trace_tree(TracesOnlySource.from_dataset("slow-query").spans), WINDOW
    )
    assert onset is not None
    durations = [
        s.duration_ms
        for s in TracesOnlySource.from_dataset("slow-query").spans
        if s.operation == "SELECT orders" and s.start >= onset
    ]
    # SELECT n'a pas d'enfant : son temps propre est sa durée.
    assert finding.evidences[0].value == round(percentile(durations, 95), 1)


def test_prompt_is_compact_and_has_example_trace_ids() -> None:
    llm = fake_llm()
    run("db-pool-exhaustion", llm)
    (call,) = llm.calls
    assert call.schema is TraceInterpretation
    assert "<telemetry>" in call.user and "example_trace_ids" in call.user
    assert len(call.user) < 6000


# --- Contrôle de la réponse du LLM -------------------------------------------------------------


def test_unknown_ids_are_dropped_and_noise_kept() -> None:
    llm = fake_llm(
        anomalies=["Vraie (trc-001).", "Inventée (trc-042)."],
        noise_evidence_ids=["trc-002", "log-001"],
    )
    finding = run("db-pool-exhaustion", llm)
    assert "Vraie (trc-001)." in finding.anomalies
    assert not any("trc-042" in a for a in finding.anomalies)
    assert any("sans rapport" in a and "trc-002" in a for a in finding.anomalies)
    assert finding.missing_data == ["traces : 2 élément(s) du LLM écarté(s), preuve citée inconnue"]


def test_llm_failure_still_returns_the_evidences() -> None:
    finding = run("slow-query", FakeLLM({}))
    assert finding.evidences[0].service == "postgres"
    assert finding.summary.startswith("60 traces analysées ; début détecté à 10:21 UTC")
    assert finding.missing_data == ["traces : interprétation LLM indisponible (LookupError)"]


def test_each_agent_has_its_own_llm_schema() -> None:
    """FakeLLM répond selon la classe : le graphe pourra donner une réponse par agent."""
    assert TraceInterpretation is not LogInterpretation
    llm = FakeLLM({LogInterpretation: {"summary": "logs"}})  # pas de réponse pour les traces
    finding = run("slow-query", llm)
    assert "interprétation LLM indisponible" in finding.missing_data[0]


# --- Cas limites -------------------------------------------------------------------------------


def span(span_id: str, parent: str | None, op: str, start_s: float, ms: float, trace: str) -> Span:
    return Span(
        trace_id=trace,
        span_id=span_id,
        parent_span_id=parent,
        service="svc",
        operation=op,
        start=T0 + timedelta(seconds=start_s),
        duration_ms=ms,
    )


def test_no_traces() -> None:
    llm = fake_llm()
    finding = trace_agent.run(TracesOnlySource([]), WINDOW, llm)
    assert finding.missing_data == ["traces : aucune trace dans la fenêtre"]
    assert llm.calls == []


def test_orphan_spans_are_reported_as_missing_data() -> None:
    spans = [span("a", None, "root", i * 60, 20, f"t{i}") for i in range(10)]
    spans.append(span("x", "lost-parent", "child", 30, 5, "t0"))
    finding = trace_agent.run(TracesOnlySource(spans), WINDOW, fake_llm())
    assert "traces : 1 span(s) sans parent, traces incomplètes" in finding.missing_data


def test_stable_latency_means_no_onset_and_no_degradation() -> None:
    spans = [span("a", None, "GET /x", i * 30, 40, f"t{i}") for i in range(60)]
    finding = trace_agent.run(TracesOnlySource(spans), WINDOW, fake_llm())
    assert trace_agent.detect_onset(build_trace_tree(spans), WINDOW) is None
    assert [e.observation for e in finding.evidences] == [
        "Latence de bout en bout p95 : 40 ms (60 traces)"
    ]


def test_small_absolute_increase_is_not_a_degradation() -> None:
    """2 ms -> 7 ms est un x3 mais pas un problème : il faut aussi +100 ms."""
    spans = []
    for i in range(60):
        late = i >= 40
        spans += [
            span(f"r{i}", None, "GET /x", i * 30, 3000 if late else 50, f"t{i}"),
            span(f"c{i}", f"r{i}", "tiny", i * 30, 7 if late else 2, f"t{i}"),
        ]
    finding = trace_agent.run(TracesOnlySource(spans), WINDOW, fake_llm())
    degraded = [e.observation for e in finding.evidences if "temps propre" in e.observation]
    assert len(degraded) == 1 and degraded[0].startswith("GET /x")


def test_detect_change_point_shared_helper() -> None:
    assert detect_change_point([1, 1, 1, 1, 9, 9, 9]) == 4
    assert detect_change_point([5, 5, 5, 5, 5]) is None
    assert detect_change_point([1, 2]) is None  # trop court pour deux segments
