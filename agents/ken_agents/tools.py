"""The agent tools (ADR-0012 section 7): narrow, typed, allowlisted, logged, bounded.

:class:`ToolBox` is everything a node may do to the outside world: one async method per
:class:`~ken_contracts.agents.ToolName`, over the frozen
:class:`~ken_services.agents.protocol.AgentBackend`. Every call:

* runs under a timeout: ``policy.node_timeout_seconds`` split evenly across its attempts,
  so that every attempt of one call fits inside the node's own timeout;
* is retried ``policy.tool_retries`` times when it is a READ tool; ``hand_off_plans``, the
  one REVIEW tool, is never retried;
* appends one :class:`ToolCallRecord` per attempt (a bounded, secret-free summary; an error
  is named by class, never by its text) to the current step;
* raises :class:`ToolError` with a typed :class:`AgentErrorCode` when it finally fails.

The ToolBox binds the run id and the review clock itself, so a node cannot hand off on
behalf of another run, and the actors are bound inside the service (no actor here).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from ken_agents.recording import StepScratch
from ken_contracts.agents import (
    TOOL_PERMISSIONS,
    AgentErrorCode,
    AgentPolicy,
    ToolCallRecord,
    ToolName,
    ToolPermission,
)
from ken_contracts.intelligence import LinkEligibility, PageAnalysis
from ken_contracts.recommendations import LinkPlan
from ken_core.logging import get_logger
from ken_services.agents.protocol import (
    AgentBackend,
    HandOffResult,
    PageSnapshot,
    PlanCheck,
    PlanReviewInput,
)

__all__ = ["ToolBox", "ToolError"]

logger = get_logger("ken_agents.tools")


class ToolError(Exception):
    """A tool failed after its retries. Carries a typed code, never exception text."""

    def __init__(self, tool: ToolName, code: AgentErrorCode) -> None:
        super().__init__(f"tool {tool.value} failed ({code.value})")
        self.tool = tool
        self.code = code


class ToolBox:
    """The allowlisted tools of ONE run. Bound to the executing step by the node wrapper."""

    def __init__(
        self,
        *,
        backend: AgentBackend,
        policy: AgentPolicy,
        agent_run_id: UUID,
        clock: Callable[[], datetime],
    ) -> None:
        self._backend = backend
        self._node_timeout = policy.node_timeout_seconds
        self._retries = policy.tool_retries
        self._run_id = agent_run_id
        self._clock = clock
        self._step: StepScratch | None = None

    def bind(self, step: StepScratch | None) -> None:
        """Attach the step that records the calls (the node wrapper only)."""
        self._step = step

    # ------------------------------------------------------------- READ tools
    async def get_page(self, *, page_id: UUID) -> PageSnapshot | None:
        return await self._call(
            ToolName.GET_PAGE,
            lambda: self._backend.get_page(page_id=page_id),
            lambda r: {"found": r is not None, "version": r.current_version if r else None},
        )

    async def get_analysis(self, *, page_id: UUID) -> PageAnalysis | None:
        return await self._call(
            ToolName.GET_ANALYSIS,
            lambda: self._backend.get_analysis(page_id=page_id),
            lambda r: {"found": r is not None, "version": r.page_version if r else None},
        )

    async def get_eligibility(self, *, page_id: UUID) -> LinkEligibility | None:
        return await self._call(
            ToolName.GET_ELIGIBILITY,
            lambda: self._backend.get_eligibility(page_id=page_id),
            lambda r: {"found": r is not None, "as_source": r.as_source if r else None},
        )

    async def list_proposed_plans(self, *, source_page_id: UUID, limit: int) -> list[LinkPlan]:
        plans = await self._call(
            ToolName.LIST_PROPOSED_PLANS,
            lambda: self._backend.list_proposed_plans(source_page_id=source_page_id, limit=limit),
            lambda r: {"limit": limit, "count": len(r)},
        )
        return list(plans[:limit])

    async def check_plan(self, *, plan_id: UUID) -> PlanCheck:
        return await self._call(
            ToolName.CHECK_PLAN,
            lambda: self._backend.check_plan(plan_id=plan_id),
            lambda r: {"plan_id": str(plan_id), "ok": r.ok, "problems": list(r.problems)},
        )

    # ------------------------------------------------------------ REVIEW tool
    async def hand_off_plans(self, *, reviews: Sequence[PlanReviewInput]) -> HandOffResult:
        """The policy gate: ONE call, never retried (it writes)."""
        reviewed_at = self._clock()
        return await self._call(
            ToolName.HAND_OFF_PLANS,
            lambda: self._backend.hand_off(
                agent_run_id=self._run_id, reviews=tuple(reviews), reviewed_at=reviewed_at
            ),
            lambda r: {
                "reviews": len(reviews),
                "handed_off": len(r.handed_off),
                "recorded": len(r.recorded),
                "conflicts": len(r.conflicts),
            },
        )

    # --------------------------------------------------------------- engine
    async def _call[R](
        self,
        tool: ToolName,
        call: Callable[[], Awaitable[R]],
        summarise: Callable[[R], dict[str, Any]],
    ) -> R:
        permission = TOOL_PERMISSIONS[tool]
        attempts = 1 + (self._retries if permission is ToolPermission.READ else 0)
        # Every attempt fits the node's own budget, so a retry can actually happen.
        timeout = self._node_timeout / attempts
        code = AgentErrorCode.TOOL_FAILED
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    result = await call()
            except TimeoutError:
                code = AgentErrorCode.TIMEOUT
                self._record(tool, permission, started, attempt, code, {"error": "timeout"})
            except Exception as exc:
                code = AgentErrorCode.TOOL_FAILED
                self._record(
                    tool, permission, started, attempt, code, {"error": type(exc).__name__}
                )
                logger.warning(
                    "agent_tool_failed",
                    tool=tool.value,
                    attempt=attempt,
                    error_class=type(exc).__name__,
                )
            else:
                self._record(tool, permission, started, attempt, None, summarise(result))
                return result
        raise ToolError(tool, code)

    def _record(
        self,
        tool: ToolName,
        permission: ToolPermission,
        started: float,
        attempt: int,
        code: AgentErrorCode | None,
        summary: dict[str, Any],
    ) -> None:
        if self._step is None:
            return
        self._step.tool_calls.append(
            ToolCallRecord(
                tool=tool,
                permission=permission,
                ok=code is None,
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                attempt=attempt,
                error_code=code,
                summary=summary,
            )
        )
