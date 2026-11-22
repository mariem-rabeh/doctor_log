"""Client LLM pour l'API Gemini de Google (offre gratuite disponible).

La réponse est demandée en JSON, avec le schéma attendu dans le prompt, puis validée par
Pydantic ; une réponse invalide est redemandée une fois. Nouvelles tentatives HTTP : http_utils.
"""

import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.services.llm.errors import LLMError, LLMRefusalError
from app.services.llm.http_utils import log_call, post_json, with_schema
from app.services.llm.safety import UNTRUSTED_NOTICE

T = TypeVar("T", bound=BaseModel)

# Alias : toujours le Flash-Lite actuel (quota gratuit plus large que Flash).
DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_INVALID_RETRIES = 1


class GeminiLLM:
    """LLMClient branché sur l'API Gemini (generateContent)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = settings.llm_model or DEFAULT_GEMINI_MODEL
        self.max_retries = settings.llm_max_retries
        self._key = settings.llm_api_key.get_secret_value()
        if not self._key:
            raise LLMError("LLM_API_KEY est vide : ajoute ta clé Gemini dans le fichier .env")
        self._client = client or httpx.Client(timeout=settings.llm_timeout_seconds)
        self._sleep = sleep

    def complete_structured(self, system: str, user: str, schema: type[T]) -> T:
        body = {
            "systemInstruction": {"parts": [{"text": f"{system}\n\n{UNTRUSTED_NOTICE}"}]},
            "contents": [{"role": "user", "parts": [{"text": with_schema(user, schema)}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        last_error: Exception | None = None
        for _ in range(1 + MAX_INVALID_RETRIES):
            data, ms = post_json(
                self._client,
                API_URL.format(model=self.model),
                body,
                {"x-goog-api-key": self._key},
                name="Gemini",
                max_retries=self.max_retries,
                sleep=self._sleep,
            )
            candidate = (data.get("candidates") or [{}])[0]
            finish = candidate.get("finishReason")
            usage = data.get("usageMetadata") or {}
            log_call(
                self.model,
                schema,
                ms,
                usage.get("promptTokenCount"),
                usage.get("candidatesTokenCount"),
                finish,
            )
            if finish in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
                raise LLMRefusalError(f"refus de Gemini ({finish})")
            parts = candidate.get("content", {}).get("parts", [])
            try:
                return schema.model_validate_json("".join(p.get("text", "") for p in parts))
            except ValidationError as exc:
                last_error = exc
        raise LLMError(f"aucune réponse valide pour {schema.__name__}") from last_error
