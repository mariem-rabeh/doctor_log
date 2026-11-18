"""Énumérations partagées par les schémas, le graphe, l'API et la base de données.

Les valeurs sont en minuscules : c'est ce qui apparaît dans le JSON de l'API,
dans la colonne de base de données et dans les types TypeScript du frontend.
"""

from enum import StrEnum


class IncidentStatus(StrEnum):
    """Cycle de vie d'un incident, de sa création à la fin de l'analyse."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(StrEnum):
    """État d'un agent du graphe, affiché sur la page Investigation Details."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentName(StrEnum):
    """Les 5 agents du graphe : clés de IncidentState.agent_statuses."""

    LOG = "log"
    METRICS = "metrics"
    TRACE = "trace"
    RCA = "rca"
    VERIFICATION = "verification"


class Verdict(StrEnum):
    """Décision du Verification Agent sur les hypothèses du RCA Agent."""

    ACCEPTED = "accepted"
    REVISION_REQUESTED = "revision_requested"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class SignalSource(StrEnum):
    """Type de télémétrie d'où provient une preuve."""

    LOGS = "logs"
    METRICS = "metrics"
    TRACES = "traces"


class Conclusion(StrEnum):
    """Libellé final affiché dans le rapport, déduit du verdict et de la confiance."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
