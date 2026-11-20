"""Log Agent : transforme les logs d'une fenêtre en un Finding structuré.

Déroulement :
1. Python lit les logs, détecte le début de l'incident, regroupe les messages et
   classe chaque groupe (nouveau, amplifié, constant).
2. Python crée les Evidence (log-001, ...) avec les vrais chiffres.
3. Le LLM reçoit uniquement ce résumé compact et l'interprète (texte, anomalies, bruit).
4. Python vérifie que le LLM ne cite que des identifiants existants, puis assemble le Finding.

Si le LLM échoue, le Finding est quand même renvoyé avec les preuves et un résumé de secours.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.agents.common import AgentInterpretation, detect_change_point, ground
from app.agents.prompts.log import SYSTEM_PROMPT, build_user_prompt
from app.schemas.diagnosis import Evidence, Finding
from app.schemas.enums import SignalSource
from app.schemas.telemetry import LogLevel, LogRecord, TimeWindow
from app.services.llm.base import LLMClient
from app.tools.datasources.base import DataSource
from app.tools.log_tools import (
    ERROR_LEVELS,
    PROBLEM_LEVELS,
    count_errors,
    exception_type,
    floor_time,
    normalize_message,
)

logger = logging.getLogger(__name__)

BUCKET = timedelta(minutes=1)
MAX_GROUP_EVIDENCES = 8  # au-delà, le prompt grossit sans apporter d'information
MIN_SEGMENT_BUCKETS = 2  # le début détecté laisse au moins 2 min de chaque côté
ONSET_MIN_RATIO = 1.5  # après >= 1,5 x avant ...
ONSET_MIN_DELTA_PER_MIN = 1.0  # ... et au moins +1 log problématique par minute
AMPLIFICATION_RATIO = 3.0
_EVIDENCE_ID = re.compile(r"\blog-\d{3,}\b")


class GroupCategory(StrEnum):
    NEW = "new"
    AMPLIFIED = "amplified"
    STEADY = "steady"
    UNCLASSIFIED = "unclassified"


_CATEGORY_ORDER = {
    GroupCategory.NEW: 0,
    GroupCategory.AMPLIFIED: 1,
    GroupCategory.STEADY: 2,
    GroupCategory.UNCLASSIFIED: 2,
}


class LogInterpretation(AgentInterpretation):
    """Réponse attendue du LLM pour les logs (voir AgentInterpretation)."""


@dataclass
class _Group:
    service: str
    level: LogLevel
    pattern: str
    records: list[LogRecord]
    before: int = 0
    after: int = 0
    category: GroupCategory = GroupCategory.UNCLASSIFIED


# --- Point d'entrée --------------------------------------------------------------------------


def run(source: DataSource, window: TimeWindow, llm: LLMClient) -> Finding:
    records = sorted(source.get_logs(window), key=lambda r: r.timestamp)
    problems = [r for r in records if r.level in PROBLEM_LEVELS]

    if not records:
        return Finding(
            agent=SignalSource.LOGS,
            summary="Aucun log dans la fenêtre analysée.",
            missing_data=["logs : aucun log dans la fenêtre"],
        )
    if not problems:
        return Finding(
            agent=SignalSource.LOGS,
            summary=f"{len(records)} logs analysés, aucun de niveau WARNING ou plus.",
        )

    onset = detect_onset(problems, window)
    groups = classify_groups(problems, window, onset)
    evidences, group_of = build_evidences(groups, problems, window, onset)
    digest = build_digest(records, problems, evidences, group_of, window, onset)

    missing_data: list[str] = []
    try:
        interpretation = llm.complete_structured(
            SYSTEM_PROMPT, build_user_prompt(digest), LogInterpretation
        )
    except Exception as exc:  # le client LLM peut lever n'importe quelle erreur réseau
        logger.warning("Log Agent : interprétation LLM indisponible (%s)", exc)
        missing_data.append(f"logs : interprétation LLM indisponible ({type(exc).__name__})")
        interpretation = None

    text = ground(
        interpretation,
        {e.id for e in evidences},
        _EVIDENCE_ID,
        fallback_summary(evidences, digest),
    )
    anomalies = list(text.anomalies)
    if text.dropped:
        missing_data.append(
            f"logs : {text.dropped} élément(s) du LLM écarté(s), preuve citée inconnue"
        )
    hidden = len(groups) - min(len(groups), MAX_GROUP_EVIDENCES)
    if hidden:
        anomalies.append(f"{hidden} autre(s) motif(s) de log moins fréquent(s) non détaillé(s).")

    return Finding(
        agent=SignalSource.LOGS,
        summary=text.summary,
        evidences=evidences,
        anomalies=anomalies,
        missing_data=missing_data,
    )


# --- 1. Début de l'incident --------------------------------------------------------------------


def bucket_counts(records: Sequence[LogRecord], window: TimeWindow) -> tuple[datetime, list[int]]:
    """Nombre de logs par minute sur toute la fenêtre, minutes vides comprises."""
    start = floor_time(window.start, BUCKET)
    size = -(-(window.end - start) // BUCKET)  # division arrondie au supérieur
    counts = [0] * size
    for record in records:
        index = (floor_time(record.timestamp, BUCKET) - start) // BUCKET
        if 0 <= index < size:
            counts[index] += 1
    return start, counts


def detect_onset(problems: Sequence[LogRecord], window: TimeWindow) -> datetime | None:
    """Minute où les logs problématiques décollent, ou None si rien ne change."""
    start, counts = bucket_counts(problems, window)
    k = detect_change_point(
        counts,
        min_segment=MIN_SEGMENT_BUCKETS,
        min_ratio=ONSET_MIN_RATIO,
        min_delta=ONSET_MIN_DELTA_PER_MIN,
    )
    return None if k is None else start + k * BUCKET


# --- 2. Groupes et catégories ------------------------------------------------------------------


def classify_groups(
    problems: Sequence[LogRecord], window: TimeWindow, onset: datetime | None
) -> list[_Group]:
    """Regroupe par (service, niveau, motif) et compare le rythme avant / après le début."""
    groups: dict[tuple[str, LogLevel, str], _Group] = {}
    for record in problems:
        key = (record.service, record.level, normalize_message(record.message))
        group = groups.setdefault(key, _Group(*key, records=[]))
        group.records.append(record)
        if onset is not None:
            if record.timestamp < onset:
                group.before += 1
            else:
                group.after += 1

    for group in groups.values():
        group.category = _categorize(group, window, onset)

    return sorted(
        groups.values(),
        key=lambda g: (_CATEGORY_ORDER[g.category], -len(g.records), g.service, g.pattern),
    )


def _categorize(group: _Group, window: TimeWindow, onset: datetime | None) -> GroupCategory:
    if onset is None:
        return GroupCategory.UNCLASSIFIED
    if group.before == 0:
        return GroupCategory.NEW
    before_rate, after_rate = _rates(group, window, onset)
    if after_rate >= AMPLIFICATION_RATIO * before_rate:
        return GroupCategory.AMPLIFIED
    return GroupCategory.STEADY


def _rates(group: _Group, window: TimeWindow, onset: datetime) -> tuple[float, float]:
    before_minutes = max((onset - window.start) / BUCKET, 1e-9)
    after_minutes = max((window.end - onset) / BUCKET, 1e-9)
    return group.before / before_minutes, group.after / after_minutes


# --- 3. Evidence --------------------------------------------------------------------------------

_CATEGORY_LABEL = {
    GroupCategory.NEW: "nouveau depuis le début de l'incident",
    GroupCategory.AMPLIFIED: "existait déjà, fortement amplifié",
    GroupCategory.STEADY: "existait déjà au même rythme",
    GroupCategory.UNCLASSIFIED: "aucun début d'incident détecté",
}


def build_evidences(
    groups: Sequence[_Group],
    problems: Sequence[LogRecord],
    window: TimeWindow,
    onset: datetime | None,
) -> tuple[list[Evidence], dict[str, _Group]]:
    """Renvoie les Evidence et, pour celles qui viennent d'un groupe, le groupe d'origine."""
    evidences: list[Evidence] = []
    group_of: dict[str, _Group] = {}
    for group in groups[:MAX_GROUP_EVIDENCES]:
        observation = f"{group.level} « {group.pattern} » : {_CATEGORY_LABEL[group.category]}"
        if onset is not None:
            before_rate, after_rate = _rates(group, window, onset)
            observation += f" (avant {before_rate:.1f}/min, pendant {after_rate:.1f}/min)"
        evidences.append(
            Evidence(
                id=f"log-{len(evidences) + 1:03d}",
                source=SignalSource.LOGS,
                service=group.service,
                period_start=group.records[0].timestamp,
                period_end=group.records[-1].timestamp,
                observation=observation,
                value=len(group.records),
                unit="occurrences",
            )
        )
        group_of[evidences[-1].id] = group

    peak = _peak_errors(problems, window, onset)
    if peak is not None:
        evidences.append(peak.model_copy(update={"id": f"log-{len(evidences) + 1:03d}"}))
    return evidences, group_of


def _peak_errors(
    problems: Sequence[LogRecord], window: TimeWindow, onset: datetime | None
) -> Evidence | None:
    """La minute la plus chargée en ERROR/CRITICAL pour un service."""
    counts = count_errors(problems, levels=ERROR_LEVELS, bucket=BUCKET)
    if not counts:
        return None
    peak = max(counts, key=lambda c: (c.count, -c.bucket_start.timestamp()))
    observation = f"Pic de {peak.count} logs ERROR/CRITICAL en une minute"
    if onset is not None:
        before = sum(
            c.count for c in counts if c.service == peak.service and c.bucket_start < onset
        )
        before_minutes = max((onset - window.start) / BUCKET, 1e-9)
        observation += f" (avant le début de l'incident : {before / before_minutes:.1f}/min)"
    return Evidence(
        id="log-000",  # renuméroté par l'appelant
        source=SignalSource.LOGS,
        service=peak.service,
        period_start=peak.bucket_start,
        period_end=min(peak.bucket_start + BUCKET, window.end),
        observation=observation,
        value=peak.count,
        unit="errors/min",
    )


# --- 4. Résumé pour le LLM ----------------------------------------------------------------------


def build_digest(
    records: Sequence[LogRecord],
    problems: Sequence[LogRecord],
    evidences: Sequence[Evidence],
    group_of: dict[str, _Group],
    window: TimeWindow,
    onset: datetime | None,
) -> dict[str, Any]:
    """Résumé compact : quelques dizaines de lignes, quel que soit le volume de logs."""
    items = []
    for evidence in evidences:
        item: dict[str, Any] = {
            "id": evidence.id,
            "service": evidence.service,
            "observation": evidence.observation,
            "value": evidence.value,
            "unit": evidence.unit,
            "first_seen": evidence.period_start,
            "last_seen": evidence.period_end,
        }
        group = group_of.get(evidence.id)
        if group is not None:
            item["category"] = group.category
            sample = next((r.exception for r in group.records if r.exception), None)
            if sample:
                item["exception_type"] = exception_type(sample)
            item["traced_occurrences"] = sum(1 for r in group.records if r.trace_id)
        items.append(item)

    return {
        "window": {"start": window.start, "end": window.end},
        "incident_onset": onset,
        "total_logs": len(records),
        "problem_logs": len(problems),
        "services": sorted({r.service for r in records}),
        "evidences": items,
    }


# --- 5. Contrôle de la réponse du LLM ----------------------------------------------------------


def fallback_summary(evidences: Sequence[Evidence], digest: dict[str, Any]) -> str:
    """Résumé factuel écrit par Python, utilisé si le LLM est indisponible ou hors sujet."""
    onset = digest["incident_onset"]
    start = f"début détecté à {onset:%H:%M} UTC" if onset else "aucun début d'incident détecté"
    top = ", ".join(f"{e.id} ({e.service}, {e.value} {e.unit})" for e in evidences[:3])
    return (
        f"{digest['total_logs']} logs analysés dont {digest['problem_logs']} WARNING ou plus ; "
        f"{start}. Principaux motifs : {top}."
    )
