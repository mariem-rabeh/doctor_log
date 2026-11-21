"""Tests unitaires des outils de métriques (branche 6), sur des séries construites à la main."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.schemas.telemetry import MetricPoint, MetricSeries
from app.tools.metrics_tools import (
    change_pct,
    compare_baseline,
    detect_anomalies,
    missing_metrics,
    series_stats,
)

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
SPLIT = T0 + timedelta(minutes=10)


def series(values: list[float], name: str = "cpu_usage", step_s: int = 60) -> MetricSeries:
    """Une série avec un point par minute à partir de T0."""
    points = [
        MetricPoint(ts=T0 + timedelta(seconds=i * step_s), value=v) for i, v in enumerate(values)
    ]
    return MetricSeries(name=name, labels={"service": "orders-service"}, points=points)


# Baseline légèrement bruitée autour de 0.35, puis plateau à 0.97 à partir de la minute 10.
BASE = [0.34, 0.36, 0.35, 0.33, 0.37, 0.35, 0.34, 0.36, 0.35, 0.35]
INCIDENT = [0.96, 0.97, 0.98, 0.97, 0.96, 0.97, 0.98, 0.97, 0.96, 0.97]
CPU = series(BASE + INCIDENT)


# --- missing_metrics ----------------------------------------------------------------------------


def test_missing_metrics_reports_absent_and_empty_series() -> None:
    present = series([0.1, 0.2], name="error_rate")
    empty = series([], name="cpu_usage")
    result = missing_metrics([present, empty], ["error_rate", "cpu_usage", "db_connections_idle"])
    assert result == ["cpu_usage", "db_connections_idle"]


def test_missing_metrics_nothing_missing() -> None:
    assert missing_metrics([CPU], ["cpu_usage"]) == []


# --- statistiques -------------------------------------------------------------------------------


def test_series_stats() -> None:
    stats = series_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats is not None
    assert (stats.count, stats.mean, stats.min, stats.max) == (5, 3.0, 1.0, 5.0)
    assert stats.p95 == pytest.approx(4.8)


def test_series_stats_of_empty_list_is_none() -> None:
    assert series_stats([]) is None


def test_change_pct() -> None:
    assert change_pct(0.35, 0.97) == pytest.approx(177.1)
    assert change_pct(1.0, 0.0) == -100.0
    assert change_pct(0.0, 0.5) is None  # variation infinie : pas de chiffre inventé


# --- compare_baseline ---------------------------------------------------------------------------


def test_compare_baseline_before_and_during() -> None:
    [result] = compare_baseline([CPU], SPLIT)
    assert result.issue is None
    assert result.before is not None and result.during is not None
    assert result.before.count == 10 and result.during.count == 10
    assert result.before.mean == pytest.approx(0.35)
    assert result.during.mean == pytest.approx(0.969)
    assert result.mean_change_pct == pytest.approx(176.9)
    assert result.p95_change_pct is not None and result.p95_change_pct > 150
    assert result.labels == {"service": "orders-service"}


def test_compare_baseline_flags_empty_series_without_error() -> None:
    [result] = compare_baseline([series([])], SPLIT)
    assert result.issue == "série vide"
    assert result.before is None and result.during is None and result.mean_change_pct is None


def test_compare_baseline_flags_missing_side() -> None:
    only_before = series(BASE)  # 10 points, tous avant SPLIT
    only_during = series(INCIDENT)
    late = compare_baseline([only_before], SPLIT)[0]
    early = compare_baseline([only_during], T0)[0]
    assert late.issue == "aucun point pendant l'incident" and late.mean_change_pct is None
    assert early.issue == "aucun point avant le début de l'incident"


def test_compare_baseline_keeps_one_result_per_series() -> None:
    other = series([0.1] * 20, name="error_rate")
    results = compare_baseline([CPU, other], SPLIT)
    assert [r.name for r in results] == ["cpu_usage", "error_rate"]
    assert results[1].mean_change_pct == 0.0


# --- detect_anomalies ---------------------------------------------------------------------------


def test_detect_anomaly_start_end_and_peak() -> None:
    [anomaly] = detect_anomalies(CPU, baseline_end=SPLIT)
    assert anomaly.direction == "up"
    assert anomaly.start == SPLIT
    assert anomaly.end == T0 + timedelta(minutes=19)
    assert anomaly.point_count == 10
    assert anomaly.peak_value == 0.98
    assert anomaly.peak_ts == T0 + timedelta(minutes=12)  # premier des pics à 0.98
    assert anomaly.peak_z > 3
    assert anomaly.baseline_mean == pytest.approx(0.35)


def test_detect_anomaly_without_known_start_uses_first_quarter() -> None:
    [anomaly] = detect_anomalies(CPU)
    assert anomaly.start == SPLIT and anomaly.direction == "up"


def test_detect_drop_on_flat_baseline() -> None:
    # up = 1 puis 0 : écart-type nul en baseline, le plancher évite une division par zéro.
    up = series([1.0] * 10 + [0.0] * 5, name="up")
    [anomaly] = detect_anomalies(up, baseline_end=SPLIT)
    assert anomaly.direction == "down"
    assert anomaly.start == SPLIT
    assert anomaly.peak_value == 0.0
    assert anomaly.peak_z < -3


def test_threshold_is_configurable() -> None:
    mild = series(BASE + [0.39] * 5)  # écart modéré
    assert detect_anomalies(mild, baseline_end=SPLIT, z_threshold=100) == []
    assert len(detect_anomalies(mild, baseline_end=SPLIT, z_threshold=3)) == 1


def test_single_spike_is_ignored_but_short_gap_is_merged() -> None:
    spike = series(BASE + [0.35, 0.97, 0.35, 0.35, 0.35])
    assert detect_anomalies(spike, baseline_end=SPLIT) == []
    gap = series(BASE + [0.97, 0.97, 0.35, 0.97, 0.97])  # un point normal au milieu
    [anomaly] = detect_anomalies(gap, baseline_end=SPLIT)
    assert anomaly.point_count == 4
    assert anomaly.end == T0 + timedelta(minutes=14)


def test_up_and_down_periods_are_separate() -> None:
    values = BASE + [0.97, 0.97, 0.97, 0.0, 0.0, 0.0]
    anomalies = detect_anomalies(series(values), baseline_end=SPLIT)
    assert [a.direction for a in anomalies] == ["up", "down"]


def test_empty_or_too_short_series_returns_no_anomaly() -> None:
    assert detect_anomalies(series([])) == []
    assert detect_anomalies(series([0.3, 0.9])) == []  # pas assez de baseline
    assert detect_anomalies(series(BASE), baseline_end=T0) == []  # aucun point de baseline


def test_unordered_points_are_sorted() -> None:
    shuffled = MetricSeries(name="cpu_usage", points=list(reversed(CPU.points)))
    [anomaly] = detect_anomalies(shuffled, baseline_end=SPLIT)
    assert anomaly.start == SPLIT


# --- Sur les datasets du projet -----------------------------------------------------------------

DATASETS = Path(__file__).resolve().parents[2] / "datasets"
INCIDENT_START = datetime(2026, 11, 18, 10, 20, tzinfo=UTC)


def _load(name: str) -> list[MetricSeries]:
    data = (DATASETS / name / "metrics.json").read_bytes()
    return TypeAdapter(list[MetricSeries]).validate_json(data)


def test_cpu_overload_dataset() -> None:
    metrics = _load("cpu-overload")
    cpu = next(s for s in metrics if s.name == "cpu_usage")
    [anomaly] = detect_anomalies(cpu)
    assert anomaly.direction == "up"
    assert INCIDENT_START <= anomaly.start < INCIDENT_START + timedelta(minutes=3)
    pool = [s for s in metrics if s.name.startswith("db_connections")]
    assert all(detect_anomalies(s) == [] for s in pool)  # le pool reste sain


def test_trap_missing_metrics_dataset() -> None:
    metrics = _load("trap-missing-metrics")
    assert missing_metrics(metrics, ["cpu_usage", "http_request_duration_p95"]) == ["cpu_usage"]
