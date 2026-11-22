"""Tests de LiveDataSource et des incidents live (branche 28)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.schemas.telemetry import TimeWindow
from app.tools.datasources.base import DataSource
from app.tools.datasources.jaeger_source import JaegerSource
from app.tools.datasources.live_source import LiveDataSource
from app.tools.datasources.prometheus_source import PrometheusSource
from tests.test_jaeger_source import recorded as jaeger_recorded
from tests.test_prometheus_source import recorded as prometheus_recorded

ALL_TIME = TimeWindow(start=datetime(2020, 1, 1, tzinfo=UTC), end=datetime(2100, 1, 1, tzinfo=UTC))


def live(logs_dir: Path) -> LiveDataSource:
    def client(handler):  # type: ignore[no-untyped-def]
        return httpx.Client(transport=httpx.MockTransport(handler))

    return LiveDataSource(
        logs_dir,
        PrometheusSource("http://prom:9090", client(prometheus_recorded)),
        JaegerSource("http://jaeger:16686", client(jaeger_recorded)),
    )


def log_line(minute: int, message: str, service: str = "orders-service") -> str:
    ts = datetime(2026, 11, 22, 10, minute, tzinfo=UTC).isoformat()
    return json.dumps(
        {
            "timestamp": ts,
            "level": "ERROR",
            "service": service,
            "message": message,
            "trace_id": "t",
            "span_id": "s",
            "exception": None,
        }
    )


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    (tmp_path / "orders-service.log").write_text(
        "\n".join([log_line(5, "b"), "{ligne cassée", log_line(1, "a")]), encoding="utf-8"
    )
    (tmp_path / "payment-service.log").write_text(
        log_line(3, "c", "payment-service"), encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("ignoré", encoding="utf-8")
    return tmp_path


def test_is_a_datasource_combining_the_three_signals(logs_dir: Path) -> None:
    source = live(logs_dir)
    assert isinstance(source, DataSource)
    assert source.get_metrics(ALL_TIME) and source.get_traces(ALL_TIME)


def test_logs_from_all_files_sorted_and_invalid_lines_skipped(logs_dir: Path) -> None:
    logs = live(logs_dir).get_logs(ALL_TIME)
    assert [log.message for log in logs] == ["a", "c", "b"]
    assert logs[0].span_id == "s"


def test_logs_are_filtered_on_the_window(logs_dir: Path) -> None:
    start = datetime(2026, 11, 22, 10, 2, tzinfo=UTC)
    window = TimeWindow(start=start, end=start + timedelta(minutes=2))
    assert [log.message for log in live(logs_dir).get_logs(window)] == ["c"]


def test_no_logs_directory_gives_no_logs(tmp_path: Path) -> None:
    assert live(tmp_path / "absent").get_logs(ALL_TIME) == []
