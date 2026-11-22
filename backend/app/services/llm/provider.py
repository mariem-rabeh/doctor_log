"""Choix du client LLM selon Settings.llm_provider."""

from app.config import Settings, get_settings
from app.services.llm.base import LLMClient
from app.services.llm.fake import FakeLLM
from app.services.llm.gemini import GeminiLLM
from app.services.llm.groq import GroqLLM


def get_llm(settings: Settings | None = None) -> LLMClient:
    """« gemini » ou « groq » : vrai modèle ; « fake » : aucun réseau."""
    settings = settings or get_settings()
    provider = settings.llm_provider.lower()
    if provider == "gemini":
        return GeminiLLM(settings)
    if provider == "groq":
        return GroqLLM(settings)
    if provider == "fake":
        return FakeLLM({})
    raise ValueError(f"LLM_PROVIDER inconnu : {settings.llm_provider!r} (gemini, groq ou fake)")
