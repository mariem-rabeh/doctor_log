"""Graphe LangGraph de l'analyse d'un incident.

prepare -> (log, metrics, trace en parallèle) -> retrieve -> rca -> verify -> rca (révision)
                                                                          \\-> report
Une erreur dans un agent d'analyse n'arrête pas le graphe : elle est notée dans missing_data.
retrieve (RAG) donne au RCA des runbooks : des connaissances, jamais des preuves.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from app.agents import log_agent, metrics_agent, rca_agent, trace_agent, verification_agent
from app.config import get_settings
from app.graph.state import IncidentState
from app.schemas.diagnosis import DiagnosisReport, Document, Finding, VerificationResult
from app.schemas.enums import AgentName, AgentStatus, Conclusion, SignalSource, Verdict
from app.schemas.telemetry import TimeWindow
from app.services.llm.base import LLMClient
from app.tools import knowledge
from app.tools.datasources.base import DataSource

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 2
CONFIRMED_CONFIDENCE = 0.8

ANALYSIS_AGENTS: list[tuple[AgentName, str, SignalSource, ModuleType]] = [
    (AgentName.LOG, "log_finding", SignalSource.LOGS, log_agent),
    (AgentName.METRICS, "metrics_finding", SignalSource.METRICS, metrics_agent),
    (AgentName.TRACE, "trace_finding", SignalSource.TRACES, trace_agent),
]
FINDING_KEYS = ("log_finding", "metrics_finding", "trace_finding")


def build_graph(source: DataSource, llm: LLMClient, documents: Sequence[Document] = ()) -> Any:
    graph: StateGraph[IncidentState] = StateGraph(IncidentState)

    def prepare(state: IncidentState) -> IncidentState:
        statuses = {name: AgentStatus.PENDING for name in AgentName}
        statuses.update({name: AgentStatus.RUNNING for name, *_ in ANALYSIS_AGENTS})
        return {
            "iteration_count": 0,
            "hypotheses": [],
            "verification": None,
            "agent_statuses": statuses,
        }

    def retrieve(state: IncidentState) -> IncidentState:
        query = knowledge.build_query(_findings(state))
        return {"documents": knowledge.search(documents, query)}

    def rca(state: IncidentState) -> IncidentState:
        verification = state.get("verification")
        feedback = verification.issues if verification else []
        hypotheses = rca_agent.run(_findings(state), llm, feedback, state.get("documents", []))
        return {
            "hypotheses": hypotheses,
            "iteration_count": state.get("iteration_count", 0) + 1,
            "agent_statuses": {
                AgentName.RCA: AgentStatus.COMPLETED,
                AgentName.VERIFICATION: AgentStatus.RUNNING,
            },
        }

    def verify(state: IncidentState) -> IncidentState:
        result = verification_agent.run(
            state.get("hypotheses", []), _findings(state), source, state["time_window"], llm
        )
        return {
            "verification": result,
            "agent_statuses": {AgentName.VERIFICATION: AgentStatus.COMPLETED},
        }

    def report(state: IncidentState) -> IncidentState:
        return {"final_report": build_report(state)}

    graph.add_node("prepare", prepare)
    for name, key, signal, module in ANALYSIS_AGENTS:
        node = _analysis_node(name, key, signal, module, source, llm)
        # LangGraph attend un paramètre nommé « state » ; Callable[...] ne peut pas l'exprimer.
        graph.add_node(name.value, node)  # type: ignore[arg-type]
        graph.add_edge("prepare", name.value)
    graph.add_node("retrieve", retrieve)
    graph.add_node("rca", rca)
    graph.add_node("verify", verify)
    graph.add_node("report", report)

    graph.add_edge(START, "prepare")
    graph.add_edge([name.value for name, *_ in ANALYSIS_AGENTS], "retrieve")  # attend les trois
    graph.add_edge("retrieve", "rca")
    graph.add_edge("rca", "verify")
    graph.add_conditional_edges("verify", next_step, {"rca": "rca", "report": "report"})
    graph.add_edge("report", END)
    return graph.compile()


def _analysis_node(
    name: AgentName,
    key: str,
    signal: SignalSource,
    module: ModuleType,
    source: DataSource,
    llm: LLMClient,
) -> Callable[[IncidentState], IncidentState]:
    def node(state: IncidentState) -> IncidentState:
        try:
            finding = module.run(source, state["time_window"], llm)
            status = AgentStatus.COMPLETED
        except Exception as exc:  # un agent en panne ne doit pas arrêter l'analyse
            logger.exception("agent %s en échec", name)
            finding = Finding(
                agent=signal,
                summary=f"Agent {name} en échec.",
                missing_data=[f"{signal} : agent en échec ({type(exc).__name__})"],
            )
            status = AgentStatus.FAILED
        # Le RCA démarre juste après les trois agents : l'interface l'affiche « en cours ».
        statuses = {name: status, AgentName.RCA: AgentStatus.RUNNING}
        return cast(IncidentState, {key: finding, "agent_statuses": statuses})

    return node


def next_step(state: IncidentState) -> str:
    verification = state.get("verification")
    revise = verification is not None and verification.verdict is Verdict.REVISION_REQUESTED
    return "rca" if revise and state.get("iteration_count", 0) < MAX_ITERATIONS else "report"


def _findings(state: IncidentState) -> list[Finding]:
    return [f for key in FINDING_KEYS if (f := state.get(key)) is not None]  # type: ignore[misc]


def conclude(verification: VerificationResult | None) -> Conclusion:
    if verification is None or verification.verdict is not Verdict.ACCEPTED:
        return Conclusion.INSUFFICIENT_EVIDENCE
    if verification.confidence >= CONFIRMED_CONFIDENCE:
        return Conclusion.CONFIRMED
    return Conclusion.PROBABLE


def build_report(state: IncidentState) -> DiagnosisReport:
    findings = _findings(state)
    hypotheses = state.get("hypotheses", [])
    verification = state.get("verification")
    conclusion = conclude(verification)
    top = hypotheses[0] if hypotheses else None
    return DiagnosisReport(
        incident_id=state["incident_id"],
        conclusion=conclusion,
        probable_cause=top.cause
        if top and conclusion is not Conclusion.INSUFFICIENT_EVIDENCE
        else None,
        confidence=verification.confidence if verification else 0.0,
        hypotheses=hypotheses,
        evidences=[e for f in findings for e in f.evidences],
        verification=verification,
        recommendations=top.verification_actions if top else [],
        missing_data=[m for f in findings for m in f.missing_data],
        documents=state.get("documents", []),
        iteration_count=state.get("iteration_count", 0),
        generated_at=datetime.now(UTC),
    )


def run_analysis(
    source: DataSource,
    window: TimeWindow,
    llm: LLMClient,
    incident_id: str,
    on_update: Callable[[IncidentState], None] | None = None,
    documents: Sequence[Document] | None = None,
) -> DiagnosisReport:
    """Lance le graphe ; on_update reçoit l'état après chaque étape (suivi par l'API).

    documents : base de connaissances du RAG ; None = les runbooks de knowledge/.
    """
    if documents is None:
        documents = knowledge.load_documents(get_settings().knowledge_dir)
    graph = build_graph(source, llm, documents)
    state: IncidentState = {"incident_id": incident_id, "time_window": window}
    for step in graph.stream(state, stream_mode="values"):
        state = step
        if on_update is not None:
            on_update(state)
    report = state.get("final_report")
    if report is None:
        raise RuntimeError("le graphe s'est terminé sans rapport")
    return report
