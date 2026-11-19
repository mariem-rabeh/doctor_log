from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.telemetry import LogRecord, MetricSeries, Span, TimeWindow


def test_log_record_valid_and_converted_to_utc() -> None:
    log = LogRecord.model_validate(
        {
            "timestamp": "2026-11-17T11:15:00+01:00",
            "level": "ERROR",
            "service": "orders",
            "message": "Timeout acquiring DB connection",
            "trace_id": "abc123",
        }
    )
    assert log.timestamp == datetime(2026, 11, 17, 10, 15, tzinfo=UTC)
    assert log.exception is None


def test_timestamp_without_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogRecord.model_validate(
            {"timestamp": "2026-11-17T10:15:00", "level": "INFO", "service": "s", "message": "m"}
        )


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogRecord.model_validate(
            {
                "timestamp": "2026-11-17T10:15:00Z",
                "level": "INFO",
                "service": "s",
                "message": "m",
                "servcie": "typo",
            }
        )


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogRecord.model_validate(
            {"timestamp": "2026-11-17T10:15:00Z", "level": "FATAL", "service": "s", "message": "m"}
        )


def test_metric_series_valid() -> None:
    series = MetricSeries.model_validate(
        {
            "name": "cpu_usage_percent",
            "labels": {"service": "orders"},
            "points": [
                {"ts": "2026-11-17T10:00:00Z", "value": 35.0},
                {"ts": "2026-11-17T10:01:00Z", "value": 97.5},
            ],
        }
    )
    assert [p.value for p in series.points] == [35.0, 97.5]


def test_span_valid_and_negative_duration_rejected() -> None:
    data = {
        "trace_id": "t1",
        "span_id": "s1",
        "service": "orders",
        "operation": "GET /orders",
        "start": "2026-11-17T10:15:00Z",
        "duration_ms": 120.5,
        "attributes": {"http.status_code": 200},
    }
    span = Span.model_validate(data)
    assert span.parent_span_id is None
    assert span.status == "ok"

    with pytest.raises(ValidationError):
        Span.model_validate({**data, "duration_ms": -1})


def test_time_window_order_and_contains() -> None:
    window = TimeWindow.model_validate(
        {"start": "2026-11-17T10:00:00Z", "end": "2026-11-17T10:30:00Z"}
    )
    assert window.contains(datetime(2026, 11, 17, 10, 15, tzinfo=UTC))
    assert not window.contains(datetime(2026, 11, 17, 10, 31, tzinfo=UTC))

    with pytest.raises(ValidationError):
        TimeWindow.model_validate({"start": "2026-11-17T10:30:00Z", "end": "2026-11-17T10:00:00Z"})
