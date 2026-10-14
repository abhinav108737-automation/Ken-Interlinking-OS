"""The LangGraph supervisor (ADR-0012 section 2)::

    load_page -> triage -> understand -> discover -> plan -> critique -> check_invariants
        -> policy_gate -> finalize

Every node is wrapped by :func:`guarded_node`, which:

1. binds the run's ToolBox to a fresh :class:`StepScratch`;
2. runs the node under its timeout (``node_timeout_seconds``, plus the model timeout for
   ``plan`` / ``critique``);
3. validates the return against the node's declared update model (:func:`validate_update`:
   an undeclared key FAILS instead of being dropped by LangGraph);
4. turns a :class:`ToolError` or a node timeout into a typed ``errors`` entry, so routing
   ends the run at ``finalize``;
5. emits one :class:`AgentStepView` per executed node to the recorder (live, through the
   runner's sink), and advances ``step_seq``.

Any other exception is a bug: its step is recorded as ``failed`` / ``internal`` and the
exception propagates to the runner, which finishes the run ``failed``.

``langgraph`` is imported lazily inside :func:`build_graph`, after
:func:`~ken_agents.tracing_guard.enforce_no_remote_tracing` (ADR-0012 section 9).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel

from ken_agents import nodes
from ken_agents.nodes import NodeContext
from ken_agents.recording import StepScratch, bound_output, bound_tool_calls
from ken_agents.state import (
    AgentState,
    CheckInvariantsUpdate,
    CritiqueUpdate,
    DiscoverUpdate,
    FailureUpdate,
    FinalizeUpdate,
    LoadPageUpdate,
    NodeUpdate,
    PlanUpdate,
    PolicyGateUpdate,
    TriageUpdate,
    UnderstandUpdate,
    validate_update,
)
from ken_agents.tools import ToolError
from ken_agents.tracing_guard import enforce_no_remote_tracing
from ken_contracts.agents import (
    AgentErrorCode,
    AgentNode,
    AgentStepView,
    TriageDecision,
)

__all__ = [
    "NODE_ORDER",
    "NodeFn",
    "StepRecorder",
    "StepSink",
    "build_graph",
    "guarded_node",
    "run_graph",
]

type NodeReturn = BaseModel | Mapping[str, Any]
type NodeFn = Callable[[AgentState, NodeContext, StepScratch], Awaitable[NodeReturn]]
type StepSink = Callable[[AgentStepView], Awaitable[None]]
type GraphNode = Callable[[AgentState], Awaitable[dict[str, object]]]

NODE_ORDER: Final = tuple(AgentNode)
"""Graph order (the enum is declared in graph order)."""

_SPECS: Final[dict[AgentNode, tuple[NodeFn, type[NodeUpdate]]]] = {
    AgentNode.LOAD_PAGE: (nodes.load_page, LoadPageUpdate),
    AgentNode.TRIAGE: (nodes.triage, TriageUpdate),
    AgentNode.UNDERSTAND: (nodes.understand, UnderstandUpdate),
    AgentNode.DISCOVER: (nodes.discover, DiscoverUpdate),
    AgentNode.PLAN: (nodes.plan, PlanUpdate),
    AgentNode.CRITIQUE: (nodes.critique, CritiqueUpdate),
    AgentNode.CHECK_INVARIANTS: (nodes.check_invariants, CheckInvariantsUpdate),
    AgentNode.POLICY_GATE: (nodes.policy_gate, PolicyGateUpdate),
    AgentNode.FINALIZE: (nodes.finalize, FinalizeUpdate),
}


@dataclass(slots=True)
class StepRecorder:
    """Collects the run's steps and forwards each to the runner's sink as it finishes."""

    run_id: UUID
    trace_id: str
    sink: StepSink
    clock: Callable[[], datetime]
    steps: list[AgentStepView] = field(default_factory=list)

    async def record(self, step: AgentStepView) -> None:
        self.steps.append(step)
        await self.sink(step)


def _budget(node: AgentNode, ctx: NodeContext) -> float | None:
    if node is AgentNode.POLICY_GATE:
        # No asyncio deadline on the WRITE node: a cancellation landing after the hand-off
        # committed would report a failed run whose plans are already queued (QA L1). The
        # hand-off transaction is bounded by the DATABASE instead (SET LOCAL lock_timeout /
        # statement_timeout in DatabaseAgentBackend.hand_off; QA N1).
        return None
    budget: float = ctx.policy.node_timeout_seconds
    if node is AgentNode.PLAN and ctx.policy.planner.use_model:
        budget += ctx.policy.planner.timeout_seconds
    if node is AgentNode.CRITIQUE and ctx.policy.critic.use_model:
        budget += ctx.policy.critic.timeout_seconds
    return budget


def guarded_node(
    node: AgentNode,
    fn: NodeFn,
    update_model: type[NodeUpdate],
    *,
    ctx: NodeContext,
    recorder: StepRecorder,
) -> GraphNode:
    """Wrap ``fn`` as a LangGraph node (see the module docstring)."""
    budget = _budget(node, ctx)

    async def run(state: AgentState) -> dict[str, object]:
        scratch = StepScratch()
        ctx.tools.bind(scratch)
        started_at = recorder.clock()
        t0 = time.monotonic()
        update: dict[str, object]
        failure: BaseException | None = None
        try:
            async with asyncio.timeout(budget):
                returned = await fn(state, ctx, scratch)
            update = validate_update(update_model, returned)
        except ToolError as exc:
            scratch.fail(exc.code)
            update = validate_update(FailureUpdate, {"errors": [*state.errors, exc.code]})
        except TimeoutError:
            scratch.fail(AgentErrorCode.TIMEOUT)
            update = validate_update(
                FailureUpdate, {"errors": [*state.errors, AgentErrorCode.TIMEOUT]}
            )
        except Exception as exc:  # a bug (incl. StateContractError): record, then raise
            scratch.fail(AgentErrorCode.INTERNAL)
            scratch.output = {"error": type(exc).__name__}
            failure = exc
            update = {}
        finally:
            ctx.tools.bind(None)
        finished_at = max(recorder.clock(), started_at)
        await recorder.record(
            AgentStepView(
                run_id=recorder.run_id,
                seq=state.step_seq,
                node=node,
                status=scratch.status,
                attempt=1,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=max(0, round((time.monotonic() - t0) * 1000)),
                model_role=scratch.model_role,
                model=scratch.model,
                fallback_used=scratch.fallback_used,
                fallback_reason=scratch.fallback_reason,
                error_code=scratch.error_code,
                tool_calls=bound_tool_calls(scratch.tool_calls),
                output=bound_output(scratch.output),
                trace_id=recorder.trace_id,
                span_id=secrets.token_hex(8),
            )
        )
        if failure is not None:
            raise failure
        update["step_seq"] = state.step_seq + 1
        return update

    run.__name__ = f"node_{node.value}"
    return run


def _route(node: AgentNode) -> Callable[[AgentState], str]:
    following = NODE_ORDER[NODE_ORDER.index(node) + 1]

    def route(state: AgentState) -> str:
        if state.errors:
            return AgentNode.FINALIZE.value
        if node is AgentNode.TRIAGE and state.triage_decision is not TriageDecision.OPTIMIZE:
            return AgentNode.FINALIZE.value
        if node is AgentNode.DISCOVER and not state.candidates:
            return AgentNode.FINALIZE.value
        return following.value

    return route


def build_graph(
    *,
    ctx: NodeContext,
    recorder: StepRecorder,
    overrides: Mapping[AgentNode, tuple[NodeFn, type[NodeUpdate]]] | None = None,
) -> Any:
    """Compile the supervisor for ONE run. ``overrides`` exists for tests only."""
    enforce_no_remote_tracing()
    from langgraph.graph import END, START, StateGraph

    specs = {**_SPECS, **(overrides or {})}
    graph: Any = StateGraph(AgentState)
    for node in NODE_ORDER:
        fn, update_model = specs[node]
        graph.add_node(node.value, guarded_node(node, fn, update_model, ctx=ctx, recorder=recorder))
    graph.add_edge(START, AgentNode.LOAD_PAGE.value)
    for node in NODE_ORDER[:-1]:
        following = NODE_ORDER[NODE_ORDER.index(node) + 1]
        targets = {following.value, AgentNode.FINALIZE.value}
        graph.add_conditional_edges(node.value, _route(node), {t: t for t in sorted(targets)})
    graph.add_edge(AgentNode.FINALIZE.value, END)
    return graph.compile()


async def run_graph(compiled: Any, initial: AgentState) -> AgentState:
    """Execute and re-validate the final state (ADR-0012 section 4)."""
    result = await compiled.ainvoke(initial)
    return AgentState.model_validate(dict(result))
