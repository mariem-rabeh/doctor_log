"""RCA Agent : croise les trois Finding et propose des hypothèses classées.

Le LLM propose ; Python contrôle : une hypothèse qui cite une preuve inconnue est rejetée.
Les documents du RAG (doc-001...) sont des connaissances : ils peuvent être cités dans la
justification ou les actions, jamais comme preuve d'une hypothèse.
"""

import json
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from app.agents.prompts.rca import SYSTEM_PROMPT
from app.schemas.diagnosis import Document, Finding, Hypothesis
from app.services.llm.base import LLMClient
from app.services.llm.safety import wrap_untrusted

logger = logging.getLogger(__name__)
_DOC_ID = re.compile(r"\bdoc-\d{3,}\b")


class RcaAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis]


def run(
    findings: Sequence[Finding],
    llm: LLMClient,
    feedback: Sequence[str] = (),
    documents: Sequence[Document] = (),
) -> list[Hypothesis]:
    """Hypothèses valides, classées 1, 2, 3... ; liste vide si rien d'exploitable."""
    known = {e.id for f in findings for e in f.evidences}
    if not known:
        return []

    prompt = build_prompt(findings, feedback, documents)
    try:
        answer = llm.complete_structured(SYSTEM_PROMPT, prompt, RcaAnswer)
    except Exception as exc:  # le client LLM peut lever n'importe quelle erreur
        logger.warning("RCA Agent : LLM indisponible (%s)", exc)
        return []

    known_docs = {d.id for d in documents}
    valid = [h for h in answer.hypotheses if _cites_only(h, known) and _docs_known(h, known_docs)]
    if len(valid) < len(answer.hypotheses):
        logger.warning("RCA : %d hypothèse(s) rejetée(s)", len(answer.hypotheses) - len(valid))
    valid.sort(key=lambda h: h.rank)
    return [h.model_copy(update={"rank": i}) for i, h in enumerate(valid, start=1)]


def _cites_only(hypothesis: Hypothesis, known: set[str]) -> bool:
    cited = set(hypothesis.supporting_evidence_ids) | set(hypothesis.contradicting_evidence_ids)
    return cited <= known  # un doc-... n'est jamais dans known : il ne peut pas servir de preuve


def _docs_known(hypothesis: Hypothesis, known_docs: set[str]) -> bool:
    """Les documents cités dans le texte doivent faire partie de ceux fournis au LLM."""
    text = " ".join([hypothesis.justification, *hypothesis.verification_actions])
    return set(_DOC_ID.findall(text)) <= known_docs


def build_prompt(
    findings: Sequence[Finding], feedback: Sequence[str], documents: Sequence[Document] = ()
) -> str:
    data = [f.model_dump(mode="json") for f in findings]
    prompt = "Constats des agents :\n" + wrap_untrusted(json.dumps(data, ensure_ascii=False))
    if documents:
        docs = json.dumps(
            [{"id": d.id, "title": d.title, "text": d.text} for d in documents], ensure_ascii=False
        )
        prompt += "\n\nDocuments de référence (connaissances, pas des preuves) :\n"
        prompt += wrap_untrusted(docs)
    if feedback:
        prompt += "\n\nRemarques de la vérification à corriger :\n- " + "\n- ".join(feedback)
    return prompt
