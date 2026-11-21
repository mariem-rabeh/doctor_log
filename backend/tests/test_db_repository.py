"""Tests du modèle Incident, de la migration et du repository, contre un vrai PostgreSQL.

La base est celle de DATABASE_URL (en local : docker compose -f docker-compose.dev.yml up -d ;
en CI : service postgres). Les tests sont ignorés si elle n'est pas joignable.
Chaque test tourne dans une transaction annulée à la fin : la base n'est pas modifiée.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Base, Incident, IncidentSource
from app.db.repository import (
    MAX_PAGE_SIZE,
    create_incident,
    get_incident,
    list_incidents,
    save_report,
    update_status,
)
from app.schemas.diagnosis import DiagnosisReport
from app.schemas.enums import AgentName, AgentStatus, Conclusion, IncidentStatus

BACKEND = Path(__file__).resolve().parents[1]
START = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    url = get_settings().database_url
    engine = create_engine(url)
    try:
        engine.connect().close()
    except OperationalError:
        pytest.skip("PostgreSQL non disponible (docker compose -f docker-compose.dev.yml up -d)")
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")  # teste aussi la migration
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    # Les commit() du repository deviennent des savepoints : tout est annulé à la fin.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _new(db: Session, title: str = "Latence élevée", **overrides: object) -> Incident:
    fields: dict[str, object] = {
        "title": title,
        "source": IncidentSource.DATASET,
        "dataset_name": "cpu-overload",
        "window_start": START,
        "window_end": END,
    }
    fields.update(overrides)
    return create_incident(db, **fields)  # type: ignore[arg-type]


def _report(incident_id: uuid.UUID) -> DiagnosisReport:
    return DiagnosisReport(
        incident_id=str(incident_id),
        conclusion=Conclusion.PROBABLE,
        probable_cause="Saturation CPU d'orders-service",
        confidence=0.72,
        hypotheses=[],
        evidences=[],
        verification=None,
        iteration_count=1,
        generated_at=END,
    )


# --- Migration ------------------------------------------------------------------------------


def test_migration_matches_models(engine: Engine) -> None:
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == [], f"modèles et migration divergent : {diff}"


# --- create / get ---------------------------------------------------------------------------


def test_create_sets_defaults(db: Session) -> None:
    incident = _new(db)
    assert isinstance(incident.id, uuid.UUID)
    assert incident.status is IncidentStatus.PENDING
    assert incident.agent_statuses == {}
    assert incident.report is None and incident.error is None
    assert incident.created_at is not None and incident.updated_at is not None


def test_get_returns_incident_or_none(db: Session) -> None:
    incident = _new(db)
    db.expire_all()  # force une vraie relecture en base
    loaded = get_incident(db, incident.id)
    assert loaded is not None
    assert loaded.title == "Latence élevée"
    assert loaded.source is IncidentSource.DATASET
    assert loaded.window_start == START  # fuseau conservé (timestamptz)
    assert get_incident(db, uuid.uuid4()) is None


def test_enum_values_are_stored_in_lowercase(db: Session) -> None:
    incident = _new(db)
    row = db.execute(
        text("SELECT source, status FROM incidents WHERE id = :id"), {"id": incident.id}
    ).one()
    assert tuple(row) == ("dataset", "pending")


def test_live_incident_needs_no_dataset_name(db: Session) -> None:
    incident = _new(db, source=IncidentSource.LIVE, dataset_name=None)
    assert incident.dataset_name is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_end": START},  # fenêtre vide
        {"window_end": START - timedelta(minutes=1)},  # fenêtre inversée
        {"source": IncidentSource.DATASET, "dataset_name": None},  # dataset sans nom
    ],
)
def test_database_rejects_invalid_incidents(db: Session, overrides: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        _new(db, **overrides)


# --- list -----------------------------------------------------------------------------------


def test_list_newest_first_with_pagination_and_total(db: Session) -> None:
    ids = []
    for i in range(5):
        incident = _new(db, title=f"incident {i}")
        # created_at = now() est identique dans une même transaction : on l'étale à la main.
        incident.created_at = START + timedelta(minutes=i)
        db.commit()
        ids.append(incident.id)

    page, total = list_incidents(db, limit=2, offset=0)
    assert total == 5
    assert [i.id for i in page] == [ids[4], ids[3]]
    page, _ = list_incidents(db, limit=2, offset=4)
    assert [i.id for i in page] == [ids[0]]


def test_list_filters_by_status(db: Session) -> None:
    done = _new(db, title="fini")
    _new(db, title="en attente")
    update_status(db, done.id, IncidentStatus.COMPLETED)

    page, total = list_incidents(db, status=IncidentStatus.COMPLETED)
    assert total == 1 and [i.title for i in page] == ["fini"]
    _, pending = list_incidents(db, status=IncidentStatus.PENDING)
    assert pending == 1


def test_list_clamps_page_size(db: Session) -> None:
    _new(db)
    page, total = list_incidents(db, limit=10_000, offset=-5)
    assert total == 1 and len(page) == 1
    assert MAX_PAGE_SIZE == 100


# --- update_status / save_report ------------------------------------------------------------


def test_update_status_merges_agent_statuses(db: Session) -> None:
    incident = _new(db)
    update_status(
        db,
        incident.id,
        IncidentStatus.RUNNING,
        agent_statuses={AgentName.LOG: AgentStatus.RUNNING, AgentName.METRICS: AgentStatus.RUNNING},
    )
    update_status(
        db,
        incident.id,
        IncidentStatus.RUNNING,
        agent_statuses={AgentName.LOG: AgentStatus.COMPLETED},
    )
    db.expire_all()
    loaded = get_incident(db, incident.id)
    assert loaded is not None
    assert loaded.status is IncidentStatus.RUNNING
    assert loaded.agent_statuses == {"log": "completed", "metrics": "running"}


def test_update_status_records_error(db: Session) -> None:
    incident = _new(db)
    updated = update_status(db, incident.id, IncidentStatus.FAILED, error="LLM indisponible")
    assert updated is not None
    assert updated.status is IncidentStatus.FAILED and updated.error == "LLM indisponible"


def test_save_report_stores_json_and_completes(db: Session) -> None:
    incident = _new(db)
    update_status(db, incident.id, IncidentStatus.FAILED, error="ancienne erreur")
    save_report(db, incident.id, _report(incident.id))
    db.expire_all()
    loaded = get_incident(db, incident.id)
    assert loaded is not None and loaded.report is not None
    assert loaded.status is IncidentStatus.COMPLETED and loaded.error is None
    # Le JSON relu redonne exactement le même DiagnosisReport.
    assert DiagnosisReport.model_validate(loaded.report) == _report(incident.id)


def test_unknown_id_returns_none(db: Session) -> None:
    unknown = uuid.uuid4()
    assert update_status(db, unknown, IncidentStatus.RUNNING) is None
    assert save_report(db, unknown, _report(unknown)) is None
