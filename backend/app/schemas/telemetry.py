"""Format des données brutes de télémétrie (contrat partagé).

Utilisé par les datasets, les outils d'analyse et les sources live (fichiers, Prometheus,
Jaeger). Toute modification doit être validée par les deux membres.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Horodatage ISO 8601 obligatoirement avec fuseau, toujours ramené en UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class _Contract(BaseModel):
    # extra="forbid" : un champ inconnu (faute de frappe dans un dataset) est une erreur.
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(_Contract):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _check_order(self) -> "TimeWindow":
        if self.end <= self.start:
            raise ValueError("end doit être strictement après start")
        return self

    def contains(self, ts: datetime) -> bool:
        """Vrai si `ts` est dans la fenêtre [start, end] (bornes incluses)."""
        return self.start <= ts <= self.end


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogRecord(_Contract):
    timestamp: UtcDatetime
    level: LogLevel
    service: str
    message: str
    trace_id: str | None = None
    exception: str | None = None


class MetricPoint(_Contract):
    ts: UtcDatetime
    value: float


class MetricSeries(_Contract):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    points: list[MetricPoint] = Field(default_factory=list)


SpanStatus = Literal["ok", "error"]


class Span(_Contract):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None  # None = span racine
    service: str
    operation: str
    start: UtcDatetime
    duration_ms: float = Field(ge=0)
    status: SpanStatus = "ok"
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)
