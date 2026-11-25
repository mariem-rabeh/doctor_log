"""État partagé du graphe LangGraph.

Chaque nœud reçoit l'état complet et renvoie un dict partiel avec seulement les clés
qu'il modifie. LangGraph fusionne ce dict dans l'état.
"""

from typing import Annotated, TypedDict

from app.schemas.diagnosis import (
    DiagnosisReport,
    Document,
    Finding,
    Hypothesis,
    VerificationResult,
)
from app.schemas.enums import AgentName, AgentStatus
from app.schemas.telemetry import TimeWindow  # fourni par la branche 2 (contracts/interfaces)


def merge_statuses(
    left: dict[AgentName, AgentStatus] | None,
    right: dict[AgentName, AgentStatus] | None,
) -> dict[AgentName, AgentStatus]:
    """Reducer de agent_statuses : fusionne au lieu d'écraser.

    Les agents Log, Metrics et Trace tournent en parallèle et écrivent chacun leur
    propre statut au même pas. Sans ce reducer, LangGraph lève InvalidUpdateError.
    """
    return {**(left or {}), **(right or {})}


class IncidentState(TypedDict, total=False):
    """Tout ce que le graphe sait d'un incident pendant l'analyse."""

    # Entrées, fixées par le nœud prepare
    incident_id: str
    time_window: TimeWindow

    # Constats des trois agents d'analyse (None si l'agent n'a pas encore tourné)
    log_finding: Finding | None
    metrics_finding: Finding | None
    trace_finding: Finding | None

    # Raisonnement
    documents: list[Document]  # runbooks retrouvés par le RAG (connaissances, pas des preuves)
    hypotheses: list[Hypothesis]
    verification: VerificationResult | None
    iteration_count: int

    # Sortie
    final_report: DiagnosisReport | None

    # Suivi, lu par GET /status et la page Investigation Details
    agent_statuses: Annotated[dict[AgentName, AgentStatus], merge_statuses]
