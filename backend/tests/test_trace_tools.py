"""Tests unitaires des outils de traces (branche 11), sur des traces construites à la main."""

from datetime import UTC, datetime, timedelta

import pytest

from app.schemas.telemetry import Span, SpanStatus
from app.tools.trace_tools import (
    build_trace_tree,
    calculate_service_latency,
    find_error_spans,
    find_slow_spans,
    percentile,
    self_time_ms,
)

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def span(
    span_id: str,
    parent: str | None,
    service: str,
    operation: str,
    start_ms: float,
    duration_ms: float,
    *,
    trace_id: str = "t1",
    status: SpanStatus = "ok",
    error_type: str | None = None,
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        operation=operation,
        start=T0 + timedelta(milliseconds=start_ms),
        duration_ms=duration_ms,
        status=status,
        attributes={"error.type": error_type} if error_type else {},
    )


def slow_query_trace(trace_id: str = "t1", select_ms: float = 4800) -> list[Span]:
    """Forme de S03 : la gateway attend le handler, qui attend le SELECT.

    GET /orders/search  [0 ─────────────────────── select+12]
      search_orders       [3 ───────────────────── select+9]
        acquire             [8─10]
        SELECT orders            [10 ── select+10]
    """
    return [
        span("gw", None, "api-gateway", "GET /orders/search", 0, select_ms + 12, trace_id=trace_id),
        span("h", "gw", "orders-service", "search_orders", 3, select_ms + 6, trace_id=trace_id),
        span("acq", "h", "orders-service", "db.acquire_connection", 8, 2, trace_id=trace_id),
        span("sql", "h", "postgres", "SELECT orders", 10, select_ms, trace_id=trace_id),
    ]


# --- build_trace_tree --------------------------------------------------------------------------


def test_build_trace_tree_rebuilds_hierarchy() -> None:
    (tree,) = build_trace_tree(slow_query_trace())

    assert tree.trace_id == "t1"
    assert tree.orphan_count == 0
    assert [r.span.span_id for r in tree.roots] == ["gw"]
    handler = tree.roots[0].children[0]
    assert [c.span.span_id for c in handler.children] == ["acq", "sql"]
    assert tree.duration_ms == 4812


def test_build_trace_tree_separates_traces_and_sorts_them() -> None:
    spans = [*slow_query_trace("late"), *slow_query_trace("early")]
    spans[0] = spans[0].model_copy(update={"start": T0 + timedelta(seconds=10)})
    trees = build_trace_tree(spans)
    assert [t.trace_id for t in trees] == ["early", "late"]


def test_orphan_span_becomes_root_and_is_counted() -> None:
    spans = [span("a", None, "s", "root", 0, 100), span("b", "missing", "s", "lost", 10, 20)]
    (tree,) = build_trace_tree(spans)
    assert {r.span.span_id for r in tree.roots} == {"a", "b"}
    assert tree.orphan_count == 1


def test_parent_cycle_does_not_lose_spans() -> None:
    spans = [span("a", "b", "s", "x", 0, 10), span("b", "a", "s", "y", 1, 5)]
    (tree,) = build_trace_tree(spans)
    assert len(tree.nodes) == 2
    assert tree.orphan_count >= 1


# --- self_time_ms ------------------------------------------------------------------------------


def test_self_time_sequential_children() -> None:
    parent = span("p", None, "s", "p", 0, 100)
    children = [span("a", "p", "s", "a", 10, 30), span("b", "p", "s", "b", 50, 20)]
    assert self_time_ms(parent, children) == 50


def test_self_time_overlapping_children_uses_union() -> None:
    """Deux appels parallèles de 40 ms qui se chevauchent couvrent 60 ms, pas 80."""
    parent = span("p", None, "s", "p", 0, 100)
    children = [span("a", "p", "s", "a", 10, 40), span("b", "p", "s", "b", 30, 40)]
    assert self_time_ms(parent, children) == 40


def test_self_time_child_overflowing_parent_is_clipped() -> None:
    parent = span("p", None, "s", "p", 0, 100)
    children = [span("a", "p", "s", "a", 80, 500)]  # finit après son parent
    assert self_time_ms(parent, children) == 80


def test_self_time_without_children_is_duration() -> None:
    assert self_time_ms(span("p", None, "s", "p", 0, 42.5), []) == 42.5


# --- find_slow_spans ---------------------------------------------------------------------------


def test_slow_spans_threshold_puts_real_culprit_first() -> None:
    """Les 3 spans dépassent 1 s, mais le SELECT a le plus grand temps propre."""
    slow = find_slow_spans(slow_query_trace(), threshold_ms=1000)

    assert [s.operation for s in slow][0] == "SELECT orders"
    assert {s.operation for s in slow} == {"SELECT orders", "search_orders", "GET /orders/search"}
    assert slow[0].self_ms == 4800
    assert all(s.self_ms < 10 for s in slow[1:])
    assert slow[0].reference_ms == 1000


def test_slow_spans_percentile_is_computed_per_operation() -> None:
    """20 traces normales (SELECT 15 ms) et 1 lente : seule la lente dépasse son p95."""
    spans = [s for i in range(20) for s in slow_query_trace(f"n{i}", select_ms=15)]
    spans += slow_query_trace("slow", select_ms=4800)

    slow = find_slow_spans(spans, percentile_rank=95)

    assert {s.trace_id for s in slow} == {"slow"}
    assert slow[0].operation == "SELECT orders"
    assert slow[0].reference_ms < 1000  # p95 de SELECT orders, pas celui de la gateway


@pytest.mark.parametrize("kwargs", [{}, {"threshold_ms": 1, "percentile_rank": 95}])
def test_slow_spans_requires_exactly_one_criterion(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        find_slow_spans(slow_query_trace(), **kwargs)


# --- find_error_spans --------------------------------------------------------------------------


def test_error_spans_mark_where_the_error_is_born() -> None:
    """Forme de S01 : le timeout d'acquisition remonte jusqu'à la gateway."""
    spans = [
        span("gw", None, "api-gateway", "GET /orders", 0, 5012, status="error"),
        span("h", "gw", "orders-service", "list_orders", 3, 5006, status="error"),
        span(
            "acq",
            "h",
            "orders-service",
            "db.acquire_connection",
            8,
            5000,
            status="error",
            error_type="TimeoutError",
        ),
        span("ok", None, "api-gateway", "GET /orders", 0, 30, trace_id="t2"),
    ]
    errors = find_error_spans(spans)

    assert [e.span_id for e in errors] == ["acq", "gw", "h"]
    assert errors[0].is_origin and errors[0].error_type == "TimeoutError"
    assert not errors[1].is_origin and not errors[2].is_origin


def test_no_error_spans() -> None:
    assert find_error_spans(slow_query_trace()) == []


# --- calculate_service_latency -----------------------------------------------------------------


def test_service_latency_blames_postgres_not_the_gateway() -> None:
    latency = calculate_service_latency(slow_query_trace())

    assert [s.service for s in latency] == ["postgres", "orders-service", "api-gateway"]
    postgres, orders, gateway = latency
    assert postgres.total_self_ms == 4800
    assert postgres.share > 0.99
    assert gateway.total_self_ms == 6  # 4812 - 4806 : la gateway n'a presque rien fait
    assert [op.operation for op in orders.operations] == ["search_orders", "db.acquire_connection"]
    assert orders.operations[1].total_self_ms == 2
    assert sum(s.share for s in latency) == pytest.approx(1, abs=1e-3)


def test_service_latency_aggregates_several_traces() -> None:
    spans = slow_query_trace("a", select_ms=100) + slow_query_trace("b", select_ms=300)
    postgres = calculate_service_latency(spans)[0]
    assert postgres.span_count == 2
    assert postgres.mean_self_ms == 200
    assert postgres.max_self_ms == 300


def test_service_latency_empty() -> None:
    assert calculate_service_latency([]) == []


# --- percentile --------------------------------------------------------------------------------


def test_percentile_linear_interpolation() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 95) == 10
    assert percentile([1, 2, 3, 4, 5], 100) == 5
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1], 101)
