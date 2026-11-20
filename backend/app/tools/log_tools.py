"""Outils d'analyse des logs : fonctions pures, sans LLM.

Ces fonctions produisent les chiffres que le Log Agent met dans ses Evidence.
Règle du projet : les chiffres viennent d'ici, jamais du LLM.
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.schemas.telemetry import LogLevel, LogRecord

ERROR_LEVELS: tuple[LogLevel, ...] = ("ERROR", "CRITICAL")
PROBLEM_LEVELS: tuple[LogLevel, ...] = ("WARNING", "ERROR", "CRITICAL")
MAX_REPORTED_PARSE_ERRORS = 20

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True)


class ParseResult(_Result):
    """Logs valides, et ce qui a été écarté (pour le signaler dans Finding.missing_data)."""

    records: list[LogRecord]
    rejected: int
    errors: list[str]  # au plus MAX_REPORTED_PARSE_ERRORS messages


class ErrorCount(_Result):
    """Nombre d'erreurs d'un service dans une tranche de temps."""

    service: str
    bucket_start: datetime
    count: int


class ErrorGroup(_Result):
    """Messages semblables (même motif normalisé) regroupés, avec leur période."""

    service: str
    level: LogLevel
    pattern: str
    count: int
    first_seen: datetime
    last_seen: datetime
    sample_message: str
    exception_type: str | None
    sample_exception: str | None
    trace_ids: list[str]


# --- parse_logs ------------------------------------------------------------------------------


def parse_logs(lines: Iterable[str | Mapping[str, Any]]) -> ParseResult:
    """Transforme des lignes JSON (ou des dicts) en LogRecord, sans s'arrêter aux lignes invalides.

    Les lignes vides sont ignorées. Les logs valides sont triés par ordre chronologique.
    """
    records: list[LogRecord] = []
    errors: list[str] = []
    rejected = 0

    for number, line in enumerate(lines, start=1):
        if isinstance(line, str) and not line.strip():
            continue
        try:
            data = json.loads(line) if isinstance(line, str) else line
            records.append(LogRecord.model_validate(data))
        except (json.JSONDecodeError, ValidationError) as exc:
            rejected += 1
            if len(errors) < MAX_REPORTED_PARSE_ERRORS:
                errors.append(f"ligne {number} : {_short_reason(exc)}")

    records.sort(key=lambda r: r.timestamp)
    return ParseResult(records=records, rejected=rejected, errors=errors)


def _short_reason(exc: json.JSONDecodeError | ValidationError) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"JSON invalide ({exc.msg})"
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "ligne"
    return f"{field} : {first['msg']}"


# --- count_errors ----------------------------------------------------------------------------


def count_errors(
    records: Iterable[LogRecord],
    *,
    levels: Sequence[LogLevel] = ERROR_LEVELS,
    bucket: timedelta = timedelta(minutes=1),
) -> list[ErrorCount]:
    """Compte les logs de niveau `levels` par tranche de `bucket` et par service.

    Seules les tranches non vides sont renvoyées, triées par temps puis par service.
    """
    counts: dict[tuple[datetime, str], int] = {}
    for record in records:
        if record.level not in levels:
            continue
        key = (floor_time(record.timestamp, bucket), record.service)
        counts[key] = counts.get(key, 0) + 1

    return [
        ErrorCount(service=service, bucket_start=start, count=count)
        for (start, service), count in sorted(counts.items())
    ]


def floor_time(ts: datetime, bucket: timedelta) -> datetime:
    """Début de la tranche qui contient ts (10:23:47 -> 10:23:00 pour une tranche d'1 min)."""
    step = bucket // timedelta(microseconds=1)
    if step <= 0:
        raise ValueError("bucket doit être strictement positif")
    elapsed = (ts - _EPOCH) // timedelta(microseconds=1)
    return _EPOCH + timedelta(microseconds=elapsed - elapsed % step)


# --- normalisation et extract_exceptions -----------------------------------------------------

# L'ordre compte : on remplace d'abord ce qui contient des chiffres (identifiants, IP)
# avant de remplacer les nombres restants.
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<ID>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b", re.I), "<ID>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<N>"),
    (re.compile(r"\s+"), " "),
]


def normalize_message(message: str) -> str:
    """Remplace les parties variables d'un message par des jetons <ID>, <IP>, <N>.

    "QueuePool limit reached, waited 2312ms" -> "QueuePool limit reached, waited <N>ms"
    """
    for pattern, token in _NORMALIZERS:
        message = pattern.sub(token, message)
    return message.strip()


def exception_type(exception: str | None) -> str | None:
    """'sqlalchemy.exc.TimeoutError: QueuePool limit...' -> 'sqlalchemy.exc.TimeoutError'."""
    if not exception:
        return None
    return exception.split(":", 1)[0].strip() or None


def extract_exceptions(
    records: Iterable[LogRecord],
    *,
    levels: Sequence[LogLevel] = PROBLEM_LEVELS,
    max_trace_ids: int = 5,
) -> list[ErrorGroup]:
    """Regroupe les logs problématiques par (service, niveau, message normalisé).

    Renvoie les groupes du plus fréquent au moins fréquent (à égalité : le plus ancien d'abord).
    """
    groups: dict[tuple[str, LogLevel, str], list[LogRecord]] = {}
    for record in sorted(records, key=lambda r: r.timestamp):
        if record.level not in levels:
            continue
        key = (record.service, record.level, normalize_message(record.message))
        groups.setdefault(key, []).append(record)

    result = [_build_group(key, members, max_trace_ids) for key, members in groups.items()]
    result.sort(key=lambda g: (-g.count, g.first_seen, g.service, g.pattern))
    return result


def _build_group(
    key: tuple[str, LogLevel, str], members: list[LogRecord], max_trace_ids: int
) -> ErrorGroup:
    service, level, pattern = key
    sample_exception = next((m.exception for m in members if m.exception), None)
    trace_ids: list[str] = []
    for member in members:
        if member.trace_id and member.trace_id not in trace_ids and len(trace_ids) < max_trace_ids:
            trace_ids.append(member.trace_id)
    return ErrorGroup(
        service=service,
        level=level,
        pattern=pattern,
        count=len(members),
        first_seen=members[0].timestamp,
        last_seen=members[-1].timestamp,
        sample_message=members[0].message,
        exception_type=exception_type(sample_exception),
        sample_exception=sample_exception,
        trace_ids=trace_ids,
    )
