"""Tests du générateur de datasets (branche 3).

Vérifient que les fichiers commités sont à jour, cohérents entre eux, et que chaque
scénario raconte bien son histoire (c'est ce qui permet de ne pas confondre S01 et S03).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.schemas.telemetry import Span

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_generator() -> ModuleType:
    path = REPO_ROOT / "scripts" / "generate_dataset.py"
    spec = importlib.util.spec_from_file_location("generate_dataset", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_dataset"] = module  # requis par les dataclasses
    spec.loader.exec_module(module)
    return module


gen = _load_generator()
NAMES = list(gen.SCENARIOS)


@pytest.mark.parametrize("name", NAMES)
def test_dataset_is_coherent(name: str) -> None:
    assert gen.check_dataset(gen.generate(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_generation_is_reproducible(name: str) -> None:
    assert gen.render(gen.generate(name)) == gen.render(gen.generate(name))


@pytest.mark.parametrize("name", NAMES)
def test_committed_files_are_up_to_date(name: str) -> None:
    folder = REPO_ROOT / "datasets" / name
    for filename, content in gen.render(gen.generate(name)).items():
        assert (folder / filename).read_bytes() == content, (
            f"{name}/{filename} est périmé : relancer scripts/generate_dataset.py"
        )


# --- Chaque scénario raconte son histoire ---------------------------------------------------


def _max_duration(name: str, operation: str, *, during_incident: bool) -> float:
    """Durée max d'une opération, soit avant l'incident, soit pendant le plateau.
    La phase de montée (entre les deux) est volontairement ignorée."""

    def keep(span: Span) -> bool:
        if span.operation != operation:
            return False
        if during_incident:
            return bool(span.start >= gen.INCIDENT_START + gen.RAMP)
        return bool(span.start < gen.INCIDENT_START)

    durations = [s.duration_ms for s in gen.generate(name).traces if keep(s)]
    return max(durations, default=0.0)


def _series_last(name: str, metric: str, **labels: str) -> float:
    for series in gen.generate(name).metrics:
        if series.name == metric and labels.items() <= series.labels.items():
            return float(series.points[-1].value)
    raise LookupError(metric)


def test_s01_pool_is_full_but_sql_stays_fast() -> None:
    assert _series_last("db-pool-exhaustion", "db_connections_idle") == 0
    assert _max_duration("db-pool-exhaustion", "db.acquire_connection", during_incident=True) > 4000
    assert _max_duration("db-pool-exhaustion", "SELECT orders", during_incident=True) < 100


def test_s03_sql_is_slow_but_pool_stays_healthy() -> None:
    assert _series_last("slow-query", "db_connections_idle") > 0
    assert _max_duration("slow-query", "db.acquire_connection", during_incident=True) < 100
    assert _max_duration("slow-query", "SELECT orders", during_incident=True) > 4000
    assert _series_last("slow-query", "http_request_duration_p95", route="/orders") < 0.5


def test_trap_real_cause_is_only_in_traces() -> None:
    ds = gen.generate("trap-misleading-logs")
    assert _max_duration("trap-misleading-logs", "GET /stock", during_incident=False) < 100
    assert _max_duration("trap-misleading-logs", "GET /stock", during_incident=True) > 3000
    assert not any("inventory" in log.message or "stock" in log.message for log in ds.logs)
    # Le bruit domine les logs non-INFO : c'est tout l'intérêt du piège.
    noisy = [log for log in ds.logs if log.level != "INFO"]
    assert len(noisy) > 100


def test_cpu_never_explains_these_incidents() -> None:
    # Scénarios où le CPU n'est PAS la cause (cpu-overload et trap-missing-metrics exclus).
    for name in ("db-pool-exhaustion", "slow-query", "trap-misleading-logs", "dependency-down"):
        assert _series_last(name, "cpu_usage") < 0.5


# --- Scénarios centrés sur les métriques (Ichrak) -------------------------------------------


def _ratio_last_vs_first(name: str, metric: str, **labels: str) -> float:
    for series in gen.generate(name).metrics:
        if series.name == metric and labels.items() <= series.labels.items():
            return float(series.points[-1].value / series.points[0].value)
    raise LookupError(metric)


def test_s02_cpu_saturates_and_latency_rises_uniformly() -> None:
    assert _series_last("cpu-overload", "cpu_usage") > 0.9
    # Uniforme : les deux routes sont multipliées par ~3.
    for route in ("/orders", "/orders/search"):
        assert (
            2.0
            < _ratio_last_vs_first("cpu-overload", "http_request_duration_p95", route=route)
            < 5.0
        )
    # Pool sain, acquisition et SQL rapides : ce n'est pas la base.
    assert _series_last("cpu-overload", "db_connections_idle") >= 10
    assert _max_duration("cpu-overload", "db.acquire_connection", during_incident=True) < 100
    assert _max_duration("cpu-overload", "SELECT orders", during_incident=True) < 100
    # Le temps passé dans le handler explose.
    assert _max_duration("cpu-overload", "list_orders", during_incident=False) < 100
    assert _max_duration("cpu-overload", "list_orders", during_incident=True) > 200


def test_s04_dependency_refuses_connections() -> None:
    ds = gen.generate("dependency-down")
    up = next(s for s in ds.metrics if s.name == "up")
    assert all(p.value == (0.0 if p.ts >= gen.INCIDENT_START else 1.0) for p in up.points)
    refused = [log for log in ds.logs if log.exception and "Connection refused" in log.exception]
    assert refused and all(log.timestamp >= gen.INCIDENT_START for log in refused)
    errors = [s for s in ds.traces if s.attributes.get("error.type") == "ConnectionRefusedError"]
    assert errors and all(s.status == "error" for s in errors)
    # Plus aucun appel réussi à inventory-service une fois la panne installée.
    late = gen.INCIDENT_START + gen.RAMP
    assert not [s for s in ds.traces if s.service == "inventory-service" and s.start >= late]
    # /orders/search ne dépend pas du stock : il reste normal.
    ratio = _ratio_last_vs_first(
        "dependency-down", "http_request_duration_p95", route="/orders/search"
    )
    assert ratio < 1.5


def test_trap_missing_cpu_metric_is_absent_and_declared() -> None:
    ds = gen.generate("trap-missing-metrics")
    assert ds.expected["missing_metrics"] == ["cpu_usage"]
    assert not [s for s in ds.metrics if s.name == "cpu_usage"]
    assert ds.expected["max_confidence"] <= 0.6
    # Même symptôme que S02...
    assert (
        _ratio_last_vs_first("trap-missing-metrics", "http_request_duration_p95", route="/orders")
        > 2.0
    )
    # ... mais aucun log ne nomme la cause.
    assert not any(
        "cpu" in log.message.lower() or "event loop" in log.message.lower() for log in ds.logs
    )


def test_declared_missing_metric_must_really_be_missing() -> None:
    ds = gen.generate("cpu-overload")
    ds.expected["missing_metrics"] = ["cpu_usage"]
    assert gen.check_dataset(ds) == ["métrique déclarée manquante mais présente : cpu_usage"]
