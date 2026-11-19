"""Faux client LLM pour les tests : réponses préenregistrées par schéma, sans réseau."""

from collections import deque
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# Une réponse préenregistrée : un objet Pydantic ou un dict (validé au moment de l'appel).
CannedResponse = BaseModel | dict[str, Any]


@dataclass(frozen=True)
class LLMCall:
    """Trace d'un appel reçu, pour vérifier dans les tests ce que l'agent a envoyé."""

    system: str
    user: str
    schema: type[BaseModel]


class FakeLLM:
    """Implémente `LLMClient` sans appeler de vrai modèle.

    Pour chaque schéma, on fournit une réponse ou une liste de réponses. Une liste est
    consommée dans l'ordre (utile pour les révisions successives) ; la dernière réponse
    est ensuite renvoyée à chaque nouvel appel.
    """

    def __init__(
        self, responses: dict[type[BaseModel], CannedResponse | list[CannedResponse]]
    ) -> None:
        self._responses: dict[type[BaseModel], deque[CannedResponse]] = {}
        for schema, value in responses.items():
            queue = deque(value) if isinstance(value, list) else deque([value])
            if not queue:
                raise ValueError(f"liste de réponses vide pour {schema.__name__}")
            self._responses[schema] = queue
        self.calls: list[LLMCall] = []

    def complete_structured(self, system: str, user: str, schema: type[T]) -> T:
        self.calls.append(LLMCall(system=system, user=user, schema=schema))

        queue = self._responses.get(schema)
        if queue is None:
            raise LookupError(f"FakeLLM : aucune réponse préenregistrée pour {schema.__name__}")
        response = queue.popleft() if len(queue) > 1 else queue[0]

        # Comme un vrai client : la réponse doit être valide pour le schéma demandé.
        data = response.model_dump() if isinstance(response, BaseModel) else response
        return schema.model_validate(data)
