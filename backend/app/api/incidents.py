"""Routes des incidents et des datasets.

POST /api/incidents             crée un incident (source dataset ou live)
GET  /api/incidents             liste paginée, filtrable par statut
GET  /api/incidents/{id}        détail d'un incident
GET  /api/datasets              datasets disponibles, avec leur plage de temps

Les routes d'analyse (/analyze, /status, /report) sont dans analysis.py.
"""

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.config import get_settings
from app.db import repository as repo
from app.db.models import Incident, IncidentSource
from app.db.session import get_db
from app.schemas.enums import IncidentStatus
from app.tools.datasources.file_source import DatasetError, FileDataSource, list_datasets

router = APIRouter(prefix="/api", tags=["incidents"])
DbSession = Annotated[Session, Depends(get_db)]


# --- Modèles d'entrée / sortie -----------------------------------------------------------------


class IncidentCreate(BaseModel):
    """Corps de POST /api/incidents.

    - dataset : dataset_name obligatoire ; fenêtre facultative (par défaut, tout le dataset) ;
    - live : fenêtre obligatoire ; les données viennent de la demo-app, Prometheus et Jaeger.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    source: Literal["dataset", "live"] = "dataset"
    dataset_name: str | None = Field(default=None, min_length=1, max_length=100)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_window(self) -> "IncidentCreate":
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start et window_end vont ensemble")
        if self.window_start and self.window_end and self.window_end <= self.window_start:
            raise ValueError("window_end doit être après window_start")
        if self.source == "dataset" and not self.dataset_name:
            raise ValueError("dataset_name est obligatoire pour un incident de type dataset")
        if self.source == "live" and self.window_start is None:
            raise ValueError("un incident live a besoin de window_start et window_end")
        return self


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    source: IncidentSource
    dataset_name: str | None
    window_start: datetime
    window_end: datetime
    status: IncidentStatus
    agent_statuses: dict[str, str]
    error: str | None
    has_report: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, incident: Incident) -> "IncidentOut":
        data = {name: getattr(incident, name) for name in cls.model_fields if name != "has_report"}
        return cls(**data, has_report=incident.report is not None)


class IncidentPage(BaseModel):
    items: list[IncidentOut]
    total: int
    limit: int
    offset: int


class DatasetOut(BaseModel):
    name: str
    window_start: datetime
    window_end: datetime


# --- Routes ------------------------------------------------------------------------------------


@router.get("/datasets", response_model=list[DatasetOut])
def get_datasets() -> list[DatasetOut]:
    """Datasets utilisables pour créer un incident (formulaire New Investigation)."""
    root = get_settings().datasets_dir
    result = []
    for name in list_datasets(root):
        try:
            window = FileDataSource(root / name).time_range()
        except DatasetError:
            continue  # dataset cassé : on ne le propose pas
        result.append(DatasetOut(name=name, window_start=window.start, window_end=window.end))
    return result


@router.post("/incidents", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
def create_incident(body: IncidentCreate, db: DbSession) -> IncidentOut:
    if body.source == "live":
        assert body.window_start is not None and body.window_end is not None
        incident = repo.create_incident(
            db,
            title=body.title,
            source=IncidentSource.LIVE,
            window_start=body.window_start,
            window_end=body.window_end,
        )
        return IncidentOut.of(incident)

    root = get_settings().datasets_dir
    if body.dataset_name not in list_datasets(root):
        raise ApiError(
            422,
            f"Dataset inconnu : {body.dataset_name}",
            details=[{"field": "dataset_name", "message": "dataset introuvable"}],
        )
    if body.window_start is not None and body.window_end is not None:
        start, end = body.window_start, body.window_end
    else:
        try:
            window = FileDataSource(root / str(body.dataset_name)).time_range()
        except DatasetError as exc:
            raise ApiError(422, str(exc)) from exc
        # Une minute de marge : les derniers spans se terminent après leur début.
        start, end = window.start, window.end + timedelta(minutes=1)

    incident = repo.create_incident(
        db,
        title=body.title,
        source=IncidentSource.DATASET,
        dataset_name=body.dataset_name,
        window_start=start,
        window_end=end,
    )
    return IncidentOut.of(incident)


@router.get("/incidents", response_model=IncidentPage)
def list_incidents(
    db: DbSession,
    status_filter: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=repo.MAX_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentPage:
    items, total = repo.list_incidents(db, status=status_filter, limit=limit, offset=offset)
    return IncidentPage(
        items=[IncidentOut.of(i) for i in items], total=total, limit=limit, offset=offset
    )


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: uuid.UUID, db: DbSession) -> IncidentOut:
    incident = repo.get_incident(db, incident_id)
    if incident is None:
        raise ApiError(404, "Incident introuvable")
    return IncidentOut.of(incident)
