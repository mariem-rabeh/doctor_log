"""Tests de l'état du graphe (branche 1).

Ignorés tant que app.schemas.telemetry (branche 2) n'existe pas.
"""

import pytest

pytest.importorskip("app.schemas.telemetry")

from langgraph.graph import END, START, StateGraph  # noqa: E402

from app.graph.state import IncidentState, merge_statuses  # noqa: E402
from app.schemas.enums import AgentName, AgentStatus  # noqa: E402


def test_merge_statuses_keeps_both_sides() -> None:
    left = {AgentName.LOG: AgentStatus.COMPLETED}
    right = {AgentName.TRACE: AgentStatus.RUNNING}
    assert merge_statuses(left, right) == {
        AgentName.LOG: AgentStatus.COMPLETED,
        AgentName.TRACE: AgentStatus.RUNNING,
    }


def test_merge_statuses_right_wins_on_same_agent() -> None:
    left = {AgentName.LOG: AgentStatus.RUNNING}
    right = {AgentName.LOG: AgentStatus.COMPLETED}
    assert merge_statuses(left, right) == {AgentName.LOG: AgentStatus.COMPLETED}


def test_merge_statuses_handles_empty_start() -> None:
    assert merge_statuses(None, {AgentName.RCA: AgentStatus.PENDING}) == {
        AgentName.RCA: AgentStatus.PENDING
    }


def test_parallel_nodes_do_not_overwrite_each_other() -> None:
    graph = StateGraph(IncidentState)
    for agent in (AgentName.LOG, AgentName.METRICS, AgentName.TRACE):

        def node(_: IncidentState, agent: AgentName = agent) -> IncidentState:
            return {"agent_statuses": {agent: AgentStatus.COMPLETED}}

        graph.add_node(agent.value, node)
        graph.add_edge(START, agent.value)
        graph.add_edge(agent.value, END)

    result = graph.compile().invoke({"incident_id": "inc-1"})

    assert result["agent_statuses"] == {
        AgentName.LOG: AgentStatus.COMPLETED,
        AgentName.METRICS: AgentStatus.COMPLETED,
        AgentName.TRACE: AgentStatus.COMPLETED,
    }
