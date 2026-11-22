"""Accès aux incidents : la seule couche qui écrit des requêtes SQL.

L'API et le graphe appellent ces fonctions au lieu de manipuler la session directement.
Chaque fonction d'écriture valide (commit) sa transaction.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Incident, IncidentSource
from app.schemas.diagnosis import DiagnosisReport
from app.schemas.enums import AgentName, AgentStatus, IncidentStatus

MAX_PAGE_SIZE = 100


def create_incident(
    db: Session,
    *,
    title: str,
    source: IncidentSource,
    window_start: datetime,
    window_end: datetime,
    dataset_name: str | None = None,
) -> Incident:
    incident = Incident(
        title=title,
        source=source,
        dataset_name=dataset_name,
        window_start=window_start,
        window_end=window_end,
        status=IncidentStatus.PENDING,
        agent_statuses={},
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident


def get_incident(db: Session, incident_id: uuid.UUID) -> Incident | None:
    return db.get(Incident, incident_id)


def list_incidents(
    db: Session,
    *,
    status: IncidentStatus | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[Sequence[Incident], int]:
    """Une page d'incidents, du plus récent au plus ancien, et le total (pour la pagination)."""
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)
    query = select(Incident)
    count = select(func.count()).select_from(Incident)
    if status is not None:
        query = query.where(Incident.status == status)
        count = count.where(Incident.status == status)
    items = db.scalars(
        query.order_by(Incident.created_at.desc(), Incident.id).limit(limit).offset(offset)
    ).all()
    total = db.scalar(count) or 0
    return items, total


def update_status(
    db: Session,
    incident_id: uuid.UUID,
    status: IncidentStatus,
    *,
    agent_statuses: Mapping[AgentName, AgentStatus] | None = None,
    error: str | None = None,
) -> Incident | None:
    """Change le statut global ; fusionne agent_statuses (les clés absentes sont gardées)."""
    incident = db.get(Incident, incident_id)
    if incident is None:
        return None
    incident.status = status
    if agent_statuses:
        # Nouveau dict : une modification « sur place » d'un JSONB n'est pas détectée.
        incident.agent_statuses = {
            **incident.agent_statuses,
            **{str(name): str(value) for name, value in agent_statuses.items()},
        }
    if error is not None:
        incident.error = error
    db.commit()
    db.refresh(incident)
    return incident


def fail_interrupted(db: Session) -> int:
    """Passe en échec les analyses restées « running » (backend arrêté en pleine analyse)."""
    incidents = db.scalars(select(Incident).where(Incident.status == IncidentStatus.RUNNING)).all()
    for incident in incidents:
        incident.status = IncidentStatus.FAILED
        incident.error = "Analyse interrompue par un redémarrage du serveur : relancez-la."
    db.commit()
    return len(incidents)


def save_report(db: Session, incident_id: uuid.UUID, report: DiagnosisReport) -> Incident | None:
    """Enregistre le rapport final et passe l'incident à completed."""
    incident = db.get(Incident, incident_id)
    if incident is None:
        return None
    incident.report = report.model_dump(mode="json")
    incident.status = IncidentStatus.COMPLETED
    incident.error = None
    db.commit()
    db.refresh(incident)
    return incident
