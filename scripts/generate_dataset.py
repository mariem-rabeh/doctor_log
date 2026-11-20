"""Génère les datasets de scénarios d'incident de façon reproductible (graine fixe).

Chaque scénario produit, dans datasets/<nom>/ :
    logs.json, metrics.json, traces.json   au format exact de app/schemas/telemetry.py
    expected_diagnosis.json                la cause attendue, utilisée par l'évaluation

Usage (depuis backend/, avec le venv actif) :
    python ../scripts/generate_dataset.py                  # tous les scénarios
    python ../scripts/generate_dataset.py slow-query       # un seul scénario
    python ../scripts/generate_dataset.py --check          # vérifie que les fichiers sont à jour

Pour ajouter un scénario : écrire une fonction scenario_xxx(rng) -> Dataset qui utilise
les briques du moteur, puis l'enregistrer dans SCENARIOS (en bas du fichier).

Conventions d'unités (identiques à Prometheus, pour que dataset et live se ressemblent) :
    http_request_duration_p95 en secondes, error_rate et cpu_usage en ratio 0-1.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.schemas.telemetry import (
    LogLevel,
    LogRecord,
    MetricPoint,
    MetricSeries,
    Span,
    SpanStatus,
    TimeWindow,
)

# --- Paramètres communs à tous les scénarios ----------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = REPO_ROOT / "datasets"
SEED = 20261118

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=T0, end=T0 + timedelta(minutes=30))
INCIDENT_START = T0 + timedelta(minutes=20)
RAMP = timedelta(minutes=3)  # montée progressive de l'incident
METRIC_STEP = timedelta(seconds=15)  # 120 points par série
REQUEST_STEP = timedelta(seconds=30)  # 60 requêtes, donc 60 traces
POOL_SIZE = 20

ROUTES = ("/orders", "/orders/search")
HANDLERS = {"/orders": "list_orders", "/orders/search": "search_orders"}
STATEMENTS = {
    "/orders": "SELECT orders ORDER BY created_at DESC LIMIT ?",
    "/orders/search": "SELECT orders WHERE customer_email = ?",
}
REQUIRED_METRICS = {
    "http_request_duration_p95",
    "error_rate",
    "db_connections_active",
    "db_connections_idle",
    "cpu_usage",
}

Range = tuple[float, float]
Attributes = dict[str, str | int | float | bool]


# --- Moteur : temps et aléatoire -----------------------------------------------------------


def ms(value: float) -> timedelta:
    return timedelta(milliseconds=value)


def r1(value: float) -> float:
    """Arrondi au dixième de ms : les durées restent exactes une fois converties en timedelta."""
    return round(value, 1)


def lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def intensity(ts: datetime) -> float:
    """Force de l'incident à l'instant ts : 0 avant, montée linéaire pendant RAMP, puis 1."""
    if ts < INCIDENT_START:
        return 0.0
    return min(1.0, (ts - INCIDENT_START) / RAMP)


def request_times() -> list[datetime]:
    times, ts = [], WINDOW.start
    while ts < WINDOW.end:
        times.append(ts)
        ts += REQUEST_STEP
    return times


def new_trace_id(rng: random.Random) -> str:
    return f"{rng.getrandbits(128):032x}"


def new_span_id(rng: random.Random) -> str:
    return f"{rng.getrandbits(64):016x}"


# --- Moteur : le conteneur d'un dataset ----------------------------------------------------


@dataclass
class Dataset:
    name: str
    logs: list[LogRecord] = field(default_factory=list)
    metrics: list[MetricSeries] = field(default_factory=list)
    traces: list[Span] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)

    def log(
        self,
        ts: datetime,
        level: LogLevel,
        service: str,
        message: str,
        trace_id: str | None = None,
        exception: str | None = None,
    ) -> None:
        self.logs.append(
            LogRecord(
                timestamp=ts,
                level=level,
                service=service,
                message=message,
                trace_id=trace_id,
                exception=exception,
            )
        )


# --- Moteur : métriques ---------------------------------------------------------------------


def metric_series(
    rng: random.Random,
    name: str,
    labels: dict[str, str],
    baseline: Range,
    incident: Range,
    *,
    integer: bool = False,
) -> MetricSeries:
    """Une série qui suit `baseline` puis glisse vers `incident` selon intensity()."""
    points, ts = [], WINDOW.start
    while ts < WINDOW.end:
        value = lerp(rng.uniform(*baseline), rng.uniform(*incident), intensity(ts))
        points.append(MetricPoint(ts=ts, value=float(round(value)) if integer else round(value, 3)))
        ts += METRIC_STEP
    return MetricSeries(name=name, labels=labels, points=points)


def derived_series(
    source: MetricSeries, name: str, transform: Callable[[float], float]
) -> MetricSeries:
    """Série calculée point par point à partir d'une autre (ex. idle = taille du pool - active)."""
    points = [MetricPoint(ts=p.ts, value=transform(p.value)) for p in source.points]
    return MetricSeries(name=name, labels=dict(source.labels), points=points)


def pool_metrics(
    rng: random.Random, active_baseline: Range, active_incident: Range
) -> list[MetricSeries]:
    """active + idle = POOL_SIZE à chaque instant : les deux séries restent cohérentes."""
    labels = {"service": "orders-service"}
    active = metric_series(
        rng, "db_connections_active", labels, active_baseline, active_incident, integer=True
    )
    idle = derived_series(active, "db_connections_idle", lambda v: POOL_SIZE - v)
    return [active, idle]


def latency_p95(rng: random.Random, route: str, baseline: Range, incident: Range) -> MetricSeries:
    labels = {"service": "api-gateway", "route": route}
    return metric_series(rng, "http_request_duration_p95", labels, baseline, incident)


# --- Moteur : traces ------------------------------------------------------------------------


@dataclass
class Call:
    """Un appel dans une trace. self_ms = temps propre, passé avant d'appeler les enfants."""

    service: str
    operation: str
    self_ms: float
    status: SpanStatus = "ok"
    attributes: Attributes = field(default_factory=dict)
    children: list[Call] = field(default_factory=list)


def build_trace(rng: random.Random, root: Call, start: datetime) -> tuple[str, list[Span], float]:
    """Transforme un arbre de Call en spans. Durée d'un span = temps propre + durée des enfants,
    exécutés l'un après l'autre. Renvoie (trace_id, spans, durée totale en ms)."""
    trace_id = new_trace_id(rng)
    spans: list[Span] = []

    def visit(call: Call, parent_id: str | None, begin: datetime) -> float:
        span_id = new_span_id(rng)
        duration = r1(call.self_ms)
        for child in call.children:
            duration = r1(duration + visit(child, span_id, begin + ms(duration)))
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_id,
                service=call.service,
                operation=call.operation,
                start=begin,
                duration_ms=duration,
                status=call.status,
                attributes=call.attributes,
            )
        )
        return duration

    total = visit(root, None, start)
    spans.sort(key=lambda s: s.start)  # parent avant ses enfants
    return trace_id, spans, total


def acquire_call(rng: random.Random, wait_ms: float, *, timeout: bool = False) -> Call:
    attributes: Attributes = {"db.pool.size": POOL_SIZE}
    if timeout:
        attributes["error.type"] = "TimeoutError"
    return Call(
        "orders-service", "db.acquire_connection", wait_ms, "error" if timeout else "ok", attributes
    )


def select_call(route: str, duration_ms: float, *, cancelled: bool = False) -> Call:
    attributes: Attributes = {"db.system": "postgresql", "db.statement": STATEMENTS[route]}
    if cancelled:
        attributes["error.type"] = "QueryCanceled"
    return Call(
        "postgres", "SELECT orders", duration_ms, "error" if cancelled else "ok", attributes
    )


def http_request(
    ds: Dataset,
    rng: random.Random,
    ts: datetime,
    route: str,
    children: list[Call],
    *,
    status_code: int = 200,
) -> tuple[str, datetime]:
    """Une requête api-gateway -> orders-service -> children, avec son log d'accès.
    Renvoie (trace_id, instant de fin) pour que le scénario ajoute ses propres logs."""
    status: SpanStatus = "error" if status_code >= 500 else "ok"
    handler = Call("orders-service", HANDLERS[route], rng.uniform(3, 8), status, {}, children)
    gateway_attributes: Attributes = {"http.route": route, "http.status_code": status_code}
    root = Call(
        "api-gateway", f"GET {route}", rng.uniform(2, 5), status, gateway_attributes, [handler]
    )
    trace_id, spans, total = build_trace(rng, root, ts)
    ds.traces.extend(spans)
    end = ts + ms(total)
    level: LogLevel = "ERROR" if status == "error" else "INFO"
    ds.log(end, level, "api-gateway", f"GET {route} {status_code} {total:.0f}ms", trace_id)
    return trace_id, end


def health_checks(ds: Dataset, rng: random.Random, services: list[str]) -> None:
    """Logs INFO de fond, sans trace_id, une fois par minute et par service."""
    for minute in range(30):
        for service in services:
            ts = WINDOW.start + timedelta(minutes=minute, seconds=rng.uniform(0, 59))
            ds.log(ts, "INFO", service, "Health check OK")


# --- Scénarios -----------------------------------------------------------------------------


def scenario_db_pool_exhaustion(rng: random.Random) -> Dataset:
    """S01 : le pool de connexions d'orders-service est plein. Toutes les routes souffrent,
    l'attente d'acquisition explose, mais la requête SQL elle-même reste rapide."""
    ds = Dataset("db-pool-exhaustion")
    health_checks(ds, rng, ["api-gateway", "orders-service"])

    for i, ts in enumerate(request_times()):
        k = intensity(ts)
        route = ROUTES[i % 2]
        if rng.random() < 0.25 * k:  # timeout d'acquisition : la requête échoue
            trace_id, end = http_request(
                ds, rng, ts, route, [acquire_call(rng, 5000.0, timeout=True)], status_code=500
            )
            ds.log(
                end,
                "ERROR",
                "orders-service",
                "Database connection timeout after 5000ms",
                trace_id,
                exception=(
                    f"sqlalchemy.exc.TimeoutError: QueuePool limit of size {POOL_SIZE} "
                    "overflow 0 reached, connection timed out, timeout 5.00"
                ),
            )
            continue

        wait = lerp(rng.uniform(1, 4), rng.uniform(1500, 4800), k)
        children = [acquire_call(rng, wait), select_call(route, rng.uniform(8, 20))]
        trace_id, end = http_request(ds, rng, ts, route, children)
        if wait > 1000:
            ds.log(
                end,
                "WARNING",
                "orders-service",
                f"QueuePool limit reached, waited {wait:.0f}ms for a connection",
                trace_id,
            )
        ds.log(end, "INFO", "orders-service", f"Fetched {rng.randint(5, 50)} orders", trace_id)

    ds.metrics = [
        latency_p95(rng, "/orders", (0.10, 0.14), (4.6, 5.0)),
        latency_p95(rng, "/orders/search", (0.10, 0.14), (4.6, 5.0)),
        metric_series(rng, "error_rate", {"service": "api-gateway"}, (0.001, 0.003), (0.22, 0.28)),
        *pool_metrics(rng, (6, 10), (20, 20)),
        metric_series(rng, "cpu_usage", {"service": "orders-service"}, (0.30, 0.40), (0.30, 0.40)),
    ]
    ds.expected = {
        "scenario": ds.name,
        "cause_category": "db_pool_exhaustion",
        "culprit_service": "orders-service",
        "cause_keywords": ["pool", "connection"],
        "must_not_conclude": ["slow_query", "cpu_saturation", "dependency_down"],
        "incident_start": INCIDENT_START.isoformat(),
        "key_evidence": [
            {"source": "metrics", "signal": "db_connections_idle"},
            {"source": "metrics", "signal": "db_connections_active"},
            {"source": "logs", "signal": "QueuePool limit"},
            {"source": "logs", "signal": "Database connection timeout"},
            {"source": "traces", "signal": "db.acquire_connection"},
        ],
    }
    return ds


def scenario_slow_query(rng: random.Random) -> Dataset:
    """S03 : la requête de recherche par email n'a pas d'index. Seul /orders/search ralentit ;
    le pool monte un peu (connexions tenues plus longtemps) mais n'est jamais plein."""
    ds = Dataset("slow-query")
    health_checks(ds, rng, ["api-gateway", "orders-service"])
    statement_timeout = 5000.0

    for i, ts in enumerate(request_times()):
        k = intensity(ts)
        route = ROUTES[i % 2]
        if route == "/orders/search":
            planned = lerp(rng.uniform(8, 20), rng.uniform(4000, 5600), k)
        else:
            planned = rng.uniform(8, 20)
        cancelled = planned >= statement_timeout
        children = [
            acquire_call(rng, rng.uniform(1, 4)),
            select_call(route, min(planned, statement_timeout), cancelled=cancelled),
        ]
        trace_id, end = http_request(
            ds, rng, ts, route, children, status_code=500 if cancelled else 200
        )
        if cancelled:
            ds.log(
                end,
                "ERROR",
                "orders-service",
                "Query failed: canceling statement due to statement timeout",
                trace_id,
                exception=(
                    "psycopg.errors.QueryCanceled: canceling statement due to statement timeout"
                ),
            )
            continue
        if planned > 1000:
            ds.log(
                end,
                "WARNING",
                "orders-service",
                f"Slow query detected duration_ms={planned:.0f} statement={STATEMENTS[route]}",
                trace_id,
            )
        ds.log(end, "INFO", "orders-service", f"Fetched {rng.randint(5, 50)} orders", trace_id)

    ds.metrics = [
        latency_p95(rng, "/orders", (0.10, 0.14), (0.10, 0.15)),
        latency_p95(rng, "/orders/search", (0.10, 0.14), (4.5, 5.0)),
        metric_series(rng, "error_rate", {"service": "api-gateway"}, (0.001, 0.003), (0.06, 0.10)),
        *pool_metrics(rng, (6, 10), (12, 15)),
        metric_series(rng, "cpu_usage", {"service": "orders-service"}, (0.30, 0.40), (0.30, 0.40)),
    ]
    ds.expected = {
        "scenario": ds.name,
        "cause_category": "slow_query",
        "culprit_service": "postgres",
        "cause_keywords": ["query", "sql", "index"],
        "must_not_conclude": ["db_pool_exhaustion", "cpu_saturation", "dependency_down"],
        "incident_start": INCIDENT_START.isoformat(),
        "key_evidence": [
            {"source": "traces", "signal": "SELECT orders"},
            {"source": "logs", "signal": "Slow query detected"},
            {"source": "metrics", "signal": "http_request_duration_p95"},
            {"source": "metrics", "signal": "db_connections_idle"},
        ],
    }
    return ds


def scenario_trap_misleading_logs(rng: random.Random) -> Dataset:
    """Piège : inventory-service répond lentement, visible uniquement dans les traces.
    Les logs sont pleins de bruit : warnings constants, et une rafale d'erreurs
    « Failed to send metrics » qui coïncide avec l'incident mais n'a aucun rapport."""
    ds = Dataset("trap-misleading-logs")

    for i, ts in enumerate(request_times()):
        k = intensity(ts)
        route = ROUTES[i % 2]
        children = [
            acquire_call(rng, rng.uniform(1, 4)),
            select_call(route, rng.uniform(8, 20)),
        ]
        if route == "/orders":  # la liste des commandes affiche le stock
            stock_ms = lerp(rng.uniform(30, 50), rng.uniform(3500, 4500), k)
            children.append(
                Call(
                    "inventory-service",
                    "GET /stock",
                    stock_ms,
                    attributes={"http.route": "/stock", "http.status_code": 200},
                )
            )
        trace_id, end = http_request(ds, rng, ts, route, children)
        ds.log(end, "INFO", "orders-service", f"Fetched {rng.randint(5, 50)} orders", trace_id)

    # Bruit constant, présent avant et pendant l'incident au même rythme.
    for ts in request_times():
        ds.log(
            ts + timedelta(seconds=rng.uniform(0, 29)),
            "WARNING",
            "api-gateway",
            "Deprecated config key 'proxy.timeout' used, migrate to 'upstream.timeout'",
        )
        ds.log(
            ts + timedelta(seconds=rng.uniform(0, 29)),
            "WARNING",
            "orders-service",
            f"Cache miss for key orders:list:page={rng.randint(1, 40)}",
        )

    # Bruit corrélé : commence avec l'incident, sans aucun lien avec la lenteur.
    ts = INCIDENT_START + timedelta(seconds=rng.uniform(5, 15))
    while ts < WINDOW.end:
        ds.log(
            ts,
            "ERROR",
            "orders-service",
            "Failed to send metrics to collector at 10.0.3.17:4317",
            exception="ConnectionRefusedError: [Errno 111] Connection refused",
        )
        ts += timedelta(seconds=rng.uniform(8, 14))

    ds.metrics = [
        latency_p95(rng, "/orders", (0.10, 0.14), (4.0, 4.6)),
        latency_p95(rng, "/orders/search", (0.10, 0.14), (0.10, 0.15)),
        metric_series(
            rng, "error_rate", {"service": "api-gateway"}, (0.001, 0.003), (0.001, 0.003)
        ),
        *pool_metrics(rng, (6, 10), (6, 10)),
        metric_series(rng, "cpu_usage", {"service": "orders-service"}, (0.30, 0.40), (0.30, 0.40)),
    ]
    ds.expected = {
        "scenario": ds.name,
        "cause_category": "slow_dependency",
        "culprit_service": "inventory-service",
        "cause_keywords": ["inventory", "stock"],
        "must_not_conclude": [
            "metrics_collector",
            "deprecated_config",
            "cache",
            "db_pool_exhaustion",
            "slow_query",
        ],
        "incident_start": INCIDENT_START.isoformat(),
        "key_evidence": [
            {"source": "traces", "signal": "GET /stock"},
            {"source": "metrics", "signal": "http_request_duration_p95"},
        ],
        "distractors": [
            {"source": "logs", "signal": "Failed to send metrics to collector"},
            {"source": "logs", "signal": "Deprecated config key"},
            {"source": "logs", "signal": "Cache miss"},
        ],
    }
    return ds


# --- Contrôles de cohérence entre les fichiers ---------------------------------------------


def check_dataset(ds: Dataset) -> list[str]:
    """Renvoie la liste des incohérences (vide si le dataset est correct)."""
    problems: list[str] = []
    spans = {(s.trace_id, s.span_id): s for s in ds.traces}
    trace_ids = {s.trace_id for s in ds.traces}

    for log in ds.logs:
        if not WINDOW.contains(log.timestamp):
            problems.append(f"log hors fenêtre : {log.timestamp} {log.message}")
        if log.trace_id is not None and log.trace_id not in trace_ids:
            problems.append(f"log avec trace_id inconnu : {log.trace_id}")

    for span in ds.traces:
        if not WINDOW.contains(span.start):
            problems.append(f"span hors fenêtre : {span.operation} {span.start}")
        if span.parent_span_id is None:
            continue
        parent = spans.get((span.trace_id, span.parent_span_id))
        if parent is None:
            problems.append(f"span sans parent : {span.span_id}")
            continue
        span_end = span.start + ms(span.duration_ms)
        parent_end = parent.start + ms(parent.duration_ms)
        if span.start < parent.start or span_end > parent_end:
            problems.append(f"span {span.operation} déborde de son parent {parent.operation}")

    names = {series.name for series in ds.metrics}
    for missing in sorted(REQUIRED_METRICS - names):
        problems.append(f"métrique manquante : {missing}")
    for series in ds.metrics:
        if any(not WINDOW.contains(p.ts) for p in series.points):
            problems.append(f"point hors fenêtre dans {series.name}")

    # expected_diagnosis.json doit pointer vers des choses qui existent vraiment.
    services = {s.service for s in ds.traces}
    if ds.expected.get("culprit_service") not in services:
        problems.append(f"culprit_service absent des traces : {ds.expected.get('culprit_service')}")
    operations = {s.operation for s in ds.traces}
    messages = [log.message for log in ds.logs]
    for item in ds.expected.get("key_evidence", []) + ds.expected.get("distractors", []):
        source, signal = item["source"], item["signal"]
        found = (
            (source == "metrics" and signal in names)
            or (source == "traces" and signal in operations)
            or (source == "logs" and any(signal in m for m in messages))
        )
        if not found:
            problems.append(f"preuve attendue introuvable : {source} / {signal}")

    return problems


# --- Écriture des fichiers -----------------------------------------------------------------


def render(ds: Dataset) -> dict[str, bytes]:
    """Contenu exact de chaque fichier (octets), trié pour être stable d'une exécution à l'autre."""
    logs = sorted(ds.logs, key=lambda log: log.timestamp)
    traces = sorted(ds.traces, key=lambda s: (s.start, s.trace_id))
    expected = json.dumps(ds.expected, indent=2, ensure_ascii=False).encode()
    return {
        "logs.json": TypeAdapter(list[LogRecord]).dump_json(logs, indent=2) + b"\n",
        "metrics.json": TypeAdapter(list[MetricSeries]).dump_json(ds.metrics, indent=2) + b"\n",
        "traces.json": TypeAdapter(list[Span]).dump_json(traces, indent=2) + b"\n",
        "expected_diagnosis.json": expected + b"\n",
    }


SCENARIOS: dict[str, Callable[[random.Random], Dataset]] = {
    "db-pool-exhaustion": scenario_db_pool_exhaustion,
    "slow-query": scenario_slow_query,
    "trap-misleading-logs": scenario_trap_misleading_logs,
}


def generate(name: str) -> Dataset:
    """Même nom -> même graine -> exactement les mêmes données."""
    rng = random.Random(f"{SEED}:{name}")
    return SCENARIOS[name](rng)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "names", nargs="*", help=f"scénarios parmi {list(SCENARIOS)} (défaut : tous)"
    )
    parser.add_argument("--out", type=Path, default=DATASETS_DIR)
    parser.add_argument("--check", action="store_true", help="vérifie sans écrire")
    args = parser.parse_args(argv)
    unknown = sorted(set(args.names) - set(SCENARIOS))
    if unknown:
        parser.error(f"scénario(s) inconnu(s) : {unknown}")

    ok = True
    for name in args.names or list(SCENARIOS):
        ds = generate(name)
        problems = check_dataset(ds)
        if problems:
            ok = False
            print(f"[{name}] {len(problems)} incohérence(s) :", *problems, sep="\n  - ")
            continue

        folder = args.out / name
        for filename, content in render(ds).items():
            path = folder / filename
            if args.check:
                if not path.exists() or path.read_bytes() != content:
                    ok = False
                    print(f"[{name}] {filename} n'est pas à jour : relancer le générateur")
            else:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        print(
            f"[{name}] {len(ds.logs)} logs, {len(ds.metrics)} séries, "
            f"{len(ds.traces)} spans ({len({s.trace_id for s in ds.traces})} traces)"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
