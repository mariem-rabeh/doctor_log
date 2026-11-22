"""Client LLM pour l'API Groq (très rapide, offre gratuite disponible).

API au format OpenAI (chat/completions) en mode JSON, avec le schéma attendu dans le prompt,
puis validée par Pydantic ; une réponse invalide est redemandée une fois.
Nouvelles tentatives HTTP : http_utils.
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

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
API_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_INVALID_RETRIES = 1


class GroqLLM:
    """LLMClient branché sur l'API Groq."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = settings.llm_model or DEFAULT_GROQ_MODEL
        self.max_retries = settings.llm_max_retries
        self._key = settings.llm_api_key.get_secret_value()
        if not self._key:
            raise LLMError("LLM_API_KEY est vide : ajoute ta clé Groq dans le fichier .env")
        self._client = client or httpx.Client(timeout=settings.llm_timeout_seconds)
        self._sleep = sleep

    def complete_structured(self, system: str, user: str, schema: type[T]) -> T:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{UNTRUSTED_NOTICE}"},
                {"role": "user", "content": with_schema(user, schema)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        last_error: Exception | None = None
        for _ in range(1 + MAX_INVALID_RETRIES):
            data, ms = post_json(
                self._client,
                API_URL,
                body,
                {"Authorization": f"Bearer {self._key}"},
                name="Groq",
                max_retries=self.max_retries,
                sleep=self._sleep,
            )
            choice = (data.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            usage = data.get("usage") or {}
            log_call(
                self.model,
                schema,
                ms,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                finish,
            )
            if finish == "content_filter":
                raise LLMRefusalError("refus de Groq (content_filter)")
            content = (choice.get("message") or {}).get("content") or ""
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
        raise LLMError(f"aucune réponse valide pour {schema.__name__}") from last_error
