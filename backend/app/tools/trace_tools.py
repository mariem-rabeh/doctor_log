"""Outils d'analyse des traces : fonctions pures, sans LLM.

Notion clé : le temps propre (self time) d'un span est sa durée moins le temps pendant
lequel au moins un de ses enfants tournait. C'est lui qui désigne le vrai coupable :
un GET /orders/search de 5 s n'est pas lent lui-même s'il attend un SELECT de 4,9 s.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from app.schemas.telemetry import Span, SpanStatus

_MS = timedelta(milliseconds=1)


# --- Arbre de trace --------------------------------------------------------------------------


@dataclass
class SpanNode:
    """Un span dans son arbre, avec ses enfants et son temps propre."""

    span: Span
    children: list["SpanNode"] = field(default_factory=list)
    self_ms: float = 0.0

    @property
    def end(self) -> datetime:
        return self.span.start + self.span.duration_ms * _MS


@dataclass
class TraceTree:
    """Tous les spans d'un trace_id, organisés en arbre."""

    trace_id: str
    roots: list[SpanNode]
    nodes: list[SpanNode]  # tous les nœuds, triés par début
    orphan_count: int  # spans dont le parent est absent (devenus racines)

    @property
    def start(self) -> datetime:
        return min(node.span.start for node in self.nodes)

    @property
    def duration_ms(self) -> float:
        end = max(node.end for node in self.nodes)
        return round((end - self.start) / _MS, 3)


def build_trace_tree(spans: Iterable[Span]) -> list[TraceTree]:
    """Regroupe les spans par trace_id et reconstruit chaque arbre.

    Un span dont le parent est introuvable devient une racine et est compté dans
    orphan_count. Les arbres sont triés par début, les enfants aussi.
    """
    by_trace: dict[str, list[Span]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)

    trees = [_build_one(trace_id, members) for trace_id, members in by_trace.items()]
    trees.sort(key=lambda tree: (tree.start, tree.trace_id))
    return trees


def _build_one(trace_id: str, spans: list[Span]) -> TraceTree:
    nodes: dict[str, SpanNode] = {}
    for span in sorted(spans, key=lambda s: s.start):
        nodes.setdefault(span.span_id, SpanNode(span))  # un span_id en double est ignoré

    roots: list[SpanNode] = []
    orphans = 0
    for node in nodes.values():
        parent_id = node.span.parent_span_id
        parent = nodes.get(parent_id) if parent_id is not None else None
        if parent is None or parent is node:
            roots.append(node)
            orphans += parent_id is not None
        else:
            parent.children.append(node)

    # Des spans pris dans une boucle de parents (a -> b -> a) ne sont accessibles depuis
    # aucune racine : on les rattache comme racines plutôt que de les perdre.
    reachable = _walk(roots)
    for node in nodes.values():
        if id(node) not in reachable:
            node.children = [c for c in node.children if id(c) not in reachable]
            roots.append(node)
            orphans += 1
            reachable |= _walk([node])

    for node in nodes.values():
        node.self_ms = self_time_ms(node.span, [child.span for child in node.children])

    ordered = sorted(nodes.values(), key=lambda n: n.span.start)
    roots.sort(key=lambda n: n.span.start)
    return TraceTree(trace_id=trace_id, roots=roots, nodes=ordered, orphan_count=orphans)


def _walk(roots: list[SpanNode]) -> set[int]:
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        stack.extend(node.children)
    return seen


def self_time_ms(parent: Span, children: Sequence[Span]) -> float:
    """Durée du parent moins le temps couvert par au moins un enfant (union des intervalles).

    Les enfants sont limités à la période du parent ; le résultat n'est jamais négatif.
    """
    parent_end = parent.duration_ms
    intervals = sorted(
        (
            max(0.0, (child.start - parent.start) / _MS),
            min(parent_end, (child.start - parent.start) / _MS + child.duration_ms),
        )
        for child in children
    )
    covered, current_start, current_end = 0.0, None, None
    for begin, end in intervals:
        if end <= begin:
            continue
        if current_end is None or begin > current_end:
            if current_start is not None and current_end is not None:
                covered += current_end - current_start
            current_start, current_end = begin, end
        else:
            current_end = max(current_end, end)
    if current_start is not None and current_end is not None:
        covered += current_end - current_start
    return round(max(0.0, parent.duration_ms - covered), 3)


# --- Résultats ---------------------------------------------------------------------------------


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True)


class SpanInfo(_Result):
    """Résumé d'un span, prêt à devenir une Evidence trc-xxx."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    operation: str
    start: datetime
    duration_ms: float
    self_ms: float
    status: SpanStatus
    error_type: str | None


class SlowSpan(SpanInfo):
    reference_ms: float  # seuil absolu, ou percentile de son opération


class ErrorSpan(SpanInfo):
    is_origin: bool  # aucun de ses enfants n'est en erreur : l'erreur naît ici


class OperationLatency(_Result):
    operation: str
    span_count: int
    total_self_ms: float
    mean_self_ms: float
    p95_self_ms: float
    max_self_ms: float


class ServiceLatency(_Result):
    service: str
    span_count: int
    total_self_ms: float
    mean_self_ms: float
    p95_self_ms: float
    max_self_ms: float
    share: float  # part du temps propre total de toutes les traces (0-1)
    operations: list[OperationLatency]


def _info(node: SpanNode) -> SpanInfo:
    span = node.span
    error_type = span.attributes.get("error.type")
    return SpanInfo(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        service=span.service,
        operation=span.operation,
        start=span.start,
        duration_ms=span.duration_ms,
        self_ms=node.self_ms,
        status=span.status,
        error_type=str(error_type) if error_type is not None else None,
    )


def _all_nodes(spans: Iterable[Span]) -> list[SpanNode]:
    return [node for tree in build_trace_tree(spans) for node in tree.nodes]


# --- Statistiques ------------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float:
    """Percentile par interpolation linéaire (même résultat que numpy par défaut)."""
    if not values:
        raise ValueError("percentile d'une liste vide")
    if not 0 <= p <= 100:
        raise ValueError("p doit être entre 0 et 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


# --- find_slow_spans ---------------------------------------------------------------------------


def find_slow_spans(
    spans: Iterable[Span],
    *,
    threshold_ms: float | None = None,
    percentile_rank: float | None = None,
) -> list[SlowSpan]:
    """Spans plus longs qu'un seuil absolu, ou que le percentile de leur propre opération.

    Donner exactement un des deux critères. Le percentile est calculé séparément pour
    chaque (service, opération). Résultat trié par temps propre décroissant : le vrai
    coupable passe devant les spans parents qui ne font que l'attendre.
    """
    if (threshold_ms is None) == (percentile_rank is None):
        raise ValueError("donner exactement un critère : threshold_ms ou percentile_rank")

    nodes = _all_nodes(spans)
    references: dict[tuple[str, str], float] = {}
    if percentile_rank is not None:
        durations: dict[tuple[str, str], list[float]] = {}
        for node in nodes:
            durations.setdefault((node.span.service, node.span.operation), []).append(
                node.span.duration_ms
            )
        references = {key: percentile(vals, percentile_rank) for key, vals in durations.items()}

    slow = []
    for node in nodes:
        key = (node.span.service, node.span.operation)
        reference = threshold_ms if threshold_ms is not None else references[key]
        if node.span.duration_ms > reference:
            info = _info(node).model_dump()
            slow.append(SlowSpan(**info, reference_ms=round(reference, 3)))

    slow.sort(key=lambda s: (-s.self_ms, -s.duration_ms, s.start))
    return slow


# --- find_error_spans --------------------------------------------------------------------------


def find_error_spans(spans: Iterable[Span]) -> list[ErrorSpan]:
    """Spans en erreur. is_origin=True quand aucun enfant n'est en erreur : c'est là que
    l'erreur naît, les parents ne font que la propager. Origines d'abord, puis par début."""
    errors = []
    for node in _all_nodes(spans):
        if node.span.status != "error":
            continue
        is_origin = not any(child.span.status == "error" for child in node.children)
        errors.append(ErrorSpan(**_info(node).model_dump(), is_origin=is_origin))

    errors.sort(key=lambda e: (not e.is_origin, e.start, e.span_id))
    return errors


# --- calculate_service_latency -----------------------------------------------------------------


def calculate_service_latency(spans: Iterable[Span]) -> list[ServiceLatency]:
    """Temps propre par service, avec le détail par opération.

    Trié du service qui consomme le plus de temps propre au moins gourmand.
    """
    nodes = _all_nodes(spans)
    grand_total = sum(node.self_ms for node in nodes)

    by_service: dict[str, dict[str, list[float]]] = {}
    for node in nodes:
        operations = by_service.setdefault(node.span.service, {})
        operations.setdefault(node.span.operation, []).append(node.self_ms)

    result = []
    for service, operations in by_service.items():
        all_values = [value for values in operations.values() for value in values]
        ops = sorted(
            (
                OperationLatency(operation=name, **_stats(values))
                for name, values in operations.items()
            ),
            key=lambda op: (-op.total_self_ms, op.operation),
        )
        stats = _stats(all_values)
        share = stats["total_self_ms"] / grand_total if grand_total else 0.0
        result.append(
            ServiceLatency(service=service, **stats, share=round(share, 4), operations=ops)
        )

    result.sort(key=lambda s: (-s.total_self_ms, s.service))
    return result


def _stats(values: list[float]) -> dict[str, float]:
    total = sum(values)
    return {
        "span_count": len(values),
        "total_self_ms": round(total, 1),
        "mean_self_ms": round(total / len(values), 1),
        "p95_self_ms": round(percentile(values, 95), 1),
        "max_self_ms": round(max(values), 1),
    }
