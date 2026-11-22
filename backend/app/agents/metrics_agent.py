"""Metrics Agent : transforme les métriques d'une fenêtre en un Finding structuré.

Même modèle que le Log Agent et le Trace Agent :
1. Python repère les métriques attendues mais absentes (-> missing_data), détecte le début
   de l'incident (première anomalie franche), puis compare chaque série à sa période normale.
2. Python crée les Evidence (met-001, ...) : d'abord les séries anormales, puis les
   métriques clés restées stables (elles servent à écarter des causes).
3. Le LLM reçoit uniquement ce résumé compact et l'interprète.
4. Python vérifie les identifiants cités, puis assemble le Finding.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agents.common import AgentInterpretation, ground
from app.agents.prompts.metrics import SYSTEM_PROMPT, build_user_prompt
from app.schemas.diagnosis import Evidence, Finding
from app.schemas.enums import SignalSource
from app.schemas.telemetry import MetricSeries, TimeWindow
from app.services.llm.base import LLMClient
from app.tools.datasources.base import DataSource
from app.tools.metrics_tools import (
    Anomaly,
    SeriesStats,
    change_pct,
    detect_anomalies,
    missing_metrics,
    series_stats,
)

logger = logging.getLogger(__name__)

EXPECTED_METRICS = (
    "http_request_duration_p95",
    "error_rate",
    "db_connections_active",
    "db_connections_idle",
    "cpu_usage",
)
ONSET_MIN_POINTS = 3  # une anomalie de moins de 3 points ne fixe pas le début de l'incident
MAX_ANOMALY_EVIDENCES = 8
MAX_STABLE_EVIDENCES = 5
UNITS = {
    "http_request_duration_p95": "s",
    "error_rate": "ratio",
    "cpu_usage": "ratio",
    "db_connections_active": "connections",
    "db_connections_idle": "connections",
    "up": "up",
}
_EVIDENCE_ID = re.compile(r"\bmet-\d{3,}\b")


class MetricsInterpretation(AgentInterpretation):
    """Réponse attendue du LLM pour les métriques (voir AgentInterpretation)."""


@dataclass
class SeriesReport:
    """Une série et ce qu'on en a tiré : stats avant / pendant et anomalies fusionnées."""

    series: MetricSeries
    before: SeriesStats | None
    during: SeriesStats | None
    anomalies: list[Anomaly]

    @property
    def label(self) -> str:
        extra = {k: v for k, v in self.series.labels.items() if k != "service"}
        suffix = ", ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        return f"{self.series.name}{{{suffix}}}" if suffix else self.series.name

    @property
    def service(self) -> str:
        return self.series.labels.get("service", "unknown")

    @property
    def peak(self) -> Anomaly | None:
        return max(self.anomalies, key=lambda a: abs(a.peak_z), default=None)


# --- Point d'entrée --------------------------------------------------------------------------


def run(
    source: DataSource,
    window: TimeWindow,
    llm: LLMClient,
    *,
    expected: Sequence[str] = EXPECTED_METRICS,
) -> Finding:
    series = [s for s in source.get_metrics(window) if s.points]
    missing = missing_metrics(series, expected)
    missing_data = [f"metrics : métrique absente : {name}" for name in missing]

    if not series:
        return Finding(
            agent=SignalSource.METRICS,
            summary="Aucune métrique dans la fenêtre analysée.",
            missing_data=missing_data or ["metrics : aucune métrique dans la fenêtre"],
        )

    onset = detect_onset(series)
    reports = analyze(series, onset)
    evidences = build_evidences(reports, expected, window, onset)
    digest = build_digest(reports, evidences, window, onset, missing)

    try:
        interpretation: MetricsInterpretation | None = llm.complete_structured(
            SYSTEM_PROMPT, build_user_prompt(digest), MetricsInterpretation
        )
    except Exception as exc:  # le client LLM peut lever n'importe quelle erreur réseau
        logger.warning("Metrics Agent : interprétation LLM indisponible (%s)", exc)
        missing_data.append(f"metrics : interprétation LLM indisponible ({type(exc).__name__})")
        interpretation = None

    text = ground(
        interpretation,
        {e.id for e in evidences},
        _EVIDENCE_ID,
        fallback_summary(evidences, digest),
    )
    if text.dropped:
        missing_data.append(
            f"metrics : {text.dropped} élément(s) du LLM écarté(s), preuve citée inconnue"
        )
    anomalous = [r for r in reports if r.anomalies]
    anomalies = list(text.anomalies)
    hidden = max(0, len(anomalous) - MAX_ANOMALY_EVIDENCES)
    if hidden:
        anomalies.append(f"{hidden} autre(s) série(s) anormale(s) moins marquée(s).")

    return Finding(
        agent=SignalSource.METRICS,
        summary=text.summary,
        evidences=evidences,
        anomalies=anomalies,
        missing_data=missing_data,
    )


# --- 1. Début de l'incident --------------------------------------------------------------------


def detect_onset(series: Sequence[MetricSeries]) -> datetime | None:
    """Début de la première anomalie franche (au moins 3 points) parmi toutes les séries.

    La baseline est ici le premier quart de chaque série (on ne connaît pas encore le début).
    """
    starts = [
        anomaly.start
        for s in series
        for anomaly in detect_anomalies(s)
        if anomaly.point_count >= ONSET_MIN_POINTS
    ]
    return min(starts, default=None)


# --- 2. Analyse de chaque série ----------------------------------------------------------------


def analyze(series: Sequence[MetricSeries], onset: datetime | None) -> list[SeriesReport]:
    """Stats avant / pendant et anomalies, la baseline étant tout ce qui précède le début.

    Sans début détecté, il n'y a pas de comparaison possible : les stats portent sur toute
    la fenêtre (champ `during`) et aucune anomalie n'est retenue.
    """
    reports = []
    for s in series:
        if onset is None:
            reports.append(SeriesReport(s, None, series_stats([p.value for p in s.points]), []))
            continue
        reports.append(
            SeriesReport(
                series=s,
                before=series_stats([p.value for p in s.points if p.ts < onset]),
                during=series_stats([p.value for p in s.points if p.ts >= onset]),
                anomalies=detect_anomalies(s, baseline_end=onset),
            )
        )
    return reports


# --- 3. Evidence --------------------------------------------------------------------------------


def build_evidences(
    reports: Sequence[SeriesReport],
    expected: Sequence[str],
    window: TimeWindow,
    onset: datetime | None,
) -> list[Evidence]:
    """Séries anormales (les plus marquées d'abord), puis métriques clés restées stables."""
    evidences: list[Evidence] = []

    def add(evidence: Evidence) -> None:
        evidences.append(evidence.model_copy(update={"id": f"met-{len(evidences) + 1:03d}"}))

    anomalous = sorted(
        (r for r in reports if r.peak is not None),
        key=lambda r: (-abs(r.peak.peak_z) if r.peak else 0, r.label),
    )
    for report in anomalous[:MAX_ANOMALY_EVIDENCES]:
        add(_anomaly_evidence(report))

    stable = [r for r in reports if not r.anomalies and r.series.name in expected]
    stable.sort(key=lambda r: (expected.index(r.series.name), r.label))
    for report in stable[:MAX_STABLE_EVIDENCES]:
        add(_stable_evidence(report, window, onset))
    return evidences


def _anomaly_evidence(report: SeriesReport) -> Evidence:
    peak = report.peak
    assert peak is not None
    before, during = report.before, report.during
    word = "hausse" if peak.direction == "up" else "baisse"
    observation = f"{report.label} : {word} anormale"
    if before and during:
        change = change_pct(before.mean, during.mean)
        trend = f", {change:+.0f} %" if change is not None else ""
        observation += (
            f" (moyenne {_fmt(before.mean)} avant, {_fmt(during.mean)} pendant{trend} ; "
            f"min {_fmt(during.min)}, max {_fmt(during.max)} pendant)"
        )
    observation += f", pic {_fmt(peak.peak_value)} à {peak.peak_ts:%H:%M:%S} (z={peak.peak_z:+.1f})"
    if len(report.anomalies) > 1:
        observation += f", en {len(report.anomalies)} épisodes"
    return Evidence(
        id="met-000",
        source=SignalSource.METRICS,
        service=report.service,
        period_start=min(a.start for a in report.anomalies),
        period_end=max(a.end for a in report.anomalies),
        observation=observation,
        value=peak.peak_value,
        unit=UNITS.get(report.series.name),
    )


def _stable_evidence(report: SeriesReport, window: TimeWindow, onset: datetime | None) -> Evidence:
    during = report.during
    assert during is not None
    if report.before is not None:
        observation = (
            f"{report.label} : stable (moyenne {_fmt(report.before.mean)} avant, "
            f"{_fmt(during.mean)} pendant ; max {_fmt(during.max)} pendant)"
        )
    else:
        observation = f"{report.label} : moyenne {_fmt(during.mean)}, max {_fmt(during.max)}"
    return Evidence(
        id="met-000",
        source=SignalSource.METRICS,
        service=report.service,
        period_start=onset or window.start,
        period_end=window.end,
        observation=observation,
        value=during.mean,
        unit=UNITS.get(report.series.name),
    )


def _fmt(value: float) -> str:
    return f"{value:.3g}" if abs(value) < 1000 else f"{value:.0f}"


# --- 4. Résumé pour le LLM ----------------------------------------------------------------------


def build_digest(
    reports: Sequence[SeriesReport],
    evidences: Sequence[Evidence],
    window: TimeWindow,
    onset: datetime | None,
    missing: Sequence[str],
) -> dict[str, Any]:
    return {
        "window": {"start": window.start, "end": window.end},
        "incident_onset": onset,
        "series": len(reports),
        "missing_metrics": list(missing),
        "evidences": [
            {
                "id": e.id,
                "service": e.service,
                "kind": "stable" if ": stable" in e.observation else "anomaly",
                "observation": e.observation,
                "value": e.value,
                "unit": e.unit,
                "period_start": e.period_start,
                "period_end": e.period_end,
            }
            for e in evidences
        ],
    }


def fallback_summary(evidences: Sequence[Evidence], digest: dict[str, Any]) -> str:
    """Résumé factuel écrit par Python, utilisé si le LLM est indisponible ou hors sujet."""
    onset = digest["incident_onset"]
    start = f"début détecté à {onset:%H:%M} UTC" if onset else "aucun début d'incident détecté"
    top = ", ".join(f"{e.id} ({e.observation.split(' :')[0]})" for e in evidences[:3])
    missing = digest["missing_metrics"]
    absent = f" Métriques absentes : {', '.join(missing)}." if missing else ""
    return f"{digest['series']} séries analysées ; {start}. Principales preuves : {top}.{absent}"
