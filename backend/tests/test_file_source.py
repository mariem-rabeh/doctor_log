"""Tests de FileDataSource (branche 7) : les 6 datasets et des dossiers construits à la main."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.telemetry import TimeWindow
from app.tools.datasources.base import DataSource
from app.tools.datasources.file_source import DatasetError, FileDataSource, list_datasets

DATASETS = Path(__file__).resolve().parents[2] / "datasets"
ALL_DATASETS = [
    "cpu-overload",
    "db-pool-exhaustion",
    "dependency-down",
    "slow-query",
    "trap-misleading-logs",
    "trap-missing-metrics",
]
T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
FULL = TimeWindow(start=T0, end=T0 + timedelta(minutes=30))


def test_list_datasets_finds_the_six_datasets() -> None:
    assert list_datasets(DATASETS) == ALL_DATASETS


@pytest.mark.parametrize("name", ALL_DATASETS)
def test_each_dataset_loads_without_error(name: str) -> None:
    source = FileDataSource.from_name(name, DATASETS)

    assert isinstance(source, DataSource)
    assert source.missing_files == []
    assert source.get_logs(FULL)
    assert source.get_metrics(FULL)
    assert source.get_traces(FULL)
    assert source.time_range().start >= FULL.start
    expected = source.expected_diagnosis()
    assert expected is not None and expected["scenario"] == name


def test_default_root_comes_from_settings() -> None:
    assert FileDataSource.from_name("slow-query").path.resolve() == (DATASETS / "slow-query")


# --- Filtrage sur la fenêtre -------------------------------------------------------------------


def test_window_filters_all_three_signals() -> None:
    source = FileDataSource.from_name("db-pool-exhaustion", DATASETS)
    window = TimeWindow(start=T0 + timedelta(minutes=20), end=T0 + timedelta(minutes=25))

    logs = source.get_logs(window)
    assert logs and all(window.contains(log.timestamp) for log in logs)
    assert logs == sorted(logs, key=lambda log: log.timestamp)

    spans = source.get_traces(window)
    assert spans and all(window.contains(span.start) for span in spans)

    for series in source.get_metrics(window):
        assert series.points and all(window.contains(p.ts) for p in series.points)
        assert len(series.points) == 21  # toutes les 15 s, bornes incluses


def test_metrics_can_be_selected_by_name() -> None:
    source = FileDataSource.from_name("db-pool-exhaustion", DATASETS)
    series = source.get_metrics(FULL, ["db_connections_idle", "does_not_exist"])
    assert [s.name for s in series] == ["db_connections_idle"]


def test_missing_metric_is_absent_not_invented() -> None:
    """Le piège d'Ichrak : cpu_usage n'existe pas, la source ne doit pas l'inventer."""
    source = FileDataSource.from_name("trap-missing-metrics", DATASETS)
    assert source.get_metrics(FULL, ["cpu_usage"]) == []


def test_window_outside_the_data_returns_nothing() -> None:
    source = FileDataSource.from_name("slow-query", DATASETS)
    night = TimeWindow(start=T0 - timedelta(hours=5), end=T0 - timedelta(hours=4))
    assert source.get_logs(night) == []
    assert source.get_metrics(night) == []
    assert source.get_traces(night) == []


def test_filtering_does_not_modify_the_loaded_data() -> None:
    source = FileDataSource.from_name("slow-query", DATASETS)
    small = TimeWindow(start=T0, end=T0 + timedelta(minutes=1))
    source.get_metrics(small)
    assert len(source.get_metrics(FULL)[0].points) == 120


# --- Fichiers absents ou invalides -------------------------------------------------------------


def test_missing_file_gives_empty_list_and_is_reported(tmp_path: Path) -> None:
    log = {"timestamp": "2026-11-18T10:01:00Z", "level": "ERROR", "service": "a", "message": "x"}
    (tmp_path / "logs.json").write_text(json.dumps([log]))
    source = FileDataSource(tmp_path)

    assert len(source.get_logs(FULL)) == 1
    assert source.get_traces(FULL) == []
    assert source.missing_files == ["metrics.json", "traces.json"]
    assert source.expected_diagnosis() is None


def test_invalid_file_raises_a_clear_error(tmp_path: Path) -> None:
    bad = {"timestamp": "2026-11-18T10:01:00Z", "level": "FATAL", "service": "a", "message": "x"}
    (tmp_path / "logs.json").write_text(json.dumps([bad]))
    with pytest.raises(DatasetError, match=r"logs\.json invalide à 0\.level"):
        FileDataSource(tmp_path).get_logs(FULL)


def test_unknown_dataset_folder() -> None:
    with pytest.raises(FileNotFoundError):
        FileDataSource.from_name("does-not-exist", DATASETS)


def test_list_datasets_ignores_empty_folders(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "traces.json").write_text("[]")
    (tmp_path / ".gitkeep").write_text("")
    assert list_datasets(tmp_path) == ["real"]
    assert list_datasets(tmp_path / "nope") == []
