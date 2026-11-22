"""Erreurs communes à tous les clients LLM."""


class LLMError(RuntimeError):
    """Aucune réponse valide n'a pu être obtenue du LLM."""


class LLMRefusalError(LLMError):
    """Le modèle a refusé de répondre pour une raison de sécurité."""
