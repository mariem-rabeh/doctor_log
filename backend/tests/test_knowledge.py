"""Tests de la base de connaissances (RAG) : chargement, requête et recherche BM25."""

from datetime import UTC, datetime
from pathlib import Path

from app.config import REPO_ROOT
from app.schemas.diagnosis import Evidence, Finding
from app.tools import knowledge

T = datetime(2026, 11, 18, 10, 25, tzinfo=UTC)


def evidence(eid: str, source: str, observation: str) -> Evidence:
    return Evidence(
        id=eid,
        source=source,
        service="orders-service",
        period_start=T,
        period_end=T,
        observation=observation,
        value=1,
    )


POOL_FINDINGS = [
    Finding(
        agent="logs",
        summary="s",
        evidences=[evidence("log-001", "logs", "WARNING « QueuePool limit reached » : nouveau")],
    ),
    Finding(
        agent="metrics",
        summary="s",
        evidences=[
            evidence("met-001", "metrics", "db_connections_idle : baisse anormale"),
            evidence("met-002", "metrics", "cpu_usage : stable (moyenne 0.35)"),
        ],
    ),
]


def test_repository_runbooks_are_loaded_without_readme() -> None:
    docs = knowledge.load_documents(REPO_ROOT / "knowledge")
    assert len(docs) >= 4
    assert all(d.path != "README.md" and d.title for d in docs)


def test_missing_folder_gives_no_document(tmp_path: Path) -> None:
    assert knowledge.load_documents(tmp_path / "absent") == []


def test_query_skips_evidences_that_did_not_change() -> None:
    query = knowledge.build_query(POOL_FINDINGS)
    assert "QueuePool" in query and "db_connections_idle" in query
    assert "cpu_usage" not in query


def test_pool_incident_finds_the_pool_runbook_first() -> None:
    docs = knowledge.load_documents(REPO_ROOT / "knowledge")
    found = knowledge.search(docs, knowledge.build_query(POOL_FINDINGS))
    assert found[0].path == "pool-connexions.md"
    assert [d.id for d in found] == [f"doc-{i:03d}" for i in range(1, len(found) + 1)]
    assert len(found) <= knowledge.MAX_DOCUMENTS


def test_unrelated_query_finds_nothing() -> None:
    docs = knowledge.load_documents(REPO_ROOT / "knowledge")
    assert knowledge.search(docs, "zzz inconnu") == []
    assert knowledge.search([], "QueuePool") == []
