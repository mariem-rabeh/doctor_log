"""Routes de l'analyse d'un incident.

POST /api/incidents/{id}/analyze   lance le graphe en tâche de fond (202), 409 si déjà en cours
GET  /api/incidents/{id}/status    statut global et avancement par agent (page de suivi)
GET  /api/incidents/{id}/report    rapport final, 404 tant qu'il n'existe pas
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from fastapi import APIRouter, BackgroundTasks, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.incidents import DbSession, IncidentOut
from app.config import get_settings
from app.db import repository as repo
from app.db.models import IncidentSource
from app.db.session import get_sessionmaker
from app.graph.state import IncidentState
from app.graph.workflow import run_analysis
from app.schemas.diagnosis import DiagnosisReport
from app.schemas.enums import AgentName, AgentStatus, IncidentStatus
from app.schemas.telemetry import TimeWindow
from app.services.llm.base import LLMClient
from app.services.llm.provider import get_llm
from app.tools.datasources.base import DataSource
from app.tools.datasources.file_source import FileDataSource
from app.tools.datasources.live_source import LiveDataSource

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/incidents", tags=["analysis"])


@contextmanager
def _new_session() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


# Remplaçables dans les tests.
open_session: Callable[[], AbstractContextManager[Session]] = _new_session
llm_factory: Callable[[], LLMClient] = get_llm


class IncidentStatusOut(BaseModel):
    id: uuid.UUID
    status: IncidentStatus
    agent_statuses: dict[str, str]
    error: str | None
    has_report: bool


@router.post(
    "/{incident_id}/analyze", response_model=IncidentOut, status_code=status.HTTP_202_ACCEPTED
)
def analyze(incident_id: uuid.UUID, db: DbSession, tasks: BackgroundTasks) -> IncidentOut:
    incident = repo.get_incident(db, incident_id)
    if incident is None:
        raise ApiError(404, "Incident introuvable")
    if incident.status is IncidentStatus.RUNNING:
        raise ApiError(409, "Une analyse est déjà en cours pour cet incident")

    pending = {name: AgentStatus.PENDING for name in AgentName}
    incident.agent_statuses = {}  # repart de zéro si on relance une analyse
    updated = repo.update_status(db, incident_id, IncidentStatus.RUNNING, agent_statuses=pending)
    assert updated is not None
    tasks.add_task(run_incident_analysis, incident_id)
    return IncidentOut.of(updated)


@router.get("/{incident_id}/status", response_model=IncidentStatusOut)
def get_status(incident_id: uuid.UUID, db: DbSession) -> IncidentStatusOut:
    incident = repo.get_incident(db, incident_id)
    if incident is None:
        raise ApiError(404, "Incident introuvable")
    return IncidentStatusOut(
        id=incident.id,
        status=incident.status,
        agent_statuses=incident.agent_statuses,
        error=incident.error,
        has_report=incident.report is not None,
    )


@router.get("/{incident_id}/report", response_model=DiagnosisReport)
def get_report(incident_id: uuid.UUID, db: DbSession) -> DiagnosisReport:
    incident = repo.get_incident(db, incident_id)
    if incident is None:
        raise ApiError(404, "Incident introuvable")
    if incident.report is None:
        raise ApiError(404, "Rapport pas encore disponible")
    return DiagnosisReport.model_validate(incident.report)


def run_incident_analysis(incident_id: uuid.UUID) -> None:
    """Tâche de fond, avec sa propre session : graphe, suivi des agents, rapport ou erreur."""
    with open_session() as db:
        incident = repo.get_incident(db, incident_id)
        if incident is None:
            return
        try:
            source: DataSource
            if incident.source is IncidentSource.LIVE:
                source = LiveDataSource.from_settings(get_settings())
            elif incident.dataset_name:
                source = FileDataSource.from_name(incident.dataset_name)
            else:
                raise ValueError("incident de type dataset sans dataset_name")
            window = TimeWindow(start=incident.window_start, end=incident.window_end)

            def on_update(state: IncidentState) -> None:
                repo.update_status(
                    db,
                    incident_id,
                    IncidentStatus.RUNNING,
                    agent_statuses=state.get("agent_statuses", {}),
                )

            report = run_analysis(source, window, llm_factory(), str(incident_id), on_update)
            repo.save_report(db, incident_id, report)
        except Exception as exc:  # l'incident doit toujours sortir de l'état « running »
            logger.exception("analyse de l'incident %s en échec", incident_id)
            db.rollback()
            repo.update_status(
                db, incident_id, IncidentStatus.FAILED, error=f"{type(exc).__name__} : {exc}"
            )
