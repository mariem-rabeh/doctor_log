"""Tests unitaires des outils de logs (branche 5), sur de petites listes écrites à la main."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.schemas.telemetry import LogLevel, LogRecord
from app.tools.log_tools import (
    count_errors,
    exception_type,
    extract_exceptions,
    floor_time,
    normalize_message,
    parse_logs,
)

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def log(
    seconds: float,
    message: str,
    *,
    level: LogLevel = "ERROR",
    service: str = "orders-service",
    trace_id: str | None = None,
    exception: str | None = None,
) -> LogRecord:
    return LogRecord(
        timestamp=T0 + timedelta(seconds=seconds),
        level=level,
        service=service,
        message=message,
        trace_id=trace_id,
        exception=exception,
    )


# --- parse_logs ------------------------------------------------------------------------------


def test_parse_logs_keeps_valid_lines_and_reports_invalid_ones() -> None:
    lines = [
        '{"timestamp": "2026-11-18T10:00:05Z", "level": "INFO", "service": "a", "message": "b"}',
        "{pas du json",
        '{"timestamp": "2026-11-18T10:00:01Z", "level": "FATAL", "service": "a", "message": "x"}',
        "",
        '{"timestamp": "2026-11-18T10:00:02Z", "level": "ERROR", "service": "a", "message": "c"}',
    ]
    result = parse_logs(lines)

    assert [r.message for r in result.records] == ["c", "b"]  # trié par timestamp
    assert result.rejected == 2  # la ligne vide n'est pas un rejet
    assert result.errors[0].startswith("ligne 2 : JSON invalide")
    assert result.errors[1].startswith("ligne 3 : level")


def test_parse_logs_accepts_dicts() -> None:
    data = {"timestamp": "2026-11-18T10:00:00Z", "level": "INFO", "service": "a", "message": "m"}
    assert parse_logs([data]).records[0].service == "a"


def test_parse_logs_caps_reported_errors() -> None:
    result = parse_logs(["{"] * 50)
    assert result.rejected == 50
    assert len(result.errors) == 20


def test_parse_logs_roundtrip_from_log_record() -> None:
    original = log(3, "Database connection timeout", trace_id="abc")
    assert parse_logs([original.model_dump_json()]).records == [original]


# --- count_errors ----------------------------------------------------------------------------


def test_count_errors_groups_by_minute_and_service() -> None:
    records = [
        log(5, "x"),
        log(50, "y"),
        log(65, "z"),
        log(70, "w", service="api-gateway"),
        log(80, "ignored", level="WARNING"),
        log(90, "ignored", level="INFO"),
    ]
    counts = count_errors(records)

    assert [(c.bucket_start - T0, c.service, c.count) for c in counts] == [
        (timedelta(0), "orders-service", 2),
        (timedelta(minutes=1), "api-gateway", 1),
        (timedelta(minutes=1), "orders-service", 1),
    ]


def test_count_errors_includes_critical_by_default() -> None:
    assert count_errors([log(0, "boom", level="CRITICAL")])[0].count == 1


def test_count_errors_levels_and_bucket_are_configurable() -> None:
    records = [log(0, "a", level="WARNING"), log(400, "b")]
    counts = count_errors(records, levels=("WARNING", "ERROR"), bucket=timedelta(minutes=10))
    assert [(c.bucket_start, c.count) for c in counts] == [(T0, 2)]


def test_count_errors_empty_input() -> None:
    assert count_errors([]) == []


def test_floor_time() -> None:
    ts = datetime(2026, 11, 18, 10, 23, 47, 123456, tzinfo=UTC)
    assert floor_time(ts, timedelta(minutes=1)) == datetime(2026, 11, 18, 10, 23, tzinfo=UTC)
    assert floor_time(ts, timedelta(minutes=5)) == datetime(2026, 11, 18, 10, 20, tzinfo=UTC)
    with pytest.raises(ValueError):
        floor_time(ts, timedelta(0))


# --- normalize_message -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "QueuePool limit reached, waited 2312ms for a connection",
            "QueuePool limit reached, waited <N>ms for a connection",
        ),
        (
            "Failed to send metrics to collector at 10.0.3.17:4317",
            "Failed to send metrics to collector at <IP>",
        ),
        ("Cache miss for key orders:list:page=26", "Cache miss for key orders:list:page=<N>"),
        ("Order 3f2a9c1e-77b0-4c1e-9d2a-0b1c2d3e4f5a not found", "Order <ID> not found"),
        ("trace a0f5b90ebb29cdf49f532c76607cfbdc failed", "trace <ID> failed"),
        ("took 1.5 s  and   retried", "took <N> s and retried"),
        ("Deprecated config key 'proxy.timeout'", "Deprecated config key 'proxy.timeout'"),
    ],
)
def test_normalize_message(message: str, expected: str) -> None:
    assert normalize_message(message) == expected


def test_exception_type() -> None:
    assert exception_type("sqlalchemy.exc.TimeoutError: QueuePool limit") == (
        "sqlalchemy.exc.TimeoutError"
    )
    assert exception_type(None) is None
    assert exception_type("") is None


# --- extract_exceptions ----------------------------------------------------------------------


def test_extract_exceptions_groups_by_normalized_message() -> None:
    timeout = "sqlalchemy.exc.TimeoutError: QueuePool limit of size 20 reached"
    records = [
        log(30, "Database connection timeout after 5000ms", trace_id="t2", exception=timeout),
        log(10, "Database connection timeout after 5001ms", trace_id="t1", exception=timeout),
        log(20, "QueuePool limit reached, waited 2312ms", level="WARNING", trace_id="t3"),
        log(40, "Database connection timeout after 4999ms", trace_id="t1"),
        log(50, "Health check OK", level="INFO"),
    ]
    groups = extract_exceptions(records)

    assert len(groups) == 2
    top = groups[0]
    assert top.pattern == "Database connection timeout after <N>ms"
    assert top.count == 3
    assert top.first_seen == T0 + timedelta(seconds=10)  # trié par temps, pas par ordre d'entrée
    assert top.last_seen == T0 + timedelta(seconds=40)
    assert top.sample_message == "Database connection timeout after 5001ms"
    assert top.exception_type == "sqlalchemy.exc.TimeoutError"
    assert top.trace_ids == ["t1", "t2"]  # sans doublon
    assert groups[1].level == "WARNING"


def test_extract_exceptions_separates_services_and_levels() -> None:
    records = [
        log(0, "timeout 1", service="a"),
        log(1, "timeout 2", service="b"),
        log(2, "timeout 3", service="a", level="WARNING"),
    ]
    assert len(extract_exceptions(records)) == 3


def test_extract_exceptions_caps_trace_ids() -> None:
    records = [log(i, f"boom {i}", trace_id=f"t{i}") for i in range(10)]
    group = extract_exceptions(records, max_trace_ids=3)[0]
    assert group.count == 10
    assert group.trace_ids == ["t0", "t1", "t2"]


def test_extract_exceptions_noise_is_grouped_not_hidden() -> None:
    """Cas du piège : le bruit forme un seul gros groupe, facile à reconnaître comme tel."""
    noise = [
        log(i * 10, f"Failed to send metrics to collector at 10.0.3.{i}:4317") for i in range(30)
    ]
    groups = extract_exceptions(noise)
    assert len(groups) == 1
    assert groups[0].count == 30


def test_results_are_json_serializable_for_prompts() -> None:
    groups = extract_exceptions([log(0, "boom 42", exception="ValueError: bad")])
    payload = json.loads(groups[0].model_dump_json())
    assert payload["pattern"] == "boom <N>"
