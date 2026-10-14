"""The human review service (ADR-0013 sections 4 and 7): :class:`DatabaseReviewService`.

Deterministic, over the Phase 3 transition service; one database transaction per action
(per PLAN for bulk). It implements :class:`ken_contracts.review.ReviewService`.

**Principal-based.** Every method takes the authenticated :class:`Principal`; ``actor``
(``user:<username>``) and ``user_id`` are DERIVED from it, never read from a request. A
principal without ``Permission.REVIEW`` (defence in depth: the API already checked) or with
a non-``user:`` actor is refused.

**Idempotent.** ``request_fingerprint`` = sha256 of the canonical JSON (sorted keys) of
``{action, plan_id, user_id, body}`` (bulk: ``{action, plan_ids (sorted), user_id, body}``,
``body`` = the request without ``plan_ids``). A stored ``operation_id`` with the same
fingerprint is REPLAYED (``idempotent_replay = true``, nothing changes); with a different one
it is a 409. A concurrent duplicate is caught twice: re-read under the plan's row lock, and
the UNIQUE violation on ``review_actions.operation_id`` (rollback, re-read, replay or 409).
A refused action (404 / 409 / 422) writes NO row, so a retry re-executes.

**Each action, in one transaction:**

1. edits only: the per-source plan-writer advisory lock (``validity.lock_source_plans``),
   taken BEFORE the row lock (the pipeline's lock order);
2. the plan row is locked ``FOR UPDATE`` (404 if missing), and the operation id re-checked;
3. ``expected_status`` must equal the plan's status (409 ``expected_status_mismatch``), and
   the status must allow the action (``REVIEWABLE`` / ``REJECTABLE`` / ``EDITABLE_STATUSES``;
   bulk: ``awaiting_approval`` only);
4. approve, bulk (approve and reject) and edits: re-validation under the lock --
   ``check_plan_freshness`` (stale: 409 ``stale_page_version``) and the engine's durable
   invalidity (``validity.plan_invalidity``: 409 ``protected_page`` / ``conflict``);
5. the transitions, through ``transitions.transition`` with ``expected_from``, as the user;
6. events (``link.approved`` / ``link.rejected`` / ``link.proposed`` for an edited plan,
   ``LinkPlanEvent`` with the user as actor, ``correlation_id`` = operation id, or the bulk
   operation id);
7. one append-only ``review_actions`` row (the audit AND the editorial-feedback record).

**Edits** (``edit_anchor`` / ``replace_target``) never mutate a plan. The old plan moves to
``superseded`` FIRST (the open-pair and open-span unique indexes are not deferrable), then a
fresh ``LinkPlan`` is inserted (``anchor_source = "edited"``, initial ``proposed`` row by
``system:review-service`` with a note naming the user) and moved to ``awaiting_approval``
(and ``approved`` with ``approve = true``) by the user. The new plan is built per ADR-0013
s7 (:meth:`DatabaseReviewService._edited_plan`): the CURRENT source version and the CURRENT
block whose text hash is the plan's ``context_hash``; the anchor is ``block.text[start:end]``
verbatim (a whole-phrase occurrence, inside one safe text run, no link ancestor, not
overlapping another open plan's span, passing the anchor rules); scoring is recomputed with
the CURRENT policies (edit: ``with_placement`` over the pair's stored relevance; replace:
``score_pair`` for the new pair, then ``with_placement``); a replaced destination passes the
engine's pair filters over live facts (``filters.pair_rejection``).

Placement quality of an edited anchor: the engine's quality for the target phrase it
matches (case-insensitively), else :data:`EDITED_PLACEMENT_QUALITY`; minus the opening-block
penalty. The anchor rules use the matched phrase's kind (a topic alias may be one word),
else the non-topic minimum.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.intelligence import Intent, LinkEligibility, TaxonomyPolicy, analysis_version
from ken_contracts.recommendations import (
    ENGINE_VERSION,
    SUBJECT_LINK_PLAN,
    CandidateRejection,
    ExperimentGuard,
    FreshnessCheck,
    LinkPlan,
    LinkPlanEvent,
    LinkPlanStatus,
    PlacementLocator,
    PlanFreshness,
    PlanNotFoundError,
    RankingPolicy,
    ReasonCode,
    RecommendationPolicy,
    actor_class,
)
from ken_contracts.review import (
    EDITABLE_STATUSES,
    REJECTABLE_STATUSES,
    REVIEW_SERVICE_ACTOR,
    REVIEWABLE_STATUSES,
    ApproveRequest,
    BulkAction,
    BulkItemResult,
    BulkReviewRequest,
    BulkReviewResult,
    EditAnchorRequest,
    FeedbackReason,
    RejectRequest,
    ReplaceTargetRequest,
    ReviewAction,
    ReviewError,
    ReviewResult,
    TargetOption,
)
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import recommendations as rec_repo
from ken_core.db.repositories import review as review_repo
from ken_core.db.repositories.events import append_event
from ken_services.intelligence.embeddings import current_recipe_fingerprint
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.intelligence.text import fold
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock, extract_blocks
from ken_services.jobs.runner import utc_now
from ken_services.recommendations.channels import EmbeddingKey, cosine_to
from ken_services.recommendations.filters import NoExperiments, PairState, pair_rejection
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.pipeline import MAX_TARGET_TITLE_CHARS
from ken_services.recommendations.placement import (
    OPENING_BLOCK_PENALTY,
    PLACEMENT_QUALITY,
    AnchorPhrase,
    anchor_phrases,
    anchor_rule_ok,
    phrase_occurrences,
    preview_for,
    span_is_safe,
)
from ken_services.recommendations.policy import load_recommendation_policy, resolve_ranking_policy
from ken_services.recommendations.rationale import render_rationale
from ken_services.recommendations.scoring import (
    PairFeatures,
    ScoredPair,
    anchor_reason_code,
    merge_reason_codes,
    score_pair,
    with_placement,
)
from ken_services.recommendations.transitions import record_initial, transition, validate_actor
from ken_services.recommendations.validity import (
    lock_source_plans,
    page_eligibility,
    plan_invalidity,
)
from ken_services.review.errors import (
    EditRejectedError,
    InvalidatedError,
    OperationReusedError,
    PairConflictError,
    ReviewPermissionError,
    StaleError,
    StatusConflictError,
)

__all__ = [
    "EDITED_PLACEMENT_QUALITY",
    "EMITTED_BY",
    "DatabaseReviewService",
    "bulk_fingerprint",
    "bulk_item_operation_id",
    "request_fingerprint",
]

EMITTED_BY: Final = "ken-review"
EDITED_PLACEMENT_QUALITY: Final = PLACEMENT_QUALITY["target_keyword"]
"""Placement quality of an edited anchor that matches none of the target's phrases (the
engine's lowest phrase quality: the reviewer chose it, the engine did not)."""
LINK_ANCESTOR: Final = "a"
"""Always excluded for an edited span, whatever the policy: never nest or overlap a link."""
_ANCHOR_CODES: Final = frozenset(
    {ReasonCode.ANCHOR_MATCHES_TARGET_TOPIC, ReasonCode.ANCHOR_MATCHES_TARGET_TITLE}
)
_OPERATION_UNIQUE: Final = "uq_review_actions_operation_id"
_S = LinkPlanStatus


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #
def _sha256_json(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def request_fingerprint(
    *, action: ReviewAction, plan_id: uuid.UUID, user_id: uuid.UUID, body: BaseModel
) -> str:
    """sha256 of the canonical JSON of ``{action, plan_id, user_id, body}`` (ADR-0013 s7)."""
    return _sha256_json(
        {
            "action": ReviewAction(action).value,
            "plan_id": str(plan_id),
            "user_id": str(user_id),
            "body": body.model_dump(mode="json"),
        }
    )


def bulk_fingerprint(*, request: BulkReviewRequest, user_id: uuid.UUID) -> str:
    """sha256 of ``{action, plan_ids (sorted), user_id, body}``; ``body`` excludes plan ids."""
    return _sha256_json(
        {
            "action": BulkAction(request.action).value,
            "plan_ids": sorted(str(p) for p in request.plan_ids),
            "user_id": str(user_id),
            "body": request.model_dump(mode="json", exclude={"plan_ids"}),
        }
    )


def bulk_item_operation_id(bulk_operation_id: uuid.UUID, plan_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(bulk_operation_id, str(plan_id))


def _collapse(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _bounded_title(title: str | None) -> str | None:
    if title is None or len(title) <= MAX_TARGET_TITLE_CHARS:
        return title
    return title[: MAX_TARGET_TITLE_CHARS - 1].rstrip() + "…"


def _intent(value: Any) -> Intent | None:
    return None if value is None else Intent(value)


# --------------------------------------------------------------------------- #
# Internal records
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Live:
    """Live facts read inside the action's transaction."""

    facts: dict[uuid.UUID, rec_repo.PageFacts]
    eligibility: dict[uuid.UUID, LinkEligibility]
    blocks: tuple[ContentBlock, ...] = ()
    links: list[rec_repo.SourceLink] = field(default_factory=list)

    @property
    def linked(self) -> set[uuid.UUID]:
        return {link.target_page_id for link in self.links if link.target_page_id is not None}


@dataclass(frozen=True, slots=True)
class _Applied:
    from_status: LinkPlanStatus
    to_status: LinkPlanStatus
    anchor_before: str
    target_before: uuid.UUID
    new_plan: LinkPlan | None = None
    new_plan_status: LinkPlanStatus | None = None


@dataclass(frozen=True, slots=True)
class _Request:
    """What every action records, whatever its kind."""

    kind: ReviewAction
    operation_id: uuid.UUID
    fingerprint: str
    reasons: tuple[FeedbackReason, ...]
    note: str
    correlation_id: uuid.UUID
    bulk_operation_id: uuid.UUID | None = None


class _ReplayFoundError(Exception):
    def __init__(self, stored: review_repo.StoredAction) -> None:
        super().__init__("operation already applied")
        self.stored = stored


type _Step = Callable[[AsyncSession, LinkPlanRow, str, datetime], Awaitable[_Applied]]


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class DatabaseReviewService:
    """:class:`ken_contracts.review.ReviewService` over PostgreSQL (module docstring)."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: RecommendationPolicy | None = None,
        ranking: RankingPolicy | None = None,
        taxonomy: TaxonomyPolicy | None = None,
        experiment_guard: ExperimentGuard | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._policy = policy
        self._ranking = ranking
        self._taxonomy = taxonomy
        self._guard: ExperimentGuard = experiment_guard or NoExperiments()
        self._clock = clock

    # ------------------------------------------------------------- policies
    def _policies(self) -> tuple[RecommendationPolicy, RankingPolicy, TaxonomyPolicy]:
        policy = self._policy or load_recommendation_policy(
            self._settings.recommendation_policy_path
        )
        ranking = self._ranking or resolve_ranking_policy(policy, root=REPO_ROOT)
        taxonomy = self._taxonomy or load_taxonomy(self._settings.taxonomy_path)
        return policy, ranking, taxonomy

    def _av(self) -> str:
        return analysis_version(self._policies()[2].version)

    # ------------------------------------------------------------ principal
    @staticmethod
    def _actor(principal: Principal) -> str:
        if not has_permission(principal.role, Permission.REVIEW):
            raise ReviewPermissionError
        try:
            actor = validate_actor(principal.actor)
        except ValueError:
            raise ReviewPermissionError from None
        if actor_class(actor) != "user":
            raise ReviewPermissionError
        return actor

    # ---------------------------------------------------------- public API
    async def approve(
        self, *, plan_id: uuid.UUID, request: ApproveRequest, principal: Principal
    ) -> ReviewResult:
        actor = self._actor(principal)
        meta = self._meta(ReviewAction.APPROVE, plan_id, request, principal)

        async def step(
            session: AsyncSession, row: LinkPlanRow, actor: str, now: datetime
        ) -> _Applied:
            return await self._approve(
                session,
                row,
                actor=actor,
                now=now,
                meta=meta,
                expected=request.expected_status,
                allowed=REVIEWABLE_STATUSES,
            )

        return await self._execute(
            meta, plan_id=plan_id, principal=principal, actor=actor, step=step, edit=False
        )

    async def reject(
        self, *, plan_id: uuid.UUID, request: RejectRequest, principal: Principal
    ) -> ReviewResult:
        actor = self._actor(principal)
        meta = self._meta(ReviewAction.REJECT, plan_id, request, principal)

        async def step(
            session: AsyncSession, row: LinkPlanRow, actor: str, now: datetime
        ) -> _Applied:
            return await self._reject(
                session,
                row,
                actor=actor,
                now=now,
                meta=meta,
                expected=request.expected_status,
                allowed=REJECTABLE_STATUSES,
                require_fresh=False,
            )

        return await self._execute(
            meta, plan_id=plan_id, principal=principal, actor=actor, step=step, edit=False
        )

    async def edit_anchor(
        self, *, plan_id: uuid.UUID, request: EditAnchorRequest, principal: Principal
    ) -> ReviewResult:
        actor = self._actor(principal)
        meta = self._meta(ReviewAction.EDIT_ANCHOR, plan_id, request, principal)

        async def step(
            session: AsyncSession, row: LinkPlanRow, actor: str, now: datetime
        ) -> _Applied:
            return await self._edit(
                session,
                row,
                actor=actor,
                now=now,
                meta=meta,
                expected=request.expected_status,
                approve=request.approve,
                span=(request.start, request.end),
                block_context_hash=request.block_context_hash,
                new_target_id=None,
            )

        return await self._execute(
            meta, plan_id=plan_id, principal=principal, actor=actor, step=step, edit=True
        )

    async def replace_target(
        self, *, plan_id: uuid.UUID, request: ReplaceTargetRequest, principal: Principal
    ) -> ReviewResult:
        actor = self._actor(principal)
        meta = self._meta(ReviewAction.REPLACE_TARGET, plan_id, request, principal)

        async def step(
            session: AsyncSession, row: LinkPlanRow, actor: str, now: datetime
        ) -> _Applied:
            return await self._edit(
                session,
                row,
                actor=actor,
                now=now,
                meta=meta,
                expected=request.expected_status,
                approve=request.approve,
                span=None,
                block_context_hash=None,
                new_target_id=request.target_page_id,
            )

        return await self._execute(
            meta, plan_id=plan_id, principal=principal, actor=actor, step=step, edit=True
        )

    async def bulk(self, *, request: BulkReviewRequest, principal: Principal) -> BulkReviewResult:
        actor = self._actor(principal)
        fingerprint = bulk_fingerprint(request=request, user_id=principal.user_id)
        kind = ReviewAction.APPROVE if request.action is BulkAction.APPROVE else ReviewAction.REJECT
        item_ops = {
            pid: bulk_item_operation_id(request.operation_id, pid) for pid in request.plan_ids
        }
        async with self._sessionmaker() as session:
            stored = await review_repo.actions_by_operations(
                session, operation_ids=list(item_ops.values())
            )
            same_bulk = await review_repo.actions_for_bulk(
                session, bulk_operation_id=request.operation_id
            )
        # Reusing a bulk id with another action or plan set (or user, or body) is a 409.
        if any(s.request_fingerprint != fingerprint for s in (*stored.values(), *same_bulk)):
            raise OperationReusedError

        items: list[BulkItemResult] = []
        replayed = 0
        for plan_id in request.plan_ids:
            done = stored.get(item_ops[plan_id])
            if done is not None:
                replayed += 1
                items.append(BulkItemResult(plan_id=plan_id, ok=True, status=done.to_status))
                continue
            meta = _Request(
                kind=kind,
                operation_id=item_ops[plan_id],
                fingerprint=fingerprint,
                reasons=tuple(request.reasons),
                note=request.note,
                correlation_id=request.operation_id,
                bulk_operation_id=request.operation_id,
            )
            step = self._bulk_step(kind, meta)
            try:
                result = await self._execute(
                    meta, plan_id=plan_id, principal=principal, actor=actor, step=step, edit=False
                )
            except PlanNotFoundError:
                items.append(BulkItemResult(plan_id=plan_id, ok=False, error_code="not_found"))
                continue
            except ReviewError as exc:
                items.append(BulkItemResult(plan_id=plan_id, ok=False, error_code=exc.code))
                continue
            if result.idempotent_replay:  # a concurrent replay of the same bulk won the race
                replayed += 1
            items.append(BulkItemResult(plan_id=plan_id, ok=True, status=result.status))
        return BulkReviewResult(
            operation_id=request.operation_id,
            action=request.action,
            items=items,
            idempotent_replay=replayed == len(request.plan_ids),
        )

    def _bulk_step(self, kind: ReviewAction, meta: _Request) -> _Step:
        async def step(
            session: AsyncSession, row: LinkPlanRow, actor: str, now: datetime
        ) -> _Applied:
            awaiting = frozenset({_S.AWAITING_APPROVAL})
            if LinkPlanStatus(row.status) is not _S.AWAITING_APPROVAL:
                raise StatusConflictError("not_awaiting_approval")
            if kind is ReviewAction.APPROVE:
                return await self._approve(
                    session, row, actor=actor, now=now, meta=meta, expected=None, allowed=awaiting
                )
            return await self._reject(
                session,
                row,
                actor=actor,
                now=now,
                meta=meta,
                expected=None,
                allowed=awaiting,
                require_fresh=True,
            )

        return step

    # --------------------------------------------------------- the executor
    def _meta(
        self, kind: ReviewAction, plan_id: uuid.UUID, request: Any, principal: Principal
    ) -> _Request:
        return _Request(
            kind=kind,
            operation_id=request.operation_id,
            fingerprint=request_fingerprint(
                action=kind, plan_id=plan_id, user_id=principal.user_id, body=request
            ),
            reasons=tuple(request.reasons),
            note=request.note,
            correlation_id=request.operation_id,
        )

    async def _stored(self, operation_id: uuid.UUID) -> review_repo.StoredAction | None:
        async with self._sessionmaker() as session:
            return await review_repo.action_by_operation(session, operation_id=operation_id)

    @staticmethod
    def _replayed(stored: review_repo.StoredAction, fingerprint: str) -> ReviewResult:
        if stored.request_fingerprint != fingerprint:
            raise OperationReusedError
        return ReviewResult(
            action_id=stored.id,
            operation_id=stored.operation_id,
            action=stored.action,
            plan_id=stored.plan_id,
            status=stored.to_status,
            new_plan_id=stored.new_plan_id,
            new_plan_status=stored.new_plan_status,
            idempotent_replay=True,
        )

    async def _execute(
        self,
        meta: _Request,
        *,
        plan_id: uuid.UUID,
        principal: Principal,
        actor: str,
        step: _Step,
        edit: bool,
    ) -> ReviewResult:
        stored = await self._stored(meta.operation_id)
        if stored is not None:
            return self._replayed(stored, meta.fingerprint)
        try:
            async with self._sessionmaker() as session, session.begin():
                if edit:
                    source_id = await session.scalar(
                        select(LinkPlanRow.source_page_id).where(LinkPlanRow.id == plan_id)
                    )
                    if source_id is None:
                        raise PlanNotFoundError
                    await lock_source_plans(session, source_page_id=source_id)
                row = await rec_repo.lock_plan(session, plan_id=plan_id)
                if row is None:
                    raise PlanNotFoundError
                # A concurrent duplicate committed while this request waited for the lock.
                again = await review_repo.action_by_operation(
                    session, operation_id=meta.operation_id
                )
                if again is not None:
                    raise _ReplayFoundError(again)
                now = self._clock()
                applied = await step(session, row, actor, now)
                return await self._record(session, meta, principal, actor, plan_id, applied, now)
        except _ReplayFoundError as found:
            return self._replayed(found.stored, meta.fingerprint)
        except IntegrityError as exc:
            if _OPERATION_UNIQUE in str(exc.orig):
                stored = await self._stored(meta.operation_id)
                if stored is not None:
                    return self._replayed(stored, meta.fingerprint)
            # A residual open-pair / open-span unique violation: someone else won (409).
            raise StatusConflictError("concurrent_conflict") from None

    async def _record(
        self,
        session: AsyncSession,
        meta: _Request,
        principal: Principal,
        actor: str,
        plan_id: uuid.UUID,
        applied: _Applied,
        now: datetime,
    ) -> ReviewResult:
        new_plan = applied.new_plan
        result = ReviewResult(
            action_id=0,
            operation_id=meta.operation_id,
            action=meta.kind,
            plan_id=plan_id,
            status=applied.to_status,
            new_plan_id=None if new_plan is None else new_plan.id,
            new_plan_status=applied.new_plan_status,
        )
        action_id = await review_repo.insert_action(
            session,
            action=review_repo.NewAction(
                operation_id=meta.operation_id,
                request_fingerprint=meta.fingerprint,
                action=meta.kind,
                actor=actor,
                user_id=principal.user_id,
                plan_id=plan_id,
                from_status=applied.from_status,
                to_status=applied.to_status,
                reasons=meta.reasons,
                note=meta.note,
                anchor_before=applied.anchor_before,
                target_before=applied.target_before,
                result=result.model_dump(mode="json", exclude={"action_id", "idempotent_replay"}),
                created_at=now,
                new_plan_id=None if new_plan is None else new_plan.id,
                new_plan_status=applied.new_plan_status,
                anchor_after=None if new_plan is None else new_plan.anchor_text,
                target_after=None if new_plan is None else new_plan.target_page_id,
                bulk_operation_id=meta.bulk_operation_id,
            ),
        )
        return result.model_copy(update={"action_id": action_id})

    # -------------------------------------------------------------- checks
    @staticmethod
    def _check_status(
        row: LinkPlanRow, expected: LinkPlanStatus | None, allowed: frozenset[LinkPlanStatus]
    ) -> LinkPlanStatus:
        current = LinkPlanStatus(row.status)
        if expected is not None and current is not LinkPlanStatus(expected):
            raise StatusConflictError("expected_status_mismatch", location="body.expected_status")
        if current not in allowed:
            raise StatusConflictError("status_not_reviewable")
        return current

    async def _live(
        self, session: AsyncSession, *, source_id: uuid.UUID, target_ids: Sequence[uuid.UUID]
    ) -> _Live:
        _policy, _ranking, taxonomy = self._policies()
        av = analysis_version(taxonomy.version)
        facts = await rec_repo.page_facts(
            session, analysis_version=av, page_ids=[source_id, *target_ids]
        )
        live = _Live(
            facts=facts,
            eligibility={
                pid: page_eligibility(f, taxonomy=taxonomy, analysis_version=av)
                for pid, f in facts.items()
            },
        )
        source = facts.get(source_id)
        if source is not None:
            body = await rec_repo.version_body(
                session, page_id=source.id, version=source.current_version
            )
            live.blocks = tuple(extract_blocks(body or ""))
            live.links = await rec_repo.version_links(
                session, page_id=source.id, version=source.current_version
            )
        return live

    def _freshness(self, plan: LinkPlan, live: _Live) -> FreshnessCheck:
        source = live.facts.get(plan.source_page_id)
        if source is None:
            raise InvalidatedError("source_ineligible (unknown)")
        check = check_plan_freshness(
            plan_source_version=plan.source_page_version,
            current_version=source.current_version,
            locator=plan.locator,
            anchor_text=plan.anchor_text,
            current_blocks=live.blocks,
            excluded_ancestors=self._policies()[0].placement.excluded_ancestors,
        )
        if PlanFreshness(check.freshness) is PlanFreshness.STALE:
            raise StaleError(check.reasons)
        return check

    @staticmethod
    def _validity(plan: LinkPlan, live: _Live) -> None:
        invalid = plan_invalidity(
            source_eligibility=live.eligibility.get(plan.source_page_id),
            target_eligibility=live.eligibility.get(plan.target_page_id),
            already_linked=plan.target_page_id in live.linked,
        )
        if invalid is not None:
            raise InvalidatedError(invalid)

    # ------------------------------------------------------------- actions
    def _note(self, meta: _Request) -> str:
        return f"review {meta.kind.value} op={meta.operation_id}"

    async def _approve(
        self,
        session: AsyncSession,
        row: LinkPlanRow,
        *,
        actor: str,
        now: datetime,
        meta: _Request,
        expected: LinkPlanStatus | None,
        allowed: frozenset[LinkPlanStatus],
    ) -> _Applied:
        current = self._check_status(row, expected, allowed)
        plan = rec_repo.plan_from_row(row)
        live = await self._live(
            session, source_id=plan.source_page_id, target_ids=[plan.target_page_id]
        )
        self._freshness(plan, live)
        self._validity(plan, live)
        if current is not _S.AWAITING_APPROVAL:
            await self._move(session, plan.id, _S.AWAITING_APPROVAL, actor, meta, current)
        await self._move(session, plan.id, _S.APPROVED, actor, meta, _S.AWAITING_APPROVAL)
        await self._event(session, EventName.LINK_APPROVED, plan, _S.APPROVED, actor, meta, now)
        return _Applied(
            from_status=current,
            to_status=_S.APPROVED,
            anchor_before=plan.anchor_text,
            target_before=plan.target_page_id,
        )

    async def _reject(
        self,
        session: AsyncSession,
        row: LinkPlanRow,
        *,
        actor: str,
        now: datetime,
        meta: _Request,
        expected: LinkPlanStatus | None,
        allowed: frozenset[LinkPlanStatus],
        require_fresh: bool,
    ) -> _Applied:
        current = self._check_status(row, expected, allowed)
        plan = rec_repo.plan_from_row(row)
        if require_fresh:  # bulk: awaiting_approval AND not stale only
            live = await self._live(session, source_id=plan.source_page_id, target_ids=[])
            self._freshness(plan, live)
        await self._move(session, plan.id, _S.REJECTED, actor, meta, current)
        await self._event(session, EventName.LINK_REJECTED, plan, _S.REJECTED, actor, meta, now)
        return _Applied(
            from_status=current,
            to_status=_S.REJECTED,
            anchor_before=plan.anchor_text,
            target_before=plan.target_page_id,
        )

    async def _edit(
        self,
        session: AsyncSession,
        row: LinkPlanRow,
        *,
        actor: str,
        now: datetime,
        meta: _Request,
        expected: LinkPlanStatus,
        approve: bool,
        span: tuple[int, int] | None,
        block_context_hash: str | None,
        new_target_id: uuid.UUID | None,
    ) -> _Applied:
        current = self._check_status(row, expected, EDITABLE_STATUSES)
        old = rec_repo.plan_from_row(row)
        targets = (
            [old.target_page_id] if new_target_id is None else [old.target_page_id, new_target_id]
        )
        live = await self._live(session, source_id=old.source_page_id, target_ids=targets)
        check = self._freshness(old, live)
        self._validity(old, live)
        if block_context_hash is not None and block_context_hash != old.locator.context_hash:
            raise StatusConflictError("context_hash_mismatch", location="body.block_context_hash")
        new_plan = await self._edited_plan(
            session,
            old=old,
            live=live,
            hint=check.block_id,
            span=span,
            new_target_id=new_target_id,
            now=now,
        )
        # Supersede BEFORE insert: the open-pair / open-span unique indexes are not deferrable.
        await self._move(session, old.id, _S.SUPERSEDED, actor, meta, current)
        await rec_repo.insert_plan(session, plan=new_plan)
        await record_initial(
            session,
            plan_id=new_plan.id,
            status=_S.PROPOSED,
            actor=REVIEW_SERVICE_ACTOR,
            occurred_at=now,
            note=f"edited by {actor} (supersedes {old.id})",
        )
        await self._move(session, new_plan.id, _S.AWAITING_APPROVAL, actor, meta, _S.PROPOSED)
        final = _S.AWAITING_APPROVAL
        await self._event(
            session, EventName.LINK_PROPOSED, new_plan, final, actor, meta, now, supersedes=old.id
        )
        if approve:
            await self._move(session, new_plan.id, _S.APPROVED, actor, meta, _S.AWAITING_APPROVAL)
            final = _S.APPROVED
            await self._event(session, EventName.LINK_APPROVED, new_plan, final, actor, meta, now)
        return _Applied(
            from_status=current,
            to_status=_S.SUPERSEDED,
            anchor_before=old.anchor_text,
            target_before=old.target_page_id,
            new_plan=new_plan,
            new_plan_status=final,
        )

    async def _move(
        self,
        session: AsyncSession,
        plan_id: uuid.UUID,
        to_status: LinkPlanStatus,
        actor: str,
        meta: _Request,
        expected_from: LinkPlanStatus,
    ) -> None:
        await transition(
            session,
            plan_id=plan_id,
            to_status=to_status,
            actor=actor,
            note=self._note(meta),
            expected_from=expected_from,
            clock=self._clock,
        )

    async def _event(
        self,
        session: AsyncSession,
        name: EventName,
        plan: LinkPlan,
        status: LinkPlanStatus,
        actor: str,
        meta: _Request,
        now: datetime,
        *,
        supersedes: uuid.UUID | None = None,
    ) -> None:
        payload = LinkPlanEvent(
            plan_id=plan.id,
            source_page_id=plan.source_page_id,
            source_page_version=plan.source_page_version,
            target_page_id=plan.target_page_id,
            anchor_text=plan.anchor_text,
            status=status,
            score=plan.score.total,
            run_id=plan.run_id,
            actor=actor,
            supersedes=supersedes,
        )
        await append_event(
            session,
            EventEnvelope(
                event_name=name,
                occurred_at=now,
                emitted_by=EMITTED_BY,
                subject_type=SUBJECT_LINK_PLAN,
                subject_id=str(plan.id),
                payload=payload.model_dump(mode="json"),
                correlation_id=meta.correlation_id,
            ),
        )

    # ------------------------------------------------------- edited plans
    @staticmethod
    def _current_block(
        blocks: Sequence[ContentBlock], context_hash: str, hint: str | None
    ) -> ContentBlock:
        matching = [b for b in blocks if sha256_hex(b.text) == context_hash]
        matching.sort(key=lambda b: b.block_id != hint)  # the freshness check's block first
        if not matching:  # pragma: no cover - the freshness check already failed
            raise StaleError(("block_missing",))
        return matching[0]

    def _validate_span(
        self,
        block: ContentBlock,
        start: int,
        end: int,
        *,
        other_spans: Sequence[tuple[int, int]],
    ) -> tuple[str, int]:
        """The verbatim anchor and its occurrence index, or a 422."""
        policy = self._policies()[0]
        if block.kind not in policy.placement.block_kinds:
            raise EditRejectedError("block_kind_not_allowed")
        if not 0 <= start < end <= len(block.text):
            raise EditRejectedError("span_out_of_block", location="body.end")
        anchor = block.text[start:end]
        occurrences = phrase_occurrences(anchor, block.text)
        if (start, end) not in occurrences:
            raise EditRejectedError("span_not_word_aligned", location="body.start")
        excluded = {*policy.placement.excluded_ancestors, LINK_ANCESTOR}
        if not span_is_safe(block, start, end, excluded_ancestors=excluded):
            raise EditRejectedError("span_unsafe", location="body.start")
        if any(start < o_end and o_start < end for o_start, o_end in other_spans):
            raise EditRejectedError("span_overlaps_plan", location="body.start")
        return anchor, occurrences.index((start, end))

    def _anchor_quality(
        self,
        anchor: str,
        block: ContentBlock,
        blocks: Sequence[ContentBlock],
        phrases: Sequence[AnchorPhrase],
    ) -> tuple[str, float]:
        """(rule kind, placement quality) of an edited anchor (module docstring)."""
        policy = self._policies()[0]
        key = fold(_collapse(anchor))
        matched = next((p.source for p in phrases if fold(_collapse(p.text)) == key), None)
        kind = matched or "edited"
        if not anchor_rule_ok(anchor, kind, policy=policy.placement):
            raise EditRejectedError("anchor_rule", location="body.start")
        quality = PLACEMENT_QUALITY[matched] if matched else EDITED_PLACEMENT_QUALITY
        kinds = frozenset(policy.placement.block_kinds)
        opening = next((b.block_id for b in blocks if b.kind in kinds and b.text), None)
        if block.block_id == opening:
            quality = round(quality - OPENING_BLOCK_PENALTY, 6)
        return kind, quality

    async def _pair_state(
        self,
        session: AsyncSession,
        *,
        live: _Live,
        source: rec_repo.PageFacts,
        target: rec_repo.PageFacts,
        open_targets: set[uuid.UUID],
        now: datetime,
    ) -> PairState:
        policy = self._policies()[0]
        since = now - timedelta(days=policy.selection.rejection_cooldown_days)
        rejected = await rec_repo.rejected_targets_since(
            session, source_page_id=source.id, since=since
        )
        locked = await self._guard.locked(source_page_id=source.id, target_page_id=target.id)
        return _pair_state(
            policy=policy,
            source=source,
            target=target,
            source_eligibility=live.eligibility[source.id],
            target_eligibility=live.eligibility[target.id],
            linked=live.linked,
            occupied=open_targets,
            rejected=rejected,
            experiment_locked=bool(locked),
        )

    async def _occupied(
        self, session: AsyncSession, source: rec_repo.PageFacts
    ) -> tuple[list[rec_repo.OpenPlan], set[uuid.UUID]]:
        open_plans = await rec_repo.open_plans_for_source(session, source_page_id=source.id)
        occupied = {p.target_page_id for p in open_plans} | await rec_repo.live_targets_at_or_after(
            session, source_page_id=source.id, source_version=source.current_version
        )
        return open_plans, occupied

    async def _score_new_pair(
        self,
        session: AsyncSession,
        *,
        old: LinkPlan,
        live: _Live,
        source: rec_repo.PageFacts,
        target: rec_repo.PageFacts,
    ) -> ScoredPair:
        policy, ranking, taxonomy = self._policies()
        if source.analysis is None or target.analysis is None:  # pragma: no cover
            raise EditRejectedError("pair_target_ineligible", location="body.target_page_id")
        cosine: float | None = None
        if old.embedding_model is not None:
            key = EmbeddingKey(
                model=old.embedding_model,
                recipe_fingerprint=current_recipe_fingerprint(self._settings),
            )
            cosine = (
                await cosine_to(
                    session, source_page_id=source.id, target_page_ids=[target.id], key=key
                )
            ).get(target.id)
        markets = await _linked_markets(session, live, source.id, self._av())
        market = target.analysis.market_id
        inbound = await rec_repo.inbound_counts_for(session, page_ids=[target.id])
        return score_pair(
            PairFeatures(
                cosine=cosine,
                source=source.analysis,
                target=target.analysis,
                source_links_into_target_market=0 if market is None else markets[market],
                target_inbound_links=inbound.get(target.id, 0),
            ),
            policy=policy,
            ranking=ranking,
            taxonomy=taxonomy,
            semantic_floor=self._settings.similarity_min_score,
        )

    async def _edited_plan(
        self,
        session: AsyncSession,
        *,
        old: LinkPlan,
        live: _Live,
        hint: str | None,
        span: tuple[int, int] | None,
        new_target_id: uuid.UUID | None,
        now: datetime,
    ) -> LinkPlan:
        """The superseding plan, built FRESH per ADR-0013 s7 (module docstring)."""
        policy, ranking, taxonomy = self._policies()
        source = live.facts[old.source_page_id]
        block = self._current_block(live.blocks, old.locator.context_hash, hint)
        open_plans, occupied = await self._occupied(session, source)
        other_spans = [
            (p.locator.start, p.locator.end)
            for p in open_plans
            if p.id != old.id and p.locator.context_hash == old.locator.context_hash
        ]
        if span is None:  # replace_target: the SAME span (fresh or rebased at its offsets)
            start, end = old.locator.start, old.locator.end
        else:
            start, end = span
            if (start, end) == (old.locator.start, old.locator.end):
                raise EditRejectedError("span_unchanged", location="body.start")
        anchor, occurrence = self._validate_span(block, start, end, other_spans=other_spans)

        if new_target_id is None:
            target = live.facts[old.target_page_id]
            scored_breakdown = old.score
            relationship = old.relationship_type
            channels = old.channels
            pair_codes = tuple(c for c in old.reason_codes if c not in _ANCHOR_CODES)
            embedding_model = old.embedding_model
        else:
            if new_target_id == old.target_page_id:
                raise EditRejectedError("target_unchanged", location="body.target_page_id")
            found = live.facts.get(new_target_id)
            if found is None:
                raise EditRejectedError("target_unknown", location="body.target_page_id")
            target = found
            state = await self._pair_state(
                session, live=live, source=source, target=target, open_targets=occupied, now=now
            )
            rejection = pair_rejection(state, policy=policy)
            if rejection is CandidateRejection.DUPLICATE_RECOMMENDATION:
                raise PairConflictError("pair_duplicate_recommendation")
            if rejection is not None:
                raise EditRejectedError(f"pair_{rejection.value}", location="body.target_page_id")
            scored = await self._score_new_pair(
                session, old=old, live=live, source=source, target=target
            )
            scored_breakdown = scored.breakdown
            relationship = scored.relationship_type
            channels = ()  # no channel retrieved this pair: the reviewer chose it
            pair_codes = scored.reason_codes
            embedding_model = old.embedding_model

        phrases = (
            anchor_phrases(
                target=target.analysis,
                target_title=target.title,
                taxonomy=taxonomy,
                policy=policy,
                source=source.analysis,
            )
            if target.analysis is not None
            else ()
        )
        kind, quality = self._anchor_quality(anchor, block, live.blocks, phrases)
        final = with_placement(scored_breakdown, quality)
        reason_codes = merge_reason_codes(pair_codes, anchor_reason_code(kind))
        title = _bounded_title(target.title)
        return LinkPlan(
            id=uuid.uuid4(),
            run_id=None,
            source_page_id=source.id,
            source_page_version=source.current_version,
            source_url=source.canonical_url,
            target_page_id=target.id,
            target_page_version=target.current_version,
            target_url=target.canonical_url,
            target_title=title,
            relationship_type=relationship,
            channels=channels,
            anchor_text=anchor,
            anchor_source="edited",
            locator=PlacementLocator(
                block_id=block.block_id,
                block_kind=block.kind,
                occurrence_index=occurrence,
                start=start,
                end=end,
                context_hash=sha256_hex(block.text),
            ),
            preview=preview_for(block.text, start, end),
            score=final,
            confidence=final.total,
            calibration_version=None,
            reason_codes=reason_codes,
            rationale=render_rationale(
                reason_codes=reason_codes,
                target_title=title,
                relationship_type=relationship,
                anchor_text=anchor,
            ),
            engine_version=ENGINE_VERSION,
            policy_version=policy.version,
            ranking_policy_version=ranking.version,
            embedding_model=embedding_model,
            reasoning_model=None,
            analysis_version=(
                source.analysis.analysis_version
                if source.analysis is not None
                else analysis_version(taxonomy.version)
            ),
            supersedes=old.id,
            status=_S.PROPOSED,
            created_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------ target options
    async def target_options(self, *, plan_id: uuid.UUID, limit: int) -> list[TargetOption]:
        """Eligible replacement destinations for the plan's anchor: pages passing the pair
        filters for its source over live facts (never the source, the current target or a
        page with an open plan from the source), ranked by ``score_pair`` relevance desc,
        then canonical URL, then id."""
        policy, ranking, taxonomy = self._policies()
        av = analysis_version(taxonomy.version)
        now = self._clock()
        async with self._sessionmaker() as session:
            row = (
                await session.scalars(select(LinkPlanRow).where(LinkPlanRow.id == plan_id))
            ).first()
            if row is None:
                raise PlanNotFoundError
            plan = rec_repo.plan_from_row(row)
            facts = await rec_repo.page_facts(session, analysis_version=av)
            source = facts.get(plan.source_page_id)
            if source is None or source.analysis is None:
                return []
            eligibility = {
                pid: page_eligibility(f, taxonomy=taxonomy, analysis_version=av)
                for pid, f in facts.items()
            }
            links = await rec_repo.version_links(
                session, page_id=source.id, version=source.current_version
            )
            live = _Live(facts=facts, eligibility=eligibility, links=links)
            _open, occupied = await self._occupied(session, source)
            since = now - timedelta(days=policy.selection.rejection_cooldown_days)
            rejected = await rec_repo.rejected_targets_since(
                session, source_page_id=source.id, since=since
            )
            survivors: list[rec_repo.PageFacts] = []
            for pid, target in facts.items():
                if pid in (source.id, plan.target_page_id) or target.analysis is None:
                    continue
                locked = await self._guard.locked(source_page_id=source.id, target_page_id=pid)
                state = _pair_state(
                    policy=policy,
                    source=source,
                    target=target,
                    source_eligibility=eligibility[source.id],
                    target_eligibility=eligibility[pid],
                    linked=live.linked,
                    occupied=occupied,
                    rejected=rejected,
                    experiment_locked=bool(locked),
                )
                if pair_rejection(state, policy=policy) is None:
                    survivors.append(target)
            if not survivors:
                return []
            cosines: Mapping[uuid.UUID, float] = {}
            if plan.embedding_model is not None:
                key = EmbeddingKey(
                    model=plan.embedding_model,
                    recipe_fingerprint=current_recipe_fingerprint(self._settings),
                )
                cosines = await cosine_to(
                    session,
                    source_page_id=source.id,
                    target_page_ids=[t.id for t in survivors],
                    key=key,
                )
            inbound = await rec_repo.inbound_counts_for(session, page_ids=[t.id for t in survivors])
        market_of = {
            pid: (f.analysis.market_id if f.analysis is not None else None)
            for pid, f in facts.items()
        }
        markets = Counter(
            market_of.get(link.target_page_id)
            for link in links
            if link.target_page_id is not None and link.target_page_id != source.id
        )
        scored: list[tuple[float, rec_repo.PageFacts]] = []
        for target in survivors:
            if target.analysis is None:  # pragma: no cover - filtered above
                continue
            market = target.analysis.market_id
            pair = score_pair(
                PairFeatures(
                    cosine=cosines.get(target.id),
                    source=source.analysis,
                    target=target.analysis,
                    source_links_into_target_market=0 if market is None else markets[market],
                    target_inbound_links=inbound.get(target.id, 0),
                ),
                policy=policy,
                ranking=ranking,
                taxonomy=taxonomy,
                semantic_floor=self._settings.similarity_min_score,
            )
            scored.append((pair.breakdown.relevance, target))
        scored.sort(key=lambda item: (-item[0], item[1].canonical_url, str(item[1].id)))
        return [
            TargetOption(
                page_id=target.id,
                canonical_url=target.canonical_url,
                title=_bounded_title(target.title),
                page_type=None if target.analysis is None else target.analysis.page_type,
                market_id=None if target.analysis is None else target.analysis.market_id,
                relevance=round(min(1.0, max(0.0, relevance)), 6),
            )
            for relevance, target in scored[:limit]
        ]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pair_state(
    *,
    policy: RecommendationPolicy,
    source: rec_repo.PageFacts,
    target: rec_repo.PageFacts,
    source_eligibility: LinkEligibility,
    target_eligibility: LinkEligibility,
    linked: set[uuid.UUID],
    occupied: set[uuid.UUID],
    rejected: set[uuid.UUID],
    experiment_locked: bool,
) -> PairState:
    """The engine's :class:`PairState` for one pair, from LIVE facts (as the pipeline)."""
    path = urlsplit(target.canonical_url).path or "/"
    return PairState(
        source_id=source.id,
        target_id=target.id,
        source_protected=source.protected,
        target_protected=target.protected,
        experiment_locked=experiment_locked,
        source_eligibility=source_eligibility,
        target_eligibility=target_eligibility,
        already_linked=target.id in linked,
        duplicate=target.id in occupied,
        previously_rejected=target.id in rejected,
        source_page_type=None if source.analysis is None else source.analysis.page_type,
        target_page_type=None if target.analysis is None else target.analysis.page_type,
        source_intent=None if source.analysis is None else _intent(source.analysis.intent),
        target_intent=None if target.analysis is None else _intent(target.analysis.intent),
        target_path_excluded=any(re.search(p, path) for p in policy.excluded_target_paths),
    )


async def _linked_markets(
    session: AsyncSession, live: _Live, source_id: uuid.UUID, av: str
) -> Counter[str | None]:
    """Markets of the pages the source's current version links to (``graph`` component)."""
    linked = [t for t in live.linked if t != source_id]
    facts = await rec_repo.page_facts(session, analysis_version=av, page_ids=linked)
    market_of = {
        pid: (f.analysis.market_id if f.analysis is not None else None) for pid, f in facts.items()
    }
    return Counter(
        market_of.get(link.target_page_id)
        for link in live.links
        if link.target_page_id is not None and link.target_page_id != source_id
    )
