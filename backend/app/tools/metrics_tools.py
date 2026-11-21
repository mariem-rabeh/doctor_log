"""Outils d'analyse des métriques : fonctions pures, sans LLM.

Ces fonctions produisent les chiffres que le Metrics Agent met dans ses Evidence.
Règle du projet : les chiffres viennent d'ici, jamais du LLM.

Une série absente ou vide n'est jamais une erreur : elle est signalée (missing_metrics,
BaselineComparison.issue) pour que l'agent la mette dans Finding.missing_data.
"""

import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.telemetry import MetricPoint, MetricSeries
from app.tools.trace_tools import percentile

DEFAULT_Z_THRESHOLD = 3.0
DEFAULT_BASELINE_FRACTION = 0.25  # sans début connu : le premier quart sert de référence
MIN_BASELINE_POINTS = 3

Direction = Literal["up", "down"]


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True)


class SeriesStats(_Result):
    count: int
    mean: float
    p95: float
    min: float
    max: float


class BaselineComparison(_Result):
    """Une série, avant et pendant l'incident. Les variations sont en % (None si incalculable)."""

    name: str
    labels: dict[str, str]
    before: SeriesStats | None
    during: SeriesStats | None
    mean_change_pct: float | None
    p95_change_pct: float | None
    issue: str | None  # pourquoi la comparaison est incomplète (série vide, pas de baseline...)


class Anomaly(_Result):
    """Période continue où la série s'écarte de sa baseline d'au moins z_threshold écarts-types."""

    name: str
    labels: dict[str, str]
    direction: Direction
    start: datetime
    end: datetime
    point_count: int
    peak_ts: datetime
    peak_value: float
    peak_z: float  # signé : positif si la valeur monte, négatif si elle baisse
    baseline_mean: float
    baseline_std: float


# --- Séries absentes ou vides -------------------------------------------------------------------


def missing_metrics(series: Iterable[MetricSeries], expected: Iterable[str]) -> list[str]:
    """Noms attendus qui n'ont aucune série, ou seulement des séries sans point (triés)."""
    with_points = {s.name for s in series if s.points}
    return sorted(set(expected) - with_points)


# --- Statistiques -------------------------------------------------------------------------------


def series_stats(values: Sequence[float]) -> SeriesStats | None:
    """Moyenne, p95, min et max ; None si la liste est vide."""
    if not values:
        return None
    return SeriesStats(
        count=len(values),
        mean=_round(sum(values) / len(values)),
        p95=_round(percentile(values, 95)),
        min=_round(min(values)),
        max=_round(max(values)),
    )


def change_pct(before: float, after: float) -> float | None:
    """Variation en % de before vers after ; None si before vaut 0 (variation infinie)."""
    if before == 0:
        return None
    return round((after - before) / abs(before) * 100, 1)


# --- compare_baseline ---------------------------------------------------------------------------


def compare_baseline(series: Iterable[MetricSeries], split: datetime) -> list[BaselineComparison]:
    """Compare chaque série avant `split` (baseline) et à partir de `split` (incident).

    Ne lève jamais d'erreur : une série vide, ou sans point d'un côté du split, est rendue
    avec `issue` renseigné et les champs manquants à None.
    """
    result = []
    for s in series:
        before = series_stats([p.value for p in s.points if p.ts < split])
        during = series_stats([p.value for p in s.points if p.ts >= split])
        issue = None
        if not s.points:
            issue = "série vide"
        elif before is None:
            issue = "aucun point avant le début de l'incident"
        elif during is None:
            issue = "aucun point pendant l'incident"
        result.append(
            BaselineComparison(
                name=s.name,
                labels=dict(s.labels),
                before=before,
                during=during,
                mean_change_pct=change_pct(before.mean, during.mean) if before and during else None,
                p95_change_pct=change_pct(before.p95, during.p95) if before and during else None,
                issue=issue,
            )
        )
    return result


# --- detect_anomalies ---------------------------------------------------------------------------


def detect_anomalies(
    series: MetricSeries,
    *,
    baseline_end: datetime | None = None,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    min_points: int = 2,
    max_gap: int = 1,
) -> list[Anomaly]:
    """Périodes où |z| ≥ z_threshold, z étant calculé par rapport à la baseline.

    - Baseline : les points avant `baseline_end`, ou le premier quart de la série si on ne
      connaît pas encore le début de l'incident. Il faut au moins 3 points de baseline,
      sinon on renvoie [] (impossible de juger).
    - Écart-type plancher : une baseline parfaitement plate (ex. up = 1) aurait un écart-type
      nul ; on utilise au minimum 1 % de |moyenne| pour garder un z-score fini.
    - Une période regroupe des points anormaux de même sens séparés d'au plus `max_gap`
      points normaux, et doit en compter au moins `min_points` (un pic isolé est du bruit).

    Renvoie [] pour une série vide, sans lever d'erreur.
    """
    points = sorted(series.points, key=lambda p: p.ts)
    if baseline_end is None:
        size = max(MIN_BASELINE_POINTS, int(len(points) * DEFAULT_BASELINE_FRACTION))
        baseline = points[:size]
    else:
        baseline = [p for p in points if p.ts < baseline_end]
    if len(baseline) < MIN_BASELINE_POINTS or len(points) <= len(baseline):
        return []

    values = [p.value for p in baseline]
    mean = sum(values) / len(values)
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    std = max(std, abs(mean) * 0.01, 1e-9)

    candidates = points[len(baseline) :] if baseline_end is None else points
    flagged: list[tuple[int, MetricPoint, float]] = []
    for index, point in enumerate(candidates):
        z = (point.value - mean) / std
        if abs(z) >= z_threshold:
            flagged.append((index, point, z))

    anomalies = []
    for group in _group(flagged, max_gap):
        if len(group) < min_points:
            continue
        _, peak, peak_z = max(group, key=lambda item: abs(item[2]))
        anomalies.append(
            Anomaly(
                name=series.name,
                labels=dict(series.labels),
                direction="up" if peak_z > 0 else "down",
                start=group[0][1].ts,
                end=group[-1][1].ts,
                point_count=len(group),
                peak_ts=peak.ts,
                peak_value=peak.value,
                peak_z=round(peak_z, 2),
                baseline_mean=_round(mean),
                baseline_std=_round(std),
            )
        )
    return anomalies


def _group(
    flagged: list[tuple[int, MetricPoint, float]], max_gap: int
) -> list[list[tuple[int, MetricPoint, float]]]:
    """Regroupe les points anormaux consécutifs (même sens, trous de max_gap points au plus)."""
    groups: list[list[tuple[int, MetricPoint, float]]] = []
    for item in flagged:
        last = groups[-1][-1] if groups else None
        same_run = (
            last is not None and item[0] - last[0] <= max_gap + 1 and (item[2] > 0) == (last[2] > 0)
        )
        if same_run:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _round(value: float) -> float:
    return round(value, 6)
