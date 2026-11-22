"""Outils communs aux clients LLM appelés en HTTP (Gemini, Groq)."""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel

from app.services.llm.errors import LLMError

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def post_json(
    client: httpx.Client,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    name: str,
    max_retries: int,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], float]:
    """POST JSON ; retente sur 429, 5xx et erreurs réseau. Renvoie (réponse, durée en ms).

    Attente : celle demandée par le fournisseur (en-tête Retry-After, 60 s au plus) ; sinon
    15, 30, 45 s pour un 429 (les quotas se rechargent à la minute) et 2, 4, 8 s pour le reste.
    """
    error = ""
    for attempt in range(max_retries + 1):
        started = time.perf_counter()
        retry_after = None
        try:
            response = client.post(url, json=body, headers=headers)
        except httpx.TransportError as exc:
            error = f"réseau ({type(exc).__name__})"
        else:
            if response.status_code == 200:
                result: dict[str, Any] = response.json()
                return result, (time.perf_counter() - started) * 1000
            if response.status_code not in RETRY_STATUSES:
                raise LLMError(f"{name} a répondu {response.status_code} : {response.text[:200]}")
            error = f"HTTP {response.status_code}"
            retry_after = _retry_after(response)
            if response.status_code == 429:  # dit quel quota est atteint (minute, jour...)
                logger.warning("%s : quota atteint : %s", name, response.text[:300])
        if attempt < max_retries:
            if retry_after is not None:
                delay = min(retry_after, 60.0)
            else:
                delay = 15.0 * (attempt + 1) if error == "HTTP 429" else 2.0 * 2**attempt
            logger.warning("%s : %s, nouvelle tentative dans %.0f s", name, error, delay)
            sleep(delay)
    raise LLMError(f"appel {name} échoué après {max_retries + 1} tentative(s) : {error}")


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def with_schema(user: str, schema: type[BaseModel]) -> str:
    """Ajoute à la fin du message le schéma JSON que la réponse doit respecter."""
    json_schema = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        f"{user}\n\nRéponds uniquement avec un objet JSON valide selon ce schéma :\n{json_schema}"
    )


def log_call(
    model: str, schema: type[BaseModel], ms: float, tokens_in: Any, tokens_out: Any, finish: Any
) -> None:
    logger.info(
        "LLM %s %s : %.0f ms, %s tokens en entrée, %s en sortie, arrêt=%s",
        model,
        schema.__name__,
        ms,
        tokens_in,
        tokens_out,
        finish,
    )
