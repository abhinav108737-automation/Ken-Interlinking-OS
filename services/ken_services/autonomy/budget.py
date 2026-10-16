"""The per-(source, target) retry budget and cool-down: :class:`DatabaseRetryBudgetGate`.

ADR-0018 s4. This is the mechanism that makes repeated unsafe retries IMPOSSIBLE rather than
unlikely, and it is deliberately small enough to reason about:

* ``attempts >= max_attempts`` -> no publish, ever again, for that pair. The budget belongs
  to the PAIR, not to a plan or a deployment, so a superseded plan, a rolled-back deployment
  and a freshly proposed duplicate all draw on the same budget. Attaching it to a plan would
  reset it exactly when a pair has proved it keeps failing.
* ``cooldown_until`` in the future -> no publish now, whatever the decision says. Set on
  every failure and every automatic rollback.
* :meth:`check` is called UNDER THE PLAN'S LOCK immediately before the publish, never when
  the decision was taken: a decision can be minutes old and the pair may have failed since.
* :meth:`record` appends the attempt. The UNIQUE index on (source, target, attempt) means a
  racing second writer of the same attempt number is REFUSED by PostgreSQL; the caller sees
  the IntegrityError as :class:`BudgetRaceError` and does not publish. The budget cannot be
  spent twice.

``publish_attempts`` is append-only and carries a ``mode <> 'shadow'`` CHECK, so a shadow
pass physically cannot record an attempt -- which is what makes "shadow publishes nothing"
checkable rather than promised.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyMode,
    RetryBudgetState,
    RetryPolicy,
    VerificationProfile,
)
from ken_core.db.repositories import autonomy as repo
from ken_services.autonomy.protocol import BudgetVerdict, budget_from_attempts

__all__ = [
    "REASON_COOLING_DOWN",
    "REASON_EXHAUSTED",
    "BudgetRaceError",
    "DatabaseRetryBudgetGate",
]

REASON_EXHAUSTED = "budget_exhausted"
REASON_COOLING_DOWN = "cooling_down"
_ATTEMPT_UNIQUE = "uq_publish_attempts_pair_attempt"


class BudgetRaceError(RuntimeError):
    """Another writer took this attempt number. The caller must NOT publish.

    Raised instead of retrying with the next number on purpose: two concurrent publishers of
    one pair is itself the anomaly, and the second one proceeding is how a pair gets two
    links.
    """


class DatabaseRetryBudgetGate:
    """:class:`~ken_services.autonomy.protocol.RetryBudgetGate` over ``publish_attempts``."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        retry: RetryPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessionmaker = sessionmaker
        # The EFFECTIVE retry policy: the versioned autonomy policy, tightened (never loosened)
        # by KEN_AUTONOMY_PUBLISH_*. See ken_services.autonomy.policy.effective_retry.
        self._retry = retry
        self._clock = clock

    @property
    def max_attempts(self) -> int:
        return self._retry.max_attempts

    @property
    def cooldown(self) -> timedelta:
        return timedelta(seconds=self._retry.cooldown_s)

    def _state(
        self,
        *,
        source_page_id: uuid.UUID,
        target_page_id: uuid.UUID,
        counts: repo.BudgetCounts,
    ) -> RetryBudgetState:
        return budget_from_attempts(
            source_page_id=source_page_id,
            target_page_id=target_page_id,
            attempts=counts.attempts,
            failures=counts.failures,
            max_attempts=self.max_attempts,
            cooldown_until=counts.cooldown_until,
            last_attempt_at=counts.last_attempt_at,
        )

    async def state(
        self, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID
    ) -> RetryBudgetState:
        async with self._sessionmaker() as session:
            counts = await repo.budget_counts(
                session, source_page_id=source_page_id, target_page_id=target_page_id
            )
            await session.rollback()
        return self._state(
            source_page_id=source_page_id, target_page_id=target_page_id, counts=counts
        )

    async def check(self, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID) -> BudgetVerdict:
        """Whether this pair may be published RIGHT NOW. Stable reason codes, never prose."""
        state = await self.state(source_page_id=source_page_id, target_page_id=target_page_id)
        return self.verdict(state)

    async def check_in(
        self, session: AsyncSession, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID
    ) -> BudgetVerdict:
        """:meth:`check` inside the caller's transaction, so it runs under the plan's lock."""
        counts = await repo.budget_counts(
            session, source_page_id=source_page_id, target_page_id=target_page_id
        )
        return self.verdict(
            self._state(source_page_id=source_page_id, target_page_id=target_page_id, counts=counts)
        )

    def verdict(self, state: RetryBudgetState) -> BudgetVerdict:
        reasons: list[str] = []
        if state.exhausted:
            reasons.append(REASON_EXHAUSTED)
        if state.cooldown_until is not None and state.cooldown_until > self._clock():
            reasons.append(REASON_COOLING_DOWN)
        return BudgetVerdict(allowed=not reasons, state=state, reasons=tuple(reasons))

    async def record(
        self,
        *,
        plan_id: uuid.UUID,
        source_page_id: uuid.UUID,
        target_page_id: uuid.UUID,
        decision: AutonomyDecision,
        mode: AutonomyMode,
        profile: VerificationProfile,
        succeeded: bool,
        failure_code: str | None,
        rolled_back: bool,
        actor: str,
        deployment_id: uuid.UUID | None = None,
    ) -> RetryBudgetState:
        """Append one attempt and return the budget as it now stands.

        Raises:
            BudgetRaceError: another writer already took this attempt number.
        """
        async with self._sessionmaker() as session, session.begin():
            return await self.record_in(
                session,
                plan_id=plan_id,
                source_page_id=source_page_id,
                target_page_id=target_page_id,
                decision=decision,
                mode=mode,
                profile=profile,
                succeeded=succeeded,
                failure_code=failure_code,
                rolled_back=rolled_back,
                actor=actor,
                deployment_id=deployment_id,
            )

    async def record_in(
        self,
        session: AsyncSession,
        *,
        plan_id: uuid.UUID,
        source_page_id: uuid.UUID,
        target_page_id: uuid.UUID,
        decision: AutonomyDecision,
        mode: AutonomyMode,
        profile: VerificationProfile,
        succeeded: bool,
        failure_code: str | None,
        rolled_back: bool,
        actor: str,
        deployment_id: uuid.UUID | None = None,
    ) -> RetryBudgetState:
        """:meth:`record` in the caller's transaction -- the SAME one as the deployment
        intent, so a crash between them cannot lose an attempt and reopen the budget."""
        if mode is AutonomyMode.SHADOW:
            raise ValueError("a shadow pass never records a publish attempt")
        counts = await repo.budget_counts(
            session, source_page_id=source_page_id, target_page_id=target_page_id
        )
        attempt = counts.attempts + 1
        if attempt > self.max_attempts:
            raise BudgetRaceError("the retry budget for this pair is already spent")
        now = self._clock()
        failed = not succeeded or rolled_back
        cooldown = (now + self.cooldown) if failed else counts.cooldown_until
        try:
            await repo.insert_attempt(
                session,
                plan_id=plan_id,
                source_page_id=source_page_id,
                target_page_id=target_page_id,
                attempt=attempt,
                max_attempts=self.max_attempts,
                decision=decision,
                mode=mode,
                profile=profile,
                succeeded=succeeded,
                failure_code=failure_code,
                rolled_back=rolled_back,
                actor=actor,
                cooldown_until=cooldown,
                deployment_id=deployment_id,
                occurred_at=now,
            )
        except IntegrityError as exc:
            if _ATTEMPT_UNIQUE in str(exc.orig):
                raise BudgetRaceError("another publisher took this attempt number") from None
            raise
        return budget_from_attempts(
            source_page_id=source_page_id,
            target_page_id=target_page_id,
            attempts=attempt,
            failures=counts.failures + (1 if failed else 0),
            max_attempts=self.max_attempts,
            cooldown_until=cooldown,
            last_attempt_at=now,
        )
