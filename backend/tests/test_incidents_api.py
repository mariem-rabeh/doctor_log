"""Tests de l'API incidents (branche 14).

- La plupart utilisent un faux repository en mémoire : ils testent les routes, la validation
  et le format des erreurs sans base de données, donc partout et en quelques millisecondes.
- Les derniers tournent contre un vrai PostgreSQL (ignorés s'il n'est pas joignable).
"""

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import incidents as api
from app.config import get_settings
from app.db import repository as real_repo
from app.db.models import Incident, IncidentSource
from app.db.session import get_db
from app.main import app
from app.schemas.enums import IncidentStatus

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


class FakeRepository:
    """Mêmes fonctions que app.db.repository, en mémoire."""

    MAX_PAGE_SIZE = real_repo.MAX_PAGE_SIZE

    def __init__(self) -> None:
        self.incidents: dict[uuid.UUID, Incident] = {}
        self.list_calls: list[dict[str, Any]] = []

    def create_incident(self, db: Any, **fields: Any) -> Incident:
        now = datetime.now(UTC) + timedelta(microseconds=len(self.incidents))
        incident = Incident(
            id=uuid.uuid4(),
            status=IncidentStatus.PENDING,
            agent_statuses={},
            report=None,
            error=None,
            created_at=now,
            updated_at=now,
            **fields,
        )
        self.incidents[incident.id] = incident
        return incident

    def get_incident(self, db: Any, incident_id: uuid.UUID) -> Incident | None:
        return self.incidents.get(incident_id)

    def list_incidents(
        self, db: Any, *, status: IncidentStatus | None, limit: int, offset: int
    ) -> tuple[Sequence[Incident], int]:
        self.list_calls.append({"status": status, "limit": limit, "offset": offset})
        items = [i for i in self.incidents.values() if status is None or i.status == status]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[offset : offset + limit], len(items)


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRepository]:
    repo = FakeRepository()
    monkeypatch.setattr(api, "repo", repo)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    yield repo
    app.dependency_overrides.clear()


@pytest.fixture
def client(fake_repo: FakeRepository) -> TestClient:
    return TestClient(app)


def create(client: TestClient, **overrides: Any) -> Any:
    body = {"title": "Latence élevée", "dataset_name": "db-pool-exhaustion", **overrides}
    return client.post("/api/incidents", json=body)


# --- GET /api/datasets -------------------------------------------------------------------------


def test_datasets_lists_the_six_datasets_with_their_window(client: TestClient) -> None:
    response = client.get("/api/datasets")
    assert response.status_code == 200
    datasets = {d["name"]: d for d in response.json()}
    assert len(datasets) == 6 and "slow-query" in datasets
    assert datasets["slow-query"]["window_start"].startswith("2026-11-18T10:00:00")


# --- POST /api/incidents -----------------------------------------------------------------------


def test_create_uses_the_dataset_window_by_default(client: TestClient) -> None:
    response = create(client)
    assert response.status_code == 201
    incident = response.json()
    assert incident["status"] == "pending"
    assert incident["source"] == "dataset"
    assert incident["has_report"] is False and incident["agent_statuses"] == {}
    assert incident["window_start"].startswith("2026-11-18T10:00:00")
    assert incident["window_end"] > incident["window_start"]


def test_create_with_an_explicit_window(client: TestClient) -> None:
    start, end = T0 + timedelta(minutes=10), T0 + timedelta(minutes=30)
    response = create(client, window_start=start.isoformat(), window_end=end.isoformat())
    assert response.status_code == 201
    assert response.json()["window_start"].startswith("2026-11-18T10:10:00")


def test_create_rejects_an_unknown_dataset(client: TestClient) -> None:
    response = create(client, dataset_name="nope")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [{"field": "dataset_name", "message": "dataset introuvable"}]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"title": ""}, "title"),
        ({"source": "live"}, "source"),
        ({"unexpected": 1}, "unexpected"),
        ({"window_start": "2026-11-18T10:00:00Z"}, ""),  # fin manquante
        ({"window_start": "2026-11-18T10:30:00Z", "window_end": "2026-11-18T10:00:00Z"}, ""),
        (
            {"window_start": "2026-11-18T10:00:00", "window_end": "2026-11-18T10:30:00"},
            "window_start",
        ),
    ],
)
def test_create_validation_errors_use_the_common_format(
    client: TestClient, overrides: dict[str, Any], field: str
) -> None:
    response = create(client, **overrides)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error" and error["message"] == "Requête invalide"
    assert error["details"][0]["field"] == field


def test_create_requires_a_title(client: TestClient) -> None:
    response = client.post("/api/incidents", json={"dataset_name": "slow-query"})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "title"


# --- GET /api/incidents/{id} -------------------------------------------------------------------


def test_get_returns_the_created_incident(client: TestClient) -> None:
    created = create(client).json()
    response = client.get(f"/api/incidents/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_incident_is_a_404_in_the_common_format(client: TestClient) -> None:
    response = client.get(f"/api/incidents/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Incident introuvable", "details": None}
    }


def test_get_with_a_malformed_id_is_a_422(client: TestClient) -> None:
    response = client.get("/api/incidents/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "path.incident_id"


# --- GET /api/incidents ------------------------------------------------------------------------


def test_list_paginates_and_filters(client: TestClient, fake_repo: FakeRepository) -> None:
    for i in range(3):
        create(client, title=f"Incident {i}")
    response = client.get("/api/incidents", params={"limit": 2, "offset": 0})
    page = response.json()
    assert response.status_code == 200
    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 0
    assert [i["title"] for i in page["items"]] == ["Incident 2", "Incident 1"]

    client.get("/api/incidents", params={"status": "running"})
    assert fake_repo.list_calls[-1]["status"] is IncidentStatus.RUNNING


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "unknown"}]
)
def test_list_rejects_bad_query_params(client: TestClient, params: dict[str, Any]) -> None:
    response = client.get("/api/incidents", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- Erreurs et CORS ---------------------------------------------------------------------------


def test_unknown_route_uses_the_common_format(client: TestClient) -> None:
    response = client.get("/api/nothing-here")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_cors_allows_the_frontend_origin_only(client: TestClient) -> None:
    def preflight(origin: str) -> Any:
        return client.options(
            "/api/incidents",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )

    allowed = preflight("http://localhost:5173")
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in preflight("http://evil.example").headers


# --- Contre un vrai PostgreSQL -----------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_session_factory() -> Iterator[Any]:
    # connect_timeout : sans lui, une base absente bloque chaque test plusieurs minutes.
    engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 2})
    try:
        engine.connect().close()
    except OperationalError:
        pytest.skip("PostgreSQL non disponible (docker compose -f docker-compose.dev.yml up -d)")
    yield engine
    engine.dispose()


@pytest.fixture
def pg_client(pg_session_factory: Any) -> Iterator[TestClient]:
    connection = pg_session_factory.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        session.close()
        transaction.rollback()
        connection.close()


def test_postgres_create_get_and_list(pg_client: TestClient) -> None:
    created = pg_client.post(
        "/api/incidents", json={"title": "PG", "dataset_name": "slow-query"}
    ).json()
    assert pg_client.get(f"/api/incidents/{created['id']}").json()["title"] == "PG"
    page = pg_client.get("/api/incidents", params={"status": "pending"}).json()
    assert created["id"] in [i["id"] for i in page["items"]]
    assert IncidentSource(created["source"]) is IncidentSource.DATASET
