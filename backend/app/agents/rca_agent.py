"""RCA Agent : croise les trois Finding et propose des hypothèses classées.

Le LLM propose ; Python contrôle : une hypothèse qui cite une preuve inconnue est rejetée.
"""

import json
import logging
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from app.agents.prompts.rca import SYSTEM_PROMPT
from app.schemas.diagnosis import Finding, Hypothesis
from app.services.llm.base import LLMClient
from app.services.llm.safety import wrap_untrusted

logger = logging.getLogger(__name__)


class RcaAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis]


def run(
    findings: Sequence[Finding], llm: LLMClient, feedback: Sequence[str] = ()
) -> list[Hypothesis]:
    """Hypothèses valides, classées 1, 2, 3... ; liste vide si rien d'exploitable."""
    known = {e.id for f in findings for e in f.evidences}
    if not known:
        return []

    try:
        answer = llm.complete_structured(SYSTEM_PROMPT, build_prompt(findings, feedback), RcaAnswer)
    except Exception as exc:  # le client LLM peut lever n'importe quelle erreur
        logger.warning("RCA Agent : LLM indisponible (%s)", exc)
        return []

    valid = [h for h in answer.hypotheses if _cites_only(h, known)]
    if len(valid) < len(answer.hypotheses):
        logger.warning("RCA : %d hypothèse(s) rejetée(s)", len(answer.hypotheses) - len(valid))
    valid.sort(key=lambda h: h.rank)
    return [h.model_copy(update={"rank": i}) for i, h in enumerate(valid, start=1)]


def _cites_only(hypothesis: Hypothesis, known: set[str]) -> bool:
    cited = set(hypothesis.supporting_evidence_ids) | set(hypothesis.contradicting_evidence_ids)
    return cited <= known


def build_prompt(findings: Sequence[Finding], feedback: Sequence[str]) -> str:
    data = [f.model_dump(mode="json") for f in findings]
    prompt = "Constats des agents :\n" + wrap_untrusted(json.dumps(data, ensure_ascii=False))
    if feedback:
        prompt += "\n\nRemarques de la vérification à corriger :\n- " + "\n- ".join(feedback)
    return prompt
