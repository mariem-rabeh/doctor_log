"""Trace Agent : transforme les traces d'une fenêtre en un Finding structuré.

Même modèle que le Log Agent :
1. Python reconstruit les arbres, détecte le début de l'incident sur la latence de bout
   en bout, compare le temps propre de chaque opération avant / après, et repère où
   naissent les erreurs.
2. Python crée les Evidence (trc-001, ...) avec les vrais chiffres.
3. Le LLM reçoit uniquement ce résumé compact et l'interprète.
4. Python vérifie les identifiants cités, puis assemble le Finding.
"""

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agents.common import AgentInterpretation, detect_change_point, ground
from app.agents.prompts.trace import SYSTEM_PROMPT, build_user_prompt
from app.schemas.diagnosis import Evidence, Finding
from app.schemas.enums import SignalSource
from app.schemas.telemetry import TimeWindow
from app.services.llm.base import LLMClient
from app.tools.datasources.base import DataSource
from app.tools.log_tools import floor_time
from app.tools.trace_tools import (
    ErrorSpan,
    SpanNode,
    TraceTree,
    build_trace_tree,
    find_error_spans,
    percentile,
)

logger = logging.getLogger(__name__)

BUCKET = timedelta(minutes=1)
MIN_SEGMENT_BUCKETS = 2
ONSET_MIN_RATIO = 1.5  # latence p95 après >= 1,5 x avant ...
ONSET_MIN_DELTA_MS = 100.0  # ... et au moins +100 ms
DEGRADATION_RATIO = 3.0  # temps propre p95 après >= 3 x avant ...
DEGRADATION_MIN_DELTA_MS = 100.0  # ... et au moins +100 ms (évite 2 ms -> 7 ms)
MAX_DEGRADED_EVIDENCES = 6
MAX_ERROR_EVIDENCES = 4
MAX_SAMPLE_TRACE_IDS = 3
_EVIDENCE_ID = re.compile(r"\btrc-\d{3,}\b")


class TraceInterpretation(AgentInterpretation):
    """Réponse attendue du LLM pour les traces (voir AgentInterpretation)."""


@dataclass
class OperationStats:
    service: str
    operation: str
    before: list[float] = field(default_factory=list)  # temps propres avant le début
    after: list[float] = field(default_factory=list)  # temps propres pendant l'incident
    after_nodes: list[SpanNode] = field(default_factory=list)

    @property
    def before_p95(self) -> float | None:
        return percentile(self.before, 95) if self.before else None

    @property
    def after_p95(self) -> float | None:
        return percentile(self.after, 95) if self.after else None

    @property
    def increase_ms(self) -> float:
        return (self.after_p95 or 0.0) - (self.before_p95 or 0.0)


@dataclass
class ErrorOrigin:
    service: str
    operation: str
    error_type: str | None
    spans: list[ErrorSpan]


# --- Point d'entrée --------------------------------------------------------------------------


def run(source: DataSource, window: TimeWindow, llm: LLMClient) -> Finding:
    spans = source.get_traces(window)
    if not spans:
        return Finding(
            agent=SignalSource.TRACES,
            summary="Aucune trace dans la fenêtre analysée.",
            missing_data=["traces : aucune trace dans la fenêtre"],
        )

    trees = build_trace_tree(spans)
    missing_data: list[str] = []
    orphans = sum(tree.orphan_count for tree in trees)
    if orphans:
        missing_data.append(f"traces : {orphans} span(s) sans parent, traces incomplètes")

    onset = detect_onset(trees, window)
    degraded = find_degraded_operations(trees, onset)
    origins = group_error_origins(find_error_spans(spans))
    evidences, samples = build_evidences(trees, degraded, origins, window, onset)
    digest = build_digest(
        trees,
        spans_count=len(spans),
        evidences=evidences,
        samples=samples,
        window=window,
        onset=onset,
        orphans=orphans,
    )

    try:
        interpretation: TraceInterpretation | None = llm.complete_structured(
            SYSTEM_PROMPT, build_user_prompt(digest), TraceInterpretation
        )
    except Exception as exc:  # le client LLM peut lever n'importe quelle erreur réseau
        logger.warning("Trace Agent : interprétation LLM indisponible (%s)", exc)
        missing_data.append(f"traces : interprétation LLM indisponible ({type(exc).__name__})")
        interpretation = None

    text = ground(
        interpretation, {e.id for e in evidences}, _EVIDENCE_ID, fallback_summary(evidences, digest)
    )
    anomalies = list(text.anomalies)
    if text.dropped:
        missing_data.append(
            f"traces : {text.dropped} élément(s) du LLM écarté(s), preuve citée inconnue"
        )
    hidden = max(0, len(degraded) - MAX_DEGRADED_EVIDENCES)
    if hidden:
        anomalies.append(f"{hidden} autre(s) opération(s) dégradée(s) moins touchée(s).")

    return Finding(
        agent=SignalSource.TRACES,
        summary=text.summary,
        evidences=evidences,
        anomalies=anomalies,
        missing_data=missing_data,
    )


# --- 1. Début de l'incident --------------------------------------------------------------------


def latency_by_minute(
    trees: Sequence[TraceTree], window: TimeWindow
) -> list[tuple[datetime, float]]:
    """p95 de la latence de bout en bout (durée des traces) pour chaque minute non vide."""
    durations: dict[datetime, list[float]] = {}
    for tree in trees:
        if window.contains(tree.start):
            durations.setdefault(floor_time(tree.start, BUCKET), []).append(tree.duration_ms)
    return [(minute, percentile(values, 95)) for minute, values in sorted(durations.items())]


def detect_onset(trees: Sequence[TraceTree], window: TimeWindow) -> datetime | None:
    """Minute où la latence de bout en bout décolle, ou None si elle reste stable.

    On travaille sur la latence (et non sur les erreurs) : une dépendance lente sans erreur,
    comme dans le cas piège, doit aussi être détectée.
    """
    series = latency_by_minute(trees, window)
    k = detect_change_point(
        [value for _, value in series],
        min_segment=MIN_SEGMENT_BUCKETS,
        min_ratio=ONSET_MIN_RATIO,
        min_delta=ONSET_MIN_DELTA_MS,
    )
    return None if k is None else series[k][0]


# --- 2. Opérations dégradées -------------------------------------------------------------------


def find_degraded_operations(
    trees: Sequence[TraceTree], onset: datetime | None
) -> list[OperationStats]:
    """Opérations dont le temps propre p95 a explosé après le début de l'incident.

    Dégradée : p95 après >= 3 x p95 avant ET +100 ms au moins. Une opération qui n'existait
    pas avant est dégradée si son p95 dépasse 100 ms. Triées par hausse décroissante.
    """
    if onset is None:
        return []
    stats: dict[tuple[str, str], OperationStats] = {}
    for tree in trees:
        for node in tree.nodes:
            key = (node.span.service, node.span.operation)
            op = stats.setdefault(key, OperationStats(*key))
            if node.span.start < onset:
                op.before.append(node.self_ms)
            else:
                op.after.append(node.self_ms)
                op.after_nodes.append(node)

    degraded = [op for op in stats.values() if _is_degraded(op)]
    degraded.sort(key=lambda op: (-op.increase_ms, op.service, op.operation))
    return degraded


def _is_degraded(op: OperationStats) -> bool:
    after, before = op.after_p95, op.before_p95
    if after is None:
        return False
    if before is None:
        return after >= DEGRADATION_MIN_DELTA_MS
    return after >= DEGRADATION_RATIO * before and after - before >= DEGRADATION_MIN_DELTA_MS


# --- 3. Origines d'erreur ----------------------------------------------------------------------


def group_error_origins(errors: Sequence[ErrorSpan]) -> list[ErrorOrigin]:
    """Regroupe les spans où naissent les erreurs par (service, opération, type d'erreur)."""
    groups: dict[tuple[str, str, str | None], list[ErrorSpan]] = {}
    for error in errors:
        if error.is_origin:
            key = (error.service, error.operation, error.error_type)
            groups.setdefault(key, []).append(error)
    origins = [
        ErrorOrigin(*key, spans=sorted(v, key=lambda e: e.start)) for key, v in groups.items()
    ]
    origins.sort(key=lambda o: (-len(o.spans), o.service, o.operation))
    return origins


# --- 4. Evidence --------------------------------------------------------------------------------


def build_evidences(
    trees: Sequence[TraceTree],
    degraded: Sequence[OperationStats],
    origins: Sequence[ErrorOrigin],
    window: TimeWindow,
    onset: datetime | None,
) -> tuple[list[Evidence], dict[str, list[str]]]:
    """Ordre : opérations dégradées (les coupables), origines d'erreur, puis le symptôme
    (latence de bout en bout). Renvoie aussi quelques trace_id d'exemple par preuve."""
    evidences: list[Evidence] = []
    samples: dict[str, list[str]] = {}

    def add(evidence: Evidence, trace_ids: Sequence[str]) -> None:
        evidence = evidence.model_copy(update={"id": f"trc-{len(evidences) + 1:03d}"})
        evidences.append(evidence)
        samples[evidence.id] = list(dict.fromkeys(trace_ids))[:MAX_SAMPLE_TRACE_IDS]

    total_after = sum(n.self_ms for t in trees for n in t.nodes if onset and n.span.start >= onset)
    for op in degraded[:MAX_DEGRADED_EVIDENCES]:
        nodes = sorted(op.after_nodes, key=lambda n: (-n.self_ms, n.span.start))
        share = sum(op.after) / total_after if total_after else 0.0
        before = f"{op.before_p95:.0f} ms" if op.before_p95 is not None else "absente"
        add(
            Evidence(
                id="trc-000",
                source=SignalSource.TRACES,
                service=op.service,
                period_start=min(n.span.start for n in op.after_nodes),
                period_end=max(n.end for n in op.after_nodes),
                observation=(
                    f"{op.operation} : temps propre p95 {before} avant, "
                    f"{op.after_p95:.0f} ms pendant l'incident ({len(op.after)} spans, "
                    f"{share:.0%} du temps propre total pendant l'incident)"
                ),
                value=round(op.after_p95 or 0.0, 1),
                unit="ms",
            ),
            [n.span.trace_id for n in nodes],
        )

    for origin in origins[:MAX_ERROR_EVIDENCES]:
        kind = f" ({origin.error_type})" if origin.error_type else ""
        add(
            Evidence(
                id="trc-000",
                source=SignalSource.TRACES,
                service=origin.service,
                period_start=origin.spans[0].start,
                period_end=origin.spans[-1].start,
                observation=(
                    f"Les erreurs naissent dans {origin.operation}{kind} : "
                    f"{len(origin.spans)} span(s) d'origine, propagées aux appelants"
                ),
                value=len(origin.spans),
                unit="error spans",
            ),
            [e.trace_id for e in origin.spans],
        )

    symptom = _end_to_end(trees, window, onset)
    if symptom is not None:
        add(*symptom)
    return evidences, samples


def _end_to_end(
    trees: Sequence[TraceTree], window: TimeWindow, onset: datetime | None
) -> tuple[Evidence, list[str]] | None:
    """La latence vue par l'utilisateur : p95 avant / pendant (ou sur toute la fenêtre)."""
    if not trees:
        return None
    root_service = Counter(r.span.service for t in trees for r in t.roots).most_common(1)[0][0]
    after = [t for t in trees if onset is None or t.start >= onset]
    before = [t for t in trees if onset is not None and t.start < onset]
    if not after:
        return None
    after_p95 = percentile([t.duration_ms for t in after], 95)
    if before:
        before_p95 = percentile([t.duration_ms for t in before], 95)
        observation = (
            f"Latence de bout en bout p95 : {before_p95:.0f} ms avant, "
            f"{after_p95:.0f} ms pendant l'incident ({len(after)} traces)"
        )
    else:
        observation = f"Latence de bout en bout p95 : {after_p95:.0f} ms ({len(after)} traces)"
    slowest = sorted(after, key=lambda t: -t.duration_ms)
    evidence = Evidence(
        id="trc-000",
        source=SignalSource.TRACES,
        service=root_service,
        period_start=onset or window.start,
        period_end=window.end,
        observation=observation,
        value=round(after_p95, 1),
        unit="ms",
    )
    return evidence, [t.trace_id for t in slowest]


# --- 5. Résumé pour le LLM ----------------------------------------------------------------------


def build_digest(
    trees: Sequence[TraceTree],
    *,
    spans_count: int,
    evidences: Sequence[Evidence],
    samples: dict[str, list[str]],
    window: TimeWindow,
    onset: datetime | None,
    orphans: int,
) -> dict[str, Any]:
    """Résumé compact : quelques dizaines de lignes, quel que soit le nombre de spans."""
    return {
        "window": {"start": window.start, "end": window.end},
        "incident_onset": onset,
        "traces": len(trees),
        "spans": spans_count,
        "orphan_spans": orphans,
        "services": sorted({n.span.service for t in trees for n in t.nodes}),
        "evidences": [
            {
                "id": e.id,
                "service": e.service,
                "observation": e.observation,
                "value": e.value,
                "unit": e.unit,
                "period_start": e.period_start,
                "period_end": e.period_end,
                "example_trace_ids": samples.get(e.id, []),
            }
            for e in evidences
        ],
    }


def fallback_summary(evidences: Sequence[Evidence], digest: dict[str, Any]) -> str:
    """Résumé factuel écrit par Python, utilisé si le LLM est indisponible ou hors sujet."""
    onset = digest["incident_onset"]
    start = f"début détecté à {onset:%H:%M} UTC" if onset else "aucun début d'incident détecté"
    top = ", ".join(f"{e.id} ({e.service}, {e.value} {e.unit})" for e in evidences[:3])
    return f"{digest['traces']} traces analysées ; {start}. Principales preuves : {top}."
