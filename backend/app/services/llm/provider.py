"""Client LLM réel (Anthropic / Claude) et choix de l'implémentation selon Settings.

AnthropicLLM implémente LLMClient avec le SDK officiel `anthropic` :
- sortie structurée : `messages.parse(output_format=schema)` renvoie un objet Pydantic validé ;
  si la réponse ne valide pas le schéma, une nouvelle tentative est faite (MAX_INVALID_RETRIES) ;
- timeout et retries avec backoff exponentiel (réseau, 408, 409, 429, 5xx) : gérés par le SDK,
  réglés par Settings.llm_timeout_seconds et Settings.llm_max_retries ;
- refus de sécurité : fallback côté serveur (`fallbacks: "default"`), puis LLMRefusalError ;
- chaque appel est journalisé : modèle, durée, tokens d'entrée et de sortie.
"""

import logging
import time
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.services.llm.base import LLMClient
from app.services.llm.fake import FakeLLM
from app.services.llm.safety import UNTRUSTED_NOTICE

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000  # requête non streamée : assez de place sans risquer le timeout HTTP
MAX_INVALID_RETRIES = 1  # nouvelles tentatives si la réponse ne valide pas le schéma
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """Aucune réponse valide n'a pu être obtenue du LLM."""


class LLMRefusalError(LLMError):
    """Le modèle (et son fallback) a refusé de répondre pour une raison de sécurité."""


class AnthropicLLM:
    """LLMClient branché sur l'API Messages d'Anthropic."""

    def __init__(self, settings: Settings, *, client: anthropic.Anthropic | None = None) -> None:
        self.model = settings.llm_model or DEFAULT_MODEL
        self.effort = settings.llm_effort
        api_key = settings.llm_api_key.get_secret_value() or None  # None : variables d'env du SDK
        self._client = client or anthropic.Anthropic(
            api_key=api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    def complete_structured(self, system: str, user: str, schema: type[T]) -> T:
        last_error: Exception | None = None
        for attempt in range(1 + MAX_INVALID_RETRIES):
            started = time.perf_counter()
            try:
                # Réponse brute d'abord : un refus arrive avec un texte vide, que la validation
                # Pydantic rejetterait avant qu'on ait pu lire stop_reason.
                raw = self._client.beta.messages.with_raw_response.parse(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=f"{system}\n\n{UNTRUSTED_NOTICE}",
                    messages=[{"role": "user", "content": user}],
                    output_format=schema,
                    output_config={"effort": self.effort},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            except anthropic.APIError as exc:  # déjà retentée par le SDK si c'était possible
                raise LLMError(f"appel LLM échoué : {type(exc).__name__}") from exc

            body: dict[str, Any] = raw.http_response.json()
            stop_reason = body.get("stop_reason")
            self._log(body, schema, started)
            if stop_reason == "refusal":
                category = (body.get("stop_details") or {}).get("category")
                raise LLMRefusalError(f"refus du modèle (catégorie : {category})")
            if stop_reason == "max_tokens":
                last_error = LLMError("réponse tronquée (max_tokens atteint)")
                continue
            try:
                parsed = raw.parse().parsed_output
            except ValidationError as exc:  # JSON reçu mais non conforme au schéma
                logger.warning(
                    "LLM %s : réponse invalide pour %s (tentative %d)",
                    self.model,
                    schema.__name__,
                    attempt + 1,
                )
                last_error = exc
                continue
            if isinstance(parsed, schema):
                return parsed
            last_error = LLMError("réponse sans objet structuré")

        raise LLMError(f"aucune réponse valide pour {schema.__name__}") from last_error

    def _log(self, body: dict[str, Any], schema: type[BaseModel], started: float) -> None:
        usage = body.get("usage") or {}
        logger.info(
            "LLM %s %s : %.0f ms, %s tokens en entrée, %s en sortie, arrêt=%s",
            body.get("model", self.model),
            schema.__name__,
            (time.perf_counter() - started) * 1000,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            body.get("stop_reason"),
        )


def get_llm(settings: Settings | None = None) -> LLMClient:
    """Implémentation à utiliser selon Settings.llm_provider."""
    settings = settings or get_settings()
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        return AnthropicLLM(settings)
    if provider == "fake":
        return FakeLLM({})  # aucune réponse : chaque agent passe sur son Finding de secours
    raise ValueError(f"LLM_PROVIDER inconnu : {settings.llm_provider!r} (anthropic ou fake)")
