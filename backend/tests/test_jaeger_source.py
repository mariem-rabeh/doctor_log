"""Tests de JaegerSource (branche 27) avec des réponses Jaeger réelles enregistrées."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.schemas.telemetry import TimeWindow
from app.tools.datasources.base import DataSource
from app.tools.datasources.jaeger_source import JaegerSource, to_spans
from app.tools.trace_tools import build_trace_tree, find_error_spans

FIXTURES = Path(__file__).parent / "fixtures" / "jaeger"
ALL_TIME = TimeWindow(start=datetime(2020, 1, 1, tzinfo=UTC), end=datetime(2100, 1, 1, tzinfo=UTC))


def load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((FIXTURES / name).read_text())  # type: ignore[no-any-return]


def recorded(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/services":
        return httpx.Response(200, json=load("services.json"))
    return httpx.Response(200, json=load(f"traces-{request.url.params['service']}.json"))


def source(handler=recorded) -> JaegerSource:  # type: ignore[no-untyped-def]
    return JaegerSource("http://jaeger:16686", httpx.Client(transport=httpx.MockTransport(handler)))


def test_is_a_datasource_without_logs_or_metrics() -> None:
    jaeger = source()
    assert isinstance(jaeger, DataSource)
    assert jaeger.get_logs(ALL_TIME) == [] and jaeger.get_metrics(ALL_TIME) == []


def test_spans_of_both_services_are_deduplicated() -> None:
    spans = source().get_traces(ALL_TIME)
    assert {s.service for s in spans} == {"orders-service", "payment-service"}
    assert len({(s.trace_id, s.span_id) for s in spans}) == len(spans)
    assert spans == sorted(spans, key=lambda s: s.start)


def test_trees_are_rebuilt_across_services() -> None:
    """Une requête /payments forme un seul arbre qui traverse les deux services."""
    trees = build_trace_tree(source().get_traces(ALL_TIME))
    crossing = [
        t
        for t in trees
        if {n.span.service for n in t.nodes} == {"orders-service", "payment-service"}
    ]
    assert crossing and all(t.orphan_count == 0 for t in crossing)


def test_simulated_payment_outage_is_an_error_origin() -> None:
    origins = {
        (e.service, e.operation)
        for e in find_error_spans(source().get_traces(ALL_TIME))
        if e.is_origin
    }
    assert any(service == "payment-service" for service, _ in origins)


def test_window_filters_spans_and_jaeger_down_gives_nothing() -> None:
    assert (
        source().get_traces(
            TimeWindow(start=datetime(2020, 1, 1, tzinfo=UTC), end=datetime(2020, 1, 2, tzinfo=UTC))
        )
        == []
    )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert source(down).get_traces(ALL_TIME) == []


def test_to_spans_converts_units_parent_and_error() -> None:
    trace = {
        "processes": {"p1": {"serviceName": "svc"}},
        "spans": [
            {
                "traceID": "t",
                "spanID": "b",
                "operationName": "child",
                "processID": "p1",
                "references": [{"refType": "CHILD_OF", "spanID": "a"}],
                "startTime": 1_700_000_000_000_000,
                "duration": 2500,
                "tags": [
                    {"key": "error", "value": True},
                    {"key": "http.status_code", "value": 503},
                ],
            }
        ],
    }
    (span,) = to_spans(trace)
    assert span.parent_span_id == "a" and span.duration_ms == 2.5 and span.status == "error"
    assert span.start == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC) + timedelta(0)
    assert span.attributes["http.status_code"] == 503
