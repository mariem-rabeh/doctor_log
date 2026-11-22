"""Tests de PrometheusSource (branche 26) avec des réponses Prometheus réelles enregistrées."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.schemas.telemetry import TimeWindow
from app.tools.datasources.base import DataSource
from app.tools.datasources.prometheus_source import QUERIES, PrometheusSource, to_series

FIXTURES = Path(__file__).parent / "fixtures" / "prometheus"
NOW = datetime.now(UTC)
WINDOW = TimeWindow(start=NOW - timedelta(minutes=5), end=NOW)


def recorded(request: httpx.Request) -> httpx.Response:
    """Rejoue la réponse enregistrée qui correspond à la requête PromQL demandée."""
    query = request.url.params["query"]
    name = next(n for n, q in QUERIES.items() if q == query)
    return httpx.Response(200, json=json.loads((FIXTURES / f"{name}.json").read_text()))


def source(handler=recorded) -> PrometheusSource:  # type: ignore[no-untyped-def]
    return PrometheusSource(
        "http://prom:9090", httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_is_a_datasource_without_logs_or_traces() -> None:
    prom = source()
    assert isinstance(prom, DataSource)
    assert prom.get_logs(WINDOW) == [] and prom.get_traces(WINDOW) == []


def test_all_contract_metrics_are_returned() -> None:
    names = {s.name for s in source().get_metrics(WINDOW)}
    assert names == set(QUERIES)


def test_labels_keep_service_and_route_only() -> None:
    p95 = [s for s in source().get_metrics(WINDOW, ["http_request_duration_p95"])]
    assert {s.labels["route"] for s in p95} >= {"/orders", "/payments"}
    assert all(set(s.labels) == {"route", "service"} for s in p95)


def test_real_pool_saturation_is_visible() -> None:
    (active,) = source().get_metrics(WINDOW, ["db_connections_active"])
    assert active.labels == {"service": "orders-service"}
    assert max(p.value for p in active.points) == 5  # pool saturé pendant la capture


def test_unknown_metric_is_absent() -> None:
    assert source().get_metrics(WINDOW, ["does_not_exist"]) == []


def test_prometheus_down_gives_no_metric_instead_of_crashing() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert source(down).get_metrics(WINDOW) == []


def test_nan_values_are_skipped() -> None:
    item = {
        "metric": {"__name__": "x", "job": "j"},
        "values": [[1, "NaN"], [2, "0.5"], [3, "+Inf"]],
    }
    series = to_series("error_rate", item)
    assert series.labels == {} and [p.value for p in series.points] == [0.5]
