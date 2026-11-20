"""Tests de validation des contrats de diagnostic (branche 1)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.diagnosis import (
    DiagnosisReport,
    Evidence,
    Finding,
    Hypothesis,
    VerificationResult,
)
from app.schemas.enums import Conclusion, SignalSource, Verdict

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


# --- Fabriques : un objet valide par défaut, qu'on modifie champ par champ -------------


def evidence_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "log-001",
        "source": "logs",
        "service": "orders-api",
        "period_start": T0,
        "period_end": T0 + timedelta(minutes=10),
        "observation": "Database connection timeout",
        "value": 312,
        "unit": "errors",
    }
    return {**data, **overrides}


def hypothesis_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "cause": "Pool de connexions saturé",
        "rank": 1,
        "justification": "Timeouts DB et attente d'acquisition longue dans les traces",
        "supporting_evidence_ids": ["log-001"],
    }
    return {**data, **overrides}


def report_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "incident_id": "inc-1",
        "conclusion": "confirmed",
        "probable_cause": "Pool de connexions saturé",
        "confidence": 0.86,
        "hypotheses": [hypothesis_data()],
        "evidences": [evidence_data()],
        "verification": {"verdict": "accepted", "confidence": 0.86},
        "iteration_count": 1,
        "generated_at": T0,
    }
    return {**data, **overrides}


# --- 1. Un objet valide passe ------------------------------------------------------------


def test_valid_evidence_is_accepted() -> None:
    evidence = Evidence(**evidence_data())
    assert evidence.source is SignalSource.LOGS
    assert evidence.value == 312


def test_valid_report_is_accepted() -> None:
    report = DiagnosisReport(**report_data())
    assert report.conclusion is Conclusion.CONFIRMED
    assert report.verification is not None
    assert report.verification.verdict is Verdict.ACCEPTED


def test_finding_lists_default_to_empty() -> None:
    finding = Finding(agent="traces", summary="RAS")
    assert finding.evidences == [] and finding.missing_data == []


# --- 2. Un champ manquant échoue ---------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "source", "service", "period_start", "value"])
def test_evidence_missing_field_is_rejected(missing: str) -> None:
    data = evidence_data()
    del data[missing]
    with pytest.raises(ValidationError):
        Evidence(**data)


@pytest.mark.parametrize("missing", ["conclusion", "hypotheses", "generated_at"])
def test_report_missing_field_is_rejected(missing: str) -> None:
    data = report_data()
    del data[missing]
    with pytest.raises(ValidationError):
        DiagnosisReport(**data)


# --- 3. Une confiance hors bornes échoue -------------------------------------------------


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_verification_confidence_out_of_bounds_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        VerificationResult(verdict="accepted", confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_verification_confidence_bounds_are_inclusive(confidence: float) -> None:
    assert VerificationResult(verdict="accepted", confidence=confidence).confidence == confidence


def test_report_confidence_out_of_bounds_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DiagnosisReport(**report_data(confidence=1.2))


# --- 4. Règles métier propres à nos contrats ---------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "met-001"},  # préfixe incohérent avec source=logs
        {"id": "log-1"},  # format log-xxx non respecté
        {"period_start": datetime(2026, 11, 18, 10, 0)},  # date sans fuseau
        {"period_end": T0 - timedelta(minutes=1)},  # fin avant début
        {"source": "database"},  # source inconnue
    ],
)
def test_invalid_evidence_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Evidence(**evidence_data(**overrides))


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(**hypothesis_data(certainty="high"))


def test_hypothesis_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(**hypothesis_data(supporting_evidence_ids=[]))


def test_hypothesis_evidence_both_for_and_against_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(**hypothesis_data(contradicting_evidence_ids=["log-001"]))


def test_confirmed_report_requires_probable_cause() -> None:
    with pytest.raises(ValidationError):
        DiagnosisReport(**report_data(probable_cause=None))


def test_insufficient_evidence_report_allows_no_cause() -> None:
    report = DiagnosisReport(
        **report_data(conclusion="insufficient_evidence", probable_cause=None, verification=None)
    )
    assert report.probable_cause is None


# --- 5. Aller-retour JSON (le rapport sera stocké en JSONB) --------------------------------


def test_report_json_round_trip() -> None:
    report = DiagnosisReport(**report_data())
    assert DiagnosisReport.model_validate_json(report.model_dump_json()) == report
