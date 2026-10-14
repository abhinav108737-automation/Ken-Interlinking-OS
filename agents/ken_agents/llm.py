"""Role-scoped model access for the ``plan`` and ``critique`` nodes (ADR-0012 section 6).

A node never sees a provider implementation, settings or a model name. It gets a
:class:`RoleModel`: one role (FAST or REASONING), one timeout, temperature 0, and only
``complete_structured`` (output validated by a frozen Pydantic model). :meth:`RoleModel.ask`
turns every model problem into a :class:`~ken_contracts.agents.FallbackReason` instead of an
exception, because a model problem never fails a run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from ken_agents.recording import StepScratch
from ken_contracts.agents import FallbackReason
from ken_contracts.model import (
    GenerationOptions,
    Message,
    ModelProvider,
    ModelRole,
    ModelSchemaViolationError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from ken_core.logging import get_logger

__all__ = ["ModelAnswer", "RoleModel"]

logger = get_logger("ken_agents.llm")


@dataclass(frozen=True, slots=True)
class ModelAnswer[S: BaseModel]:
    """A validated value and the model that served it, or the reason there is none."""

    value: S | None
    model: str | None
    fallback_reason: FallbackReason | None


class RoleModel:
    """``complete_structured`` for ONE role, with a timeout; nothing else."""

    def __init__(self, *, provider: ModelProvider, role: ModelRole, timeout_seconds: float) -> None:
        self._provider = provider
        self.role = role
        self.timeout_seconds = timeout_seconds

    async def ask[S: BaseModel](
        self, *, messages: Sequence[Message], schema: type[S], step: StepScratch
    ) -> ModelAnswer[S]:
        step.model_role = self.role
        options = GenerationOptions(temperature=0.0, timeout_s=self.timeout_seconds)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await self._provider.complete_structured(
                    role=self.role, messages=messages, schema=schema, options=options
                )
        except (TimeoutError, ModelTimeoutError):
            return self._fallback(FallbackReason.MODEL_TIMEOUT, "timeout")
        except ModelSchemaViolationError:
            return self._fallback(FallbackReason.SCHEMA_VIOLATION, "schema_violation")
        except ModelUnavailableError:
            return self._fallback(FallbackReason.MODEL_UNAVAILABLE, "unavailable")
        except Exception as exc:  # any other model problem: still never fails the run
            return self._fallback(FallbackReason.MODEL_UNAVAILABLE, type(exc).__name__)
        step.model = result.completion.model
        return ModelAnswer(value=result.value, model=result.completion.model, fallback_reason=None)

    def _fallback[S: BaseModel](self, reason: FallbackReason, detail: str) -> ModelAnswer[S]:
        logger.warning(
            "agent_model_fallback", role=self.role.value, reason=reason.value, detail=detail
        )
        return ModelAnswer(value=None, model=None, fallback_reason=reason)
