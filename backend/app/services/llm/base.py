"""Interface commune à tous les clients LLM (contrat partagé)."""

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    def complete_structured(self, system: str, user: str, schema: type[T]) -> T:
        """Envoie un prompt et renvoie la réponse validée sous forme d'objet `schema`.

        `system` : instructions (rôle, règles). `user` : contenu à analyser.
        Lève une exception si aucune réponse valide n'a pu être obtenue.
        """
        ...
