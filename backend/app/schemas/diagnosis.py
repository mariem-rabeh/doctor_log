"""Schémas du diagnostic : preuves, constats des agents, hypothèses, vérification, rapport.

Règle du projet : les chiffres (Evidence.value) sont calculés par les outils Python.
Le LLM ne fait que les interpréter et les citer par leur identifiant.
"""

from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import Conclusion, SignalSource, Verdict

# Préfixe d'identifiant attendu pour chaque source : log-001, met-002, trc-003.
EVIDENCE_PREFIX: dict[SignalSource, str] = {
    SignalSource.LOGS: "log",
    SignalSource.METRICS: "met",
    SignalSource.TRACES: "trc",
}


class StrictModel(BaseModel):
    """Base commune : tout champ inconnu est refusé (utile contre les sorties LLM inventées)."""

    model_config = ConfigDict(extra="forbid")


class Document(StrictModel):
    """Un document de la base de connaissances (RAG) : une connaissance, jamais une preuve."""

    id: str = Field(pattern=r"^doc-\d{3,}$", examples=["doc-001"])
    title: str
    path: str  # nom du fichier dans knowledge/
    text: str


class Evidence(StrictModel):
    """Un fait mesuré dans la télémétrie, citable par son identifiant."""

    id: str = Field(pattern=r"^(log|met|trc)-\d{3,}$", examples=["log-001"])
    source: SignalSource
    service: str = Field(min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime
    observation: str = Field(min_length=1)
    value: float | str
    unit: str | None = None

    @model_validator(mode="after")
    def check_consistency(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end doit être postérieur ou égal à period_start")
        expected = EVIDENCE_PREFIX[self.source]
        if not self.id.startswith(f"{expected}-"):
            raise ValueError(f"une preuve de source {self.source} doit avoir un id {expected}-xxx")
        return self


class Finding(StrictModel):
    """Ce qu'un agent d'analyse (logs, métriques ou traces) a constaté."""

    agent: SignalSource
    summary: str
    evidences: list[Evidence] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class Hypothesis(StrictModel):
    """Une cause possible proposée par le RCA Agent, appuyée sur des preuves citées."""

    cause: str = Field(min_length=1)
    rank: int = Field(ge=1)
    justification: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(min_length=1)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    verification_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_no_overlap(self) -> Self:
        overlap = set(self.supporting_evidence_ids) & set(self.contradicting_evidence_ids)
        if overlap:
            raise ValueError(f"preuves à la fois pour et contre : {sorted(overlap)}")
        return self


class VerificationResult(StrictModel):
    """Avis du Verification Agent sur les hypothèses."""

    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)
    to_verify: list[str] = Field(default_factory=list)


class DiagnosisReport(StrictModel):
    """Rapport final renvoyé par l'API et affiché sur la page Diagnosis Report."""

    incident_id: str
    conclusion: Conclusion
    probable_cause: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    hypotheses: list[Hypothesis]
    evidences: list[Evidence]
    verification: VerificationResult | None
    recommendations: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)  # consultés par le RCA
    iteration_count: int = Field(ge=0)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def check_cause_matches_conclusion(self) -> Self:
        if self.conclusion != Conclusion.INSUFFICIENT_EVIDENCE and not self.probable_cause:
            raise ValueError("probable_cause obligatoire sauf si les preuves sont insuffisantes")
        return self
