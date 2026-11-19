from collections.abc import Sequence
from datetime import UTC, datetime

from app.schemas.telemetry import LogRecord, MetricPoint, MetricSeries, Span, TimeWindow
from app.tools.datasources.base import DataSource

WINDOW = TimeWindow(
    start=datetime(2026, 11, 17, 10, 0, tzinfo=UTC),
    end=datetime(2026, 11, 17, 10, 30, tzinfo=UTC),
)


class InMemorySource:
    """Source minimale écrite pour le test : elle n'hérite pas de DataSource."""

    def __init__(self, series: list[MetricSeries]) -> None:
        self._series = series

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        return []

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        return [s for s in self._series if names is None or s.name in names]

    def get_traces(self, window: TimeWindow) -> list[Span]:
        return []


class IncompleteSource:
    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        return []


def _cpu_series() -> MetricSeries:
    return MetricSeries(
        name="cpu_usage_percent",
        points=[MetricPoint(ts=datetime(2026, 11, 17, 10, 15, tzinfo=UTC), value=97.0)],
    )


def test_class_with_the_three_methods_is_a_datasource() -> None:
    source: DataSource = InMemorySource([_cpu_series()])  # vérifié aussi par mypy
    assert isinstance(source, DataSource)


def test_class_missing_methods_is_not_a_datasource() -> None:
    assert not isinstance(IncompleteSource(), DataSource)


def test_missing_metric_is_absent_not_invented() -> None:
    source: DataSource = InMemorySource([_cpu_series()])
    result = source.get_metrics(WINDOW, ["cpu_usage_percent", "db_connections_active"])
    assert [s.name for s in result] == ["cpu_usage_percent"]
