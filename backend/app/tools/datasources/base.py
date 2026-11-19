"""Interface commune à toutes les sources de données (contrat partagé).

Les agents ne dépendent que de `DataSource` : ils fonctionnent de la même façon avec des
fichiers (FileDataSource) ou des sources live (Prometheus, Jaeger).
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.schemas.telemetry import LogRecord, MetricSeries, Span, TimeWindow


@runtime_checkable
class DataSource(Protocol):
    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        """Logs dont le timestamp est dans la fenêtre, triés par ordre chronologique."""
        ...

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        """Séries de métriques, avec uniquement les points situés dans la fenêtre.

        `names` : noms des métriques voulues (None = toutes). Une métrique demandée mais
        introuvable est simplement absente du résultat : pas d'exception, et jamais de
        valeur inventée. C'est l'appelant qui signale l'absence (`Finding.missing_data`).
        """
        ...

    def get_traces(self, window: TimeWindow) -> list[Span]:
        """Spans dont le début est dans la fenêtre."""
        ...
