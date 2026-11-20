"""Mécanismes partagés par les agents d'analyse (logs, traces, et métriques côté Ichrak).

- detect_change_point : trouve où une série de nombres « décolle » (début d'incident) ;
- AgentInterpretation : ce que le LLM a le droit de renvoyer ;
- ground : ne garde que ce qui cite des preuves existantes ;
- telemetry_prompt : encadre les données pour qu'elles ne soient jamais prises pour des ordres.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- Détection du début ------------------------------------------------------------------------


def detect_change_point(
    values: Sequence[float],
    *,
    min_segment: int = 2,
    min_ratio: float = 1.5,
    min_delta: float = 1.0,
) -> int | None:
    """Indice k où la série passe d'un niveau bas (values[:k]) à un niveau haut (values[k:]).

    On essaie chaque coupure et on garde celle qui maximise k·(n−k)/n · (après − avant)² :
    un écart net entre deux parties de taille raisonnable. La coupure n'est retenue que si
    après ≥ min_ratio × avant ET après ≥ avant + min_delta ; sinon None.
    """
    n = len(values)
    best_k, best_score = None, 0.0
    for k in range(min_segment, n - min_segment + 1):
        before = sum(values[:k]) / k
        after = sum(values[k:]) / (n - k)
        if after <= before:
            continue
        score = k * (n - k) / n * (after - before) ** 2
        if score > best_score:
            best_k, best_score = k, score

    if best_k is None:
        return None
    before = sum(values[:best_k]) / best_k
    after = sum(values[best_k:]) / (n - best_k)
    if after < max(min_ratio * before, before + min_delta):
        return None
    return best_k


# --- Réponse du LLM ----------------------------------------------------------------------------


class AgentInterpretation(BaseModel):
    """Ce que le LLM a le droit de produire : du texte et des identifiants existants.

    Chaque agent en dérive sa propre classe (LogInterpretation, TraceInterpretation...) :
    FakeLLM choisit sa réponse selon la classe, ce qui permet de tester le graphe complet
    avec une réponse différente par agent.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    anomalies: list[str] = Field(default_factory=list)
    noise_evidence_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Grounded:
    summary: str
    anomalies: list[str]
    dropped: int  # éléments du LLM écartés car ils citent une preuve inconnue


def ground(
    interpretation: AgentInterpretation | None,
    known_ids: set[str],
    id_pattern: re.Pattern[str],
    fallback_summary: str,
) -> Grounded:
    """Garde seulement ce qui cite des preuves existantes.

    - summary citant un id inconnu -> remplacé par fallback_summary ;
    - anomalie citant un id inconnu -> écartée ;
    - noise_evidence_ids inconnus -> écartés, les autres deviennent une anomalie lisible.
    """
    if interpretation is None:
        return Grounded(fallback_summary, [], 0)

    def is_grounded(text: str) -> bool:
        return set(id_pattern.findall(text)) <= known_ids

    dropped = 0
    summary = interpretation.summary
    if not is_grounded(summary):
        summary, dropped = fallback_summary, 1

    anomalies = []
    for sentence in interpretation.anomalies:
        if is_grounded(sentence):
            anomalies.append(sentence)
        else:
            dropped += 1

    noise = [i for i in dict.fromkeys(interpretation.noise_evidence_ids) if i in known_ids]
    dropped += len(set(interpretation.noise_evidence_ids) - known_ids)
    if noise:
        anomalies.append(f"Probablement sans rapport avec l'incident : {', '.join(noise)}.")
    return Grounded(summary, anomalies, dropped)


# --- Prompt ------------------------------------------------------------------------------------


def telemetry_prompt(intro: str, digest: dict[str, Any]) -> str:
    """Résumé JSON encadré de balises <telemetry>, que son contenu ne peut pas refermer."""
    payload = json.dumps(digest, ensure_ascii=False, indent=1, default=str)
    payload = payload.replace("</telemetry", "<\\/telemetry")
    return f"{intro}\n<telemetry>\n{payload}\n</telemetry>"
