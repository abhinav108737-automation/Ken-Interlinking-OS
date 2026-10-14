"""THE single link-plan transition service (ADR-0011 section 6).

Every status change of a ``link_plans`` row goes through :func:`transition`:

1. the plan row is locked ``SELECT ... FOR UPDATE`` (concurrent movers serialise);
2. ``expected_from`` (optional) must equal the locked status (optimistic intent check);
3. the move must be in ``ALLOWED_TRANSITIONS`` (terminal states have no exits);
4. the actor must match ``ACTOR_PATTERN`` (``user:`` / ``agent:<name>@<version>`` / ``system:``)
   and its CLASS must hold authority over the target status (``TRANSITION_AUTHORITY``: only
   ``user:`` approves, rejects or supersedes; only ``system:`` validates, deploys, verifies,
   rolls back or expires; an agent never decides). That a ``user:`` actor is authenticated
   is the calling API's responsibility (Phase 5);
5. ``status`` / ``updated_at`` are updated (the only columns the content trigger allows) and
   one append-only ``link_plan_transitions`` row is written.

The caller owns the transaction, so a transition commits atomically with whatever else the
caller writes (e.g. a superseding plan, an event). A refused transition raises a typed error
and changes nothing.

The INITIAL transition of a new plan (``None -> proposed``) is written by the engine together
with the plan insert (:func:`record_initial`), never through :func:`transition`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.recommendations import (
    ACTOR_PATTERN,
    LinkPlanStatus,
    PlanNotFoundError,
    PlanTransitionView,
    is_allowed_transition,
    may_transition,
)
from ken_core.db.repositories import recommendations as repo

__all__ = [
    "ENGINE_ACTOR",
    "IllegalTransitionError",
    "InvalidActorError",
    "PlanNotFoundError",
    "UnauthorizedActorError",
    "record_initial",
    "transition",
    "validate_actor",
]

ENGINE_ACTOR: Final = "system:recommendation-engine"
MAX_NOTE_CHARS: Final = 500
_ACTOR: Final = re.compile(ACTOR_PATTERN)


class IllegalTransitionError(Exception):
    """The move is not in ``ALLOWED_TRANSITIONS`` (or the plan is not in ``expected_from``)."""

    def __init__(
        self,
        current: LinkPlanStatus,
        target: LinkPlanStatus,
        *,
        expected: LinkPlanStatus | None = None,
    ) -> None:
        self.current = current
        self.target = target
        self.expected = expected
        if expected is not None and expected is not current:
            message = f"plan is {current.value}, not {expected.value}"
        else:
            message = f"illegal transition {current.value} -> {target.value}"
        super().__init__(message)


class InvalidActorError(ValueError):
    """The actor does not match ``ACTOR_PATTERN``. The message never echoes the value."""

    def __init__(self) -> None:
        super().__init__("actor must be user:<id>, agent:<name>@<version> or system:<name>")


class UnauthorizedActorError(PermissionError):
    """The actor's class may not move a plan into that status (``TRANSITION_AUTHORITY``)."""

    def __init__(self, target: LinkPlanStatus) -> None:
        self.target = target
        super().__init__(f"this actor class may not move a plan to {target.value}")


def _authorize(actor: str, target: LinkPlanStatus) -> None:
    if not may_transition(actor, target):
        raise UnauthorizedActorError(target)


def validate_actor(actor: str) -> str:
    if not isinstance(actor, str) or _ACTOR.fullmatch(actor) is None:
        raise InvalidActorError
    return actor


def _note(note: str | None) -> str | None:
    if note is None:
        return None
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note must be at most {MAX_NOTE_CHARS} characters")
    return note


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def transition(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    to_status: LinkPlanStatus,
    actor: str,
    note: str | None = None,
    expected_from: LinkPlanStatus | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> PlanTransitionView:
    """Move one plan to ``to_status`` (see the module docstring) and return the history row.

    Raises:
        InvalidActorError: ``actor`` does not match ``ACTOR_PATTERN`` (checked first).
        UnauthorizedActorError: the actor's class has no authority over ``to_status``.
        ValueError: ``note`` is longer than 500 characters.
        PlanNotFoundError: no such plan.
        IllegalTransitionError: the move is not allowed, or the plan is not ``expected_from``.
    """
    validate_actor(actor)
    note = _note(note)
    target = LinkPlanStatus(to_status)
    _authorize(actor, target)
    expected = None if expected_from is None else LinkPlanStatus(expected_from)
    row = await repo.lock_plan(session, plan_id=plan_id)
    if row is None:
        raise PlanNotFoundError
    current = LinkPlanStatus(row.status)
    if expected is not None and expected is not current:
        raise IllegalTransitionError(current, target, expected=expected)
    if not is_allowed_transition(current, target):
        raise IllegalTransitionError(current, target)
    now = max(clock(), row.updated_at)
    await repo.set_plan_status(session, plan_id=plan_id, status=target, now=now)
    return await repo.insert_transition(
        session,
        plan_id=plan_id,
        from_status=current,
        to_status=target,
        actor=actor,
        note=note,
        occurred_at=now,
    )


async def record_initial(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    status: LinkPlanStatus,
    actor: str,
    occurred_at: datetime,
    note: str | None = None,
) -> PlanTransitionView:
    """The first history row of a NEW plan (``None -> status``), same transaction as its insert."""
    validate_actor(actor)
    _authorize(actor, LinkPlanStatus(status))
    return await repo.insert_transition(
        session,
        plan_id=plan_id,
        from_status=None,
        to_status=LinkPlanStatus(status),
        actor=actor,
        note=_note(note),
        occurred_at=occurred_at,
    )
