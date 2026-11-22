"""Source de traces live : interroge l'API HTTP de Jaeger et renvoie des Span du contrat.

Pour chaque service connu de Jaeger : /api/traces?service=...&start=...&end=... (microsecondes).
Une même trace peut revenir pour plusieurs services : les spans sont dédoublonnés.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.schemas.telemetry import LogRecord, MetricSeries, Span, TimeWindow

logger = logging.getLogger(__name__)

MAX_TRACES_PER_SERVICE = 500
IGNORED_SERVICES = {"jaeger-all-in-one", "jaeger"}


class JaegerSource:
    """Traces depuis Jaeger. Les logs et les métriques viennent d'autres sources."""

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15)

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        return []

    def get_metrics(self, window: TimeWindow, names: object = None) -> list[MetricSeries]:
        return []

    def get_traces(self, window: TimeWindow) -> list[Span]:
        spans: dict[tuple[str, str], Span] = {}
        for service in self._get("/api/services") or []:
            if service in IGNORED_SERVICES:
                continue
            params = {
                "service": service,
                "start": int(window.start.timestamp() * 1_000_000),
                "end": int(window.end.timestamp() * 1_000_000),
                "limit": MAX_TRACES_PER_SERVICE,
            }
            for trace in self._get("/api/traces", params) or []:
                for span in to_spans(trace):
                    if window.contains(span.start):
                        spans[(span.trace_id, span.span_id)] = span
        return sorted(spans.values(), key=lambda s: s.start)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:  # Jaeger absent : pas de traces, l'agent le signalera
            logger.warning("Jaeger : %s indisponible (%s)", path, type(exc).__name__)
            return None
        return response.json().get("data")


def to_spans(trace: dict[str, Any]) -> list[Span]:
    """Une trace au format Jaeger -> spans du contrat."""
    processes = trace.get("processes", {})
    result = []
    for raw in trace.get("spans", []):
        tags = {t["key"]: t["value"] for t in raw.get("tags", [])}
        parent = next(
            (r["spanID"] for r in raw.get("references", []) if r.get("refType") == "CHILD_OF"),
            None,
        )
        is_error = tags.get("error") is True or tags.get("otel.status_code") == "ERROR"
        result.append(
            Span(
                trace_id=raw["traceID"],
                span_id=raw["spanID"],
                parent_span_id=parent,
                service=processes.get(raw["processID"], {}).get("serviceName", "unknown"),
                operation=raw["operationName"],
                start=datetime.fromtimestamp(raw["startTime"] / 1_000_000, UTC),
                duration_ms=raw["duration"] / 1000,
                status="error" if is_error else "ok",
                attributes={
                    k: v for k, v in tags.items() if isinstance(v, str | int | float | bool)
                },
            )
        )
    return result
