"""Source live : logs JSON écrits par la demo-app + Prometheus + Jaeger.

Les agents l'utilisent exactement comme une FileDataSource : même interface DataSource.
"""

from collections.abc import Sequence
from pathlib import Path

from app.config import Settings
from app.schemas.telemetry import LogRecord, MetricSeries, Span, TimeWindow
from app.tools.datasources.jaeger_source import JaegerSource
from app.tools.datasources.prometheus_source import PrometheusSource
from app.tools.log_tools import parse_logs


class LiveDataSource:
    def __init__(self, logs_dir: Path, prometheus: PrometheusSource, jaeger: JaegerSource) -> None:
        self.logs_dir = logs_dir
        self.prometheus = prometheus
        self.jaeger = jaeger

    @classmethod
    def from_settings(cls, settings: Settings) -> "LiveDataSource":
        return cls(
            settings.logs_dir,
            PrometheusSource(settings.prometheus_url),
            JaegerSource(settings.jaeger_url),
        )

    def get_logs(self, window: TimeWindow) -> list[LogRecord]:
        """Toutes les lignes valides des fichiers *.log, dans la fenêtre, par ordre chronologique.

        Une ligne illisible est ignorée (parse_logs la compte) au lieu de bloquer l'analyse.
        """
        records: list[LogRecord] = []
        for path in sorted(self.logs_dir.glob("*.log")):
            with path.open(encoding="utf-8") as lines:
                records += [r for r in parse_logs(lines).records if window.contains(r.timestamp)]
        return sorted(records, key=lambda r: r.timestamp)

    def get_metrics(
        self, window: TimeWindow, names: Sequence[str] | None = None
    ) -> list[MetricSeries]:
        return self.prometheus.get_metrics(window, names)

    def get_traces(self, window: TimeWindow) -> list[Span]:
        return self.jaeger.get_traces(window)
