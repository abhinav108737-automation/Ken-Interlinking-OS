"""Per-step scratch and bounded, secret-free step payloads (ADR-0012 section 5).

A node never sees the run store. While a node runs, the graph's node wrapper hands it a
:class:`StepScratch`; the ToolBox appends a :class:`ToolCallRecord` to it per call, the
model client notes the role it asked for and the model the provider reported, and the node
notes fallbacks and its small ``output``. The wrapper turns the scratch into one
``AgentStepView`` and passes it to the runner's sink.

``output`` and ``tool_calls`` must each fit ``MAX_STEP_OUTPUT_BYTES`` as JSON (a DB CHECK).
:func:`bound_output` and :func:`bound_tool_calls` enforce it deterministically: strings and
lists are shortened first, and if that is not enough the payload is replaced by a fixed
summary, so the same input always gives the same stored payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from ken_contracts.agents import (
    MAX_STEP_OUTPUT_BYTES,
    AgentErrorCode,
    FallbackReason,
    StepStatus,
    ToolCallRecord,
)
from ken_contracts.model import ModelRole

__all__ = ["StepScratch", "bound_output", "bound_tool_calls", "json_size"]

_MAX_STRING: Final = 300
_MAX_LIST: Final = 20


@dataclass(slots=True)
class StepScratch:
    """What one executing node accumulates for its step record."""

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model_role: ModelRole | None = None
    model: str | None = None
    fallback_used: bool = False
    fallback_reason: FallbackReason | None = None
    status: StepStatus = StepStatus.SUCCEEDED
    error_code: AgentErrorCode | None = None
    output: dict[str, Any] = field(default_factory=dict)

    def fallback(self, reason: FallbackReason) -> None:
        """Record that the node used its deterministic rule (first reason wins)."""
        if not self.fallback_used:
            self.fallback_used = True
            self.fallback_reason = reason

    def skip(self) -> None:
        self.status = StepStatus.SKIPPED

    def fail(self, code: AgentErrorCode) -> None:
        self.status = StepStatus.FAILED
        self.error_code = code


def json_size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))


def _shrink(value: Any, depth: int = 0) -> Any:
    """JSON-safe copy with long strings and lists shortened (deterministic)."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[: _MAX_STRING - 3] + "..."
    if isinstance(value, bool | int | float) or value is None:
        return value
    if depth >= 6:
        return "..."
    if isinstance(value, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        items = [_shrink(v, depth + 1) for v in list(value)[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            items.append(f"... {len(value) - _MAX_LIST} more")
        return items
    return _shrink(str(value), depth + 1)


def bound_output(output: dict[str, Any], limit: int = MAX_STEP_OUTPUT_BYTES) -> dict[str, Any]:
    """``output`` as stored: JSON-safe and at most ``limit`` bytes of JSON."""
    shrunk: dict[str, Any] = _shrink(output)
    if json_size(shrunk) <= limit:
        return shrunk
    return {"truncated": True, "original_bytes": json_size(output), "keys": sorted(shrunk)[:50]}


def bound_tool_calls(
    calls: list[ToolCallRecord], limit: int = MAX_STEP_OUTPUT_BYTES
) -> list[ToolCallRecord]:
    """Tool call records as stored: summaries shrunk, then dropped, then the list cut."""
    bounded = [c.model_copy(update={"summary": _shrink(c.summary)}) for c in calls]
    if json_size([c.model_dump(mode="json") for c in bounded]) <= limit:
        return bounded
    bounded = [c.model_copy(update={"summary": {}}) for c in calls]
    while bounded and json_size([c.model_dump(mode="json") for c in bounded]) > limit:
        bounded.pop()
    return bounded
