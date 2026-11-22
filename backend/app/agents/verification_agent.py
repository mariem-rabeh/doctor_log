"""Verification Agent : contrôle les hypothèses du RCA et calcule confiance et verdict.

Contrôles en Python (sur l'hypothèse principale) :
1. chaque preuve citée existe dans les findings ;
2. sa période est dans la fenêtre analysée ;
3. pour une métrique, sa valeur existe bien dans la plage des données de la source ;
4. pas de contradiction entre sources (ex. « pool saturé » alors que des connexions restent libres).
Le LLM juge seulement : causalité étayée ou simple corrélation ?
"""

import logging
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.diagnosis import Evidence, Finding, Hypothesis, VerificationResult
from app.schemas.enums import SignalSource, Verdict
from app.schemas.telemetry import TimeWindow
from app.services.llm.base import LLMClient
from app.tools.datasources.base import DataSource

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.5
MISSING_METRIC_CAP = 0.45  # une métrique clé absente : on ne peut pas conclure

SYSTEM_PROMPT = """\
Tu es le Verification Agent d'un système de diagnostic d'incidents.
On te donne une hypothèse de cause racine et les preuves qu'elle cite.
Dis si les preuves établissent un lien de cause (causal = true) ou seulement une
corrélation dans le temps (causal = false). Explique en une ou deux phrases (comment),
et liste ce qu'un humain devrait encore vérifier (to_verify). N'invente aucun chiffre.
Rédige toujours en français.
"""


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    causal: bool
    comment: str
    to_verify: list[str] = Field(default_factory=list)


# (mots de la cause, métrique, test sur les valeurs, message si le test échoue)
Rule = tuple[tuple[str, ...], str, Callable[[list[float]], bool], str]
CONTRADICTIONS: list[Rule] = [
    (
        ("pool", "connexion"),
        "db_connections_idle",
        lambda v: min(v) > 0,
        "le pool n'est jamais plein : db_connections_idle reste au-dessus de 0",
    ),
    (
        ("cpu",),
        "cpu_usage",
        lambda v: max(v) < 0.7,
        "le CPU n'est pas saturé : cpu_usage reste sous 0.7",
    ),
]


def run(
    hypotheses: Sequence[Hypothesis],
    findings: Sequence[Finding],
    source: DataSource,
    window: TimeWindow,
    llm: LLMClient,
) -> VerificationResult:
    if not hypotheses:
        return VerificationResult(
            verdict=Verdict.INSUFFICIENT_EVIDENCE, confidence=0.0, issues=["aucune hypothèse"]
        )

    top = hypotheses[0]
    evidences = {e.id: e for f in findings for e in f.evidences}
    issues = check_evidences(top, evidences, source, window) + check_contradictions(
        top, source, window
    )

    judgment = _judge(top, evidences, llm)
    cited = [evidences[i] for i in top.supporting_evidence_ids if i in evidences]
    confidence = compute_confidence(judgment, cited, issues, findings)

    if issues:
        verdict = Verdict.REVISION_REQUESTED
    elif confidence < MIN_CONFIDENCE:
        verdict = Verdict.INSUFFICIENT_EVIDENCE
    else:
        verdict = Verdict.ACCEPTED
    to_verify = judgment.to_verify if judgment else ["jugement du LLM indisponible"]
    return VerificationResult(
        verdict=verdict, confidence=confidence, issues=issues, to_verify=to_verify
    )


def check_evidences(
    hypothesis: Hypothesis, evidences: dict[str, Evidence], source: DataSource, window: TimeWindow
) -> list[str]:
    issues = []
    for eid in hypothesis.supporting_evidence_ids + hypothesis.contradicting_evidence_ids:
        evidence = evidences.get(eid)
        if evidence is None:
            issues.append(f"{eid} : preuve inconnue")
            continue
        if evidence.period_start < window.start or evidence.period_end > window.end:
            issues.append(f"{eid} : période hors de la fenêtre analysée")
        if evidence.source is SignalSource.METRICS and not _value_in_data(evidence, source, window):
            issues.append(f"{eid} : valeur {evidence.value} absente des données")
    return issues


def _value_in_data(evidence: Evidence, source: DataSource, window: TimeWindow) -> bool:
    """La valeur d'une preuve métrique doit être dans la plage [min, max] de sa métrique."""
    name = evidence.observation.split(" :")[0].split("{")[0]
    values = [p.value for s in source.get_metrics(window, [name]) for p in s.points]
    if not values or not isinstance(evidence.value, int | float):
        return False
    return min(values) - 1e-9 <= evidence.value <= max(values) + 1e-9


def check_contradictions(
    hypothesis: Hypothesis, source: DataSource, window: TimeWindow
) -> list[str]:
    cause = hypothesis.cause.lower()
    issues = []
    for words, metric, contradicts, message in CONTRADICTIONS:
        if not any(w in cause for w in words):
            continue
        values = [p.value for s in source.get_metrics(window, [metric]) for p in s.points]
        if values and contradicts(values):
            issues.append(f"contradiction : {message}")
    return issues


def _judge(
    hypothesis: Hypothesis, evidences: dict[str, Evidence], llm: LLMClient
) -> Judgment | None:
    cited = [
        evidences[i].model_dump_json() for i in hypothesis.supporting_evidence_ids if i in evidences
    ]
    prompt = f"Hypothèse : {hypothesis.model_dump_json()}\nPreuves citées :\n" + "\n".join(cited)
    try:
        return llm.complete_structured(SYSTEM_PROMPT, prompt, Judgment)
    except Exception as exc:  # le client LLM peut lever n'importe quelle erreur
        logger.warning("Verification Agent : LLM indisponible (%s)", exc)
        return None


def compute_confidence(
    judgment: Judgment | None,
    cited: Sequence[Evidence],
    issues: Sequence[str],
    findings: Sequence[Finding],
) -> float:
    """0,5 de base ; +0,3 si causal ; +0,1 par source en plus de la première ;
    −0,2 par problème ; −0,1 par donnée manquante (au plus −0,3).
    Une métrique attendue absente plafonne la confiance sous le seuil d'acceptation."""
    score = 0.5 + (0.3 if judgment and judgment.causal else 0.0)
    score += 0.1 * max(0, len({e.source for e in cited}) - 1)
    score -= 0.2 * len(issues)
    score -= min(0.3, 0.1 * sum(len(f.missing_data) for f in findings))
    if any("métrique absente" in m for f in findings for m in f.missing_data):
        score = min(score, MISSING_METRIC_CAP)
    return round(min(1.0, max(0.0, score)), 2)
