"""Tables de la base de données."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.schemas.enums import IncidentStatus


class IncidentSource(StrEnum):
    """D'où viennent les données analysées."""

    DATASET = "dataset"  # un dossier de datasets/
    LIVE = "live"  # Prometheus, Jaeger et les logs de la demo-app


def _enum(enum_cls: type[StrEnum]) -> Enum:
    # Stocke la VALEUR ("pending") en VARCHAR + contrainte CHECK, plutôt qu'un type ENUM
    # PostgreSQL : ajouter une valeur plus tard ne demande alors qu'une migration simple.
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda e: [member.value for member in e],
    )


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("window_end > window_start", name="ck_incidents_window_order"),
        CheckConstraint(
            "source <> 'dataset' OR dataset_name IS NOT NULL",
            name="ck_incidents_dataset_name_required",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[IncidentSource] = mapped_column(_enum(IncidentSource))
    dataset_name: Mapped[str | None] = mapped_column(String(100), default=None)

    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus), default=IncidentStatus.PENDING, index=True
    )
    # {"log": "completed", "metrics": "running", ...} : clés AgentName, valeurs AgentStatus.
    agent_statuses: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    # DiagnosisReport sérialisé (model_dump(mode="json")), présent quand status = completed.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # Message d'erreur quand status = failed.
    error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
