"""Tests de l'API d'analyse (branche 19) contre PostgreSQL, avec FakeLLM.

Ignorés si PostgreSQL n'est pas joignable (docker compose -f docker-compose.dev.yml up -d).
"""

from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.agents.log_agent import LogInterpretation
from app.agents.metrics_agent import MetricsInterpretation
from app.agents.rca_agent import RcaAnswer
from app.agents.trace_agent import TraceInterpretation
from app.agents.verification_agent import Judgment
from app.api import analysis
from app.config import get_settings
from app.db.session import get_db
from app.main import app
from app.services.llm.fake import FakeLLM


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 2})
    try:
        engine.connect().close()
    except OperationalError:
        pytest.skip("PostgreSQL non disponible")
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Une transaction annulée à la fin ; la tâche de fond utilise la même session."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_db] = lambda: session
    monkeypatch.setattr(analysis, "open_session", lambda: nullcontext(session))
    monkeypatch.setattr(analysis, "llm_factory", fake_llm)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        session.close()
        transaction.rollback()
        connection.close()


def fake_llm() -> FakeLLM:
    text = {"summary": "résumé"}
    hypothesis = {
        "cause": "Pool de connexions saturé",
        "rank": 1,
        "justification": "j",
        "supporting_evidence_ids": ["log-001", "met-005", "trc-001"],
    }
    return FakeLLM(
        {
            LogInterpretation: text,
            TraceInterpretation: text,
            MetricsInterpretation: text,
            RcaAnswer: {"hypotheses": [hypothesis]},
            Judgment: {"causal": True, "comment": "ok"},
        }
    )


def new_incident(client: TestClient, dataset: str = "db-pool-exhaustion") -> str:
    response = client.post("/api/incidents", json={"title": "Latence", "dataset_name": dataset})
    assert response.status_code == 201
    incident_id: str = response.json()["id"]
    return incident_id


def test_full_analysis_of_s01(client: TestClient) -> None:
    incident_id = new_incident(client)

    started = client.post(f"/api/incidents/{incident_id}/analyze")
    assert started.status_code == 202
    assert started.json()["status"] == "running"

    # TestClient exécute la tâche de fond juste après la réponse.
    status = client.get(f"/api/incidents/{incident_id}/status").json()
    assert status["status"] == "completed" and status["has_report"] is True
    assert set(status["agent_statuses"]) == {"log", "metrics", "trace", "rca", "verification"}
    assert set(status["agent_statuses"].values()) == {"completed"}

    report: dict[str, Any] = client.get(f"/api/incidents/{incident_id}/report").json()
    assert report["conclusion"] == "confirmed"
    assert report["probable_cause"] == "Pool de connexions saturé"
    assert report["incident_id"] == incident_id


def test_report_is_404_before_analysis(client: TestClient) -> None:
    incident_id = new_incident(client)
    response = client.get(f"/api/incidents/{incident_id}/report")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Rapport pas encore disponible"


def test_analysis_already_running_is_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident_id = new_incident(client)
    monkeypatch.setattr(analysis, "run_incident_analysis", lambda _: None)  # reste « running »
    assert client.post(f"/api/incidents/{incident_id}/analyze").status_code == 202
    second = client.post(f"/api/incidents/{incident_id}/analyze")
    assert second.status_code == 409 and second.json()["error"]["code"] == "conflict"


def test_failure_sets_failed_with_the_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> FakeLLM:
        raise RuntimeError("clé LLM absente")

    monkeypatch.setattr(analysis, "llm_factory", broken)
    incident_id = new_incident(client)
    client.post(f"/api/incidents/{incident_id}/analyze")
    status = client.get(f"/api/incidents/{incident_id}/status").json()
    assert status["status"] == "failed"
    assert status["error"] == "RuntimeError : clé LLM absente"
    # Une analyse en échec peut être relancée.
    monkeypatch.setattr(analysis, "llm_factory", fake_llm)
    assert client.post(f"/api/incidents/{incident_id}/analyze").status_code == 202


@pytest.mark.parametrize("path", ["status", "report", "analyze"])
def test_unknown_incident_is_404(client: TestClient, path: str) -> None:
    url = f"/api/incidents/00000000-0000-0000-0000-000000000000/{path}"
    response = client.post(url) if path == "analyze" else client.get(url)
    assert response.status_code == 404


def test_live_incident_is_created_and_analyzed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_live_source import live

    monkeypatch.setattr(analysis.LiveDataSource, "from_settings", lambda _settings: live(tmp_path))
    body = {
        "title": "Incident live",
        "source": "live",
        "window_start": "2026-11-22T09:00:00Z",
        "window_end": "2026-11-22T10:00:00Z",
    }
    created = client.post("/api/incidents", json=body)
    assert created.status_code == 201
    incident = created.json()
    assert incident["source"] == "live" and incident["dataset_name"] is None

    client.post(f"/api/incidents/{incident['id']}/analyze")
    status = client.get(f"/api/incidents/{incident['id']}/status").json()
    assert status["status"] == "completed", status["error"]
