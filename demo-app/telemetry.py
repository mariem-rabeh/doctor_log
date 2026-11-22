"""Instrumentation de la demo-app : traces OpenTelemetry vers Jaeger et logs JSON.

- Traces : FastAPI et httpx instrumentés automatiquement (spans SQL créés dans app.py), export OTLP (HTTP)
  vers Jaeger (OTEL_EXPORTER_OTLP_ENDPOINT, défaut http://localhost:4318).
- Logs : une ligne JSON par log, au format LogRecord du backend, avec trace_id et span_id,
  écrite dans LOG_DIR/<service>.log : c'est ce fichier que lit la source live.
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

LOG_DIR = Path(os.getenv("LOG_DIR", Path(__file__).resolve().parents[1] / "logs"))
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        line = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "message": record.getMessage(),
            "trace_id": f"{context.trace_id:032x}" if context.is_valid else None,
            "span_id": f"{context.span_id:016x}" if context.is_valid else None,
            "exception": self.formatException(record.exc_info).splitlines()[-1]
            if record.exc_info
            else None,
        }
        return json.dumps(line, ensure_ascii=False)


def setup(app: FastAPI, service: str) -> logging.Logger:
    """Active traces et logs JSON pour un service ; renvoie le logger à utiliser."""
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{OTLP_ENDPOINT}/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics")
    HTTPXClientInstrumentor().instrument()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / f"{service}.log", encoding="utf-8")
    handler.setFormatter(JsonFormatter(service))
    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger
