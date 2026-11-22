"""FileDataSource : lit un dossier de dataset (logs.json, metrics.json, traces.json).

Les fichiers sont lus et validés une seule fois, au premier accès, avec les modèles du
contrat (app/schemas/telemetry.py). Chaque appel filtre ensuite sur la fenêtre demandée.

- Fichier absent : la source renvoie une liste vide (comme une source live sans données)
  et le signale dans `missing_files` ; c'est à l'agent de l'écrire dans missing_data.
- Fichier présent mais invalide : DatasetError, car c'est un dataset cassé à corriger.
"""

import json
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from app.config import get_settings
from app.schemas.telemetry import LogRecord, MetricSeries, Span, TimeWindow

LOGS_FILE = "logs.json"
METRICS_FILE = "metrics.json"
TRACES_FILE = "traces.json"
EXPECTED_FILE = "expected_diagnosis.json"
DATA_FILES = (LOGS_FILE, METRICS_FILE, TRACES_FILE)

_LOGS = TypeAdapter(list[LogRecord])
_METRICS = TypeAdapter(list[MetricSeries])
_TRACES = TypeAdapter(list[Span])

T = TypeVar("T")


class DatasetError(ValueError):
    """Un fichier de dataset existe mais n'est pas valide."""


class FileDataSource:
    """Implémente DataSource à partir d'un dossier de dataset."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise FileNotFoundError(f"dataset introuvable : {self.path}")

    @classmethod
    def from_name(cls, name: str, root: Path | None = None) -> "FileDataSource":
        """FileDataSource.from_name("db-pool-exhaustion") -> datasets/db-pool-exhaustion."""
        return cls((root or get_settings().datasets_dir) / name)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def missing_files(self) -> list[str]:
        return [f for f in DATA_FILES if not (self.path / f).is_file()]

    # --- Interface DataSource --------------------------------------------------------------

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        """Logs dont le timestamp est dans la fenêtre, triés par ordre chronologique."""
        return [log for log in self._logs if window.contains(log.timestamp)]

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        """Séries demandées, réduites aux points de la fenêtre.

        Une série sans aucun point dans la fenêtre est absente du résultat, comme une
        métrique introuvable : il n'y a rien à analyser, et rien n'est inventé.
        """
        wanted = None if names is None else set(names)
        result = []
        for series in self._metrics:
            if wanted is not None and series.name not in wanted:
                continue
            points = [p for p in series.points if window.contains(p.ts)]
            if points:
                result.append(series.model_copy(update={"points": points}))
        return result

    def get_traces(self, window: TimeWindow) -> list[Span]:
        """Spans dont le début est dans la fenêtre, triés par début."""
        return [span for span in self._traces if window.contains(span.start)]

    # --- Utilitaires (CLI, évaluation) -----------------------------------------------------

    def time_range(self) -> TimeWindow:
        """Plus petite fenêtre qui contient toutes les données du dataset."""
        stamps = [log.timestamp for log in self._logs]
        stamps += [p.ts for series in self._metrics for p in series.points]
        stamps += [span.start for span in self._traces]
        if not stamps:
            raise DatasetError(f"{self.name} : aucune donnée horodatée")
        return TimeWindow(start=min(stamps), end=max(stamps))

    def expected_diagnosis(self) -> dict[str, Any] | None:
        """Contenu de expected_diagnosis.json (utilisé par l'évaluation), ou None."""
        path = self.path / EXPECTED_FILE
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    # --- Lecture (une seule fois, au premier accès) ----------------------------------------

    @cached_property
    def _logs(self) -> list[LogRecord]:
        return sorted(self._load(LOGS_FILE, _LOGS), key=lambda log: log.timestamp)

    @cached_property
    def _metrics(self) -> list[MetricSeries]:
        return self._load(METRICS_FILE, _METRICS)

    @cached_property
    def _traces(self) -> list[Span]:
        return sorted(self._load(TRACES_FILE, _TRACES), key=lambda span: span.start)

    def _load(self, filename: str, adapter: TypeAdapter[list[T]]) -> list[T]:
        path = self.path / filename
        if not path.is_file():
            return []
        try:
            return adapter.validate_json(path.read_bytes())
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise DatasetError(
                f"{self.name}/{filename} invalide à {where} : {first['msg']}"
            ) from exc


def list_datasets(root: Path | None = None) -> list[str]:
    """Noms des dossiers de datasets/ qui contiennent au moins un fichier de données."""
    root = root or get_settings().datasets_dir
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and any((child / f).is_file() for f in DATA_FILES)
    )
