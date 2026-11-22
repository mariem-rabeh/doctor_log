"""Source de métriques live : interroge Prometheus (query_range) et renvoie des MetricSeries.

Chaque nom de métrique du contrat correspond à une requête PromQL sur les métriques exposées
par la demo-app. Une métrique sans donnée est absente du résultat (jamais inventée).
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from app.schemas.telemetry import LogRecord, MetricPoint, MetricSeries, Span, TimeWindow

logger = logging.getLogger(__name__)

STEP_SECONDS = 15
QUERIES = {
    "http_request_duration_p95": (
        "histogram_quantile(0.95, sum by (le, route, service) "
        "(rate(http_request_duration_seconds_bucket[1m])))"
    ),
    "error_rate": (
        'sum by (service) (rate(http_requests_total{status=~"5.."}[1m])) '
        "/ sum by (service) (rate(http_requests_total[1m]))"
    ),
    "db_connections_active": "db_connections_active",
    "db_connections_idle": "db_connections_idle",
    "cpu_usage": "cpu_usage",
    "up": "up",
}
IGNORED_LABELS = {"__name__", "instance", "job"}


class PrometheusSource:
    """Métriques depuis Prometheus. Les logs et les traces viennent d'autres sources."""

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15)

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        return []

    def get_traces(self, window: TimeWindow) -> list[Span]:
        return []

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        result: list[MetricSeries] = []
        for name in names or list(QUERIES):
            query = QUERIES.get(name)
            if query is None:
                continue
            result += [s for s in self._query_range(name, query, window) if s.points]
        return result

    def _query_range(self, name: str, query: str, window: TimeWindow) -> list[MetricSeries]:
        params: dict[str, Any] = {
            "query": query,
            "start": window.start.timestamp(),
            "end": window.end.timestamp(),
            "step": STEP_SECONDS,
        }
        try:
            response = self._client.get(f"{self.base_url}/api/v1/query_range", params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # Prometheus absent : la métrique sera signalée manquante
            logger.warning("Prometheus : %s indisponible (%s)", name, type(exc).__name__)
            return []
        return [to_series(name, item) for item in response.json()["data"]["result"]]


def to_series(name: str, item: dict[str, Any]) -> MetricSeries:
    """Un résultat de matrice Prometheus -> MetricSeries (les NaN et infinis sont ignorés)."""
    labels = {k: v for k, v in item["metric"].items() if k not in IGNORED_LABELS}
    points = []
    for ts, raw in item["values"]:
        value = float(raw)
        if value == value and abs(value) != float("inf"):  # écarte NaN et ±Inf
            points.append(MetricPoint(ts=datetime.fromtimestamp(float(ts), UTC), value=value))
    return MetricSeries(name=name, labels=labels, points=points)
