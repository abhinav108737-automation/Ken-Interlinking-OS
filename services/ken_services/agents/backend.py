"""The database implementation of :class:`~ken_services.agents.protocol.AgentBackend`.

Everything an agent tool may do, over LIVE state, owning every session (ADR-0012 section 7).
Nodes never see this object; they see a ``ToolBox`` built over it.

READ methods
    ``get_page`` / ``get_analysis`` / ``get_eligibility`` read the page's CURRENT version.
    ``get_analysis`` returns the current version's analysis at the CURRENT analysis version
    only (else ``None``: triage then decides ``reindex``). Eligibility is computed, never
    stored, with the Phase 2 :func:`compute_eligibility` exactly as the engine does.
    ``list_proposed_plans`` lists the source's ``proposed`` plans with no ``agent_run_plans``
    row, in engine order (total score desc, created_at, id).

``check_plan``
    The deterministic invariants of one plan, in this order (stable codes):

    ``not_proposed``            the plan is not ``proposed`` (or does not exist);
    ``source_version_changed``  the source changed and the plan is STALE
                                (:func:`check_plan_freshness`; ``rebasable`` is fine);
    ``anchor_not_verbatim``     the source is unchanged but no block with the plan's
                                ``context_hash`` has the anchor at ``[start:end]``;
    ``source_ineligible``       the source may not be a link source NOW (any reason,
                                durable or not: lag also blocks a hand-off);
    ``target_ineligible``       the same for the target (or the target is unknown);
    ``already_linked``          the source's current version already links to the target.

``hand_off`` (the policy gate; the ONLY writing method)
    ONE transaction. The run must exist and be ``running``, and every reviewed plan must
    belong to the run's source page, else :class:`ValueError` and nothing is written. Then,
    for each review, under its own SAVEPOINT:

    1. the plan is locked ``FOR UPDATE``; not ``proposed`` -> conflict ``not_proposed``;
    2. ``accept`` / ``uncertain``: :meth:`check_plan`'s invariants are RE-CHECKED inside the
       transaction (defence in depth against a change since the tool call); a problem ->
       conflict with that problem's code; else ``proposed -> critiqued`` (``CRITIC_ACTOR``)
       ``-> policy_checked -> awaiting_approval`` (``POLICY_GATE_ACTOR``) through the Phase 3
       transition service with ``expected_from``;
    3. the append-only ``agent_run_plans`` row (``handed_off`` true for 2, false for
       ``reject``, which is never transitioned).

    A conflicted plan's savepoint is rolled back: it gets no row and no transition (a later
    run may review it again). ``already_reviewed`` is reported if another run recorded the
    plan first (UNIQUE ``plan_id``). Actors are bound here; there is no edge to ``approved``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import (
    CRITIC_ACTOR,
    MAX_EXPLANATION_CHARS,
    POLICY_GATE_ACTOR,
    CriticReason,
    CriticVerdict,
    FallbackReason,
)
from ken_contracts.intelligence import (
    LinkEligibility,
    PageAnalysis,
    TaxonomyPolicy,
    analysis_version,
)
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_contracts.recommendations import (
    LinkPlan,
    LinkPlanStatus,
    PlanFreshness,
    RecommendationPolicy,
)
from ken_core.config import Settings
from ken_core.db.models.inventory import Page
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import agents as agent_repo
from ken_core.db.repositories import recommendations as rec_repo
from ken_services.agents.protocol import (
    HandOffResult,
    PageSnapshot,
    PlanCheck,
    PlanReviewInput,
)
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.jobs.runner import utc_now
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.policy import load_recommendation_policy
from ken_services.recommendations.transitions import IllegalTransitionError, transition

__all__ = [
    "ALREADY_REVIEWED",
    "MAX_LIST_PLANS",
    "PROBLEM_CODES",
    "DatabaseAgentBackend",
]

NOT_PROPOSED: Final = "not_proposed"
SOURCE_VERSION_CHANGED: Final = "source_version_changed"
ANCHOR_NOT_VERBATIM: Final = "anchor_not_verbatim"
SOURCE_INELIGIBLE: Final = "source_ineligible"
TARGET_INELIGIBLE: Final = "target_ineligible"
ALREADY_LINKED: Final = "already_linked"
PROBLEM_CODES: Final = (
    NOT_PROPOSED,
    SOURCE_VERSION_CHANGED,
    ANCHOR_NOT_VERBATIM,
    SOURCE_INELIGIBLE,
    TARGET_INELIGIBLE,
    ALREADY_LINKED,
)
"""``check_plan`` problem codes, in the order they are reported."""
ALREADY_REVIEWED: Final = "already_reviewed"
"""Hand-off conflict: another run recorded a review of this plan first."""
MAX_LIST_PLANS: Final = 100
MAX_REVIEW_REASONS: Final = 4
_HANDED_OFF: Final = frozenset({CriticVerdict.ACCEPT, CriticVerdict.UNCERTAIN})
_PROPOSED: Final = LinkPlanStatus.PROPOSED


def _eligibility(
    facts: rec_repo.PageFacts, *, taxonomy: TaxonomyPolicy, av: str
) -> LinkEligibility:
    """The Phase 2 eligibility of a page NOW (the engine's computation, ADR-0011)."""
    analysis = facts.analysis
    return compute_eligibility(
        EligibilityState(
            publication_state=PublicationState(facts.publication_state),
            gone=facts.gone,
            protected=facts.protected,
            robots_directives=facts.robots_directives,
            canonical_status=CanonicalStatus(facts.canonical_status or CanonicalStatus.SELF.value),
            http_status=facts.http_status,
            word_count=facts.word_count,
            page_type=None if analysis is None else analysis.page_type,
            has_analysis=facts.has_any_analysis,
            analysis_fresh=analysis is not None and analysis.analysis_version == av,
        ),
        taxonomy=taxonomy,
    )


def _validated(reviews: Sequence[PlanReviewInput]) -> list[PlanReviewInput]:
    """Coerce and bound every review BEFORE anything is written (ValueError otherwise)."""
    out: list[PlanReviewInput] = []
    seen: set[uuid.UUID] = set()
    for review in reviews:
        if not isinstance(review, PlanReviewInput):
            raise TypeError("reviews must be PlanReviewInput")
        if review.plan_id in seen:
            raise ValueError("a plan may be reviewed at most once per hand-off")
        seen.add(review.plan_id)
        reasons = tuple(CriticReason(r) for r in review.reasons)
        if len(reasons) > MAX_REVIEW_REASONS or len(set(reasons)) != len(reasons):
            raise ValueError(f"at most {MAX_REVIEW_REASONS} distinct reasons per review")
        if len(review.explanation) > MAX_EXPLANATION_CHARS:
            raise ValueError(f"explanation must be at most {MAX_EXPLANATION_CHARS} characters")
        out.append(
            PlanReviewInput(
                plan_id=review.plan_id,
                verdict=CriticVerdict(review.verdict),
                reasons=reasons,
                explanation=review.explanation,
                fallback_reason=None
                if review.fallback_reason is None
                else FallbackReason(review.fallback_reason),
                model=review.model,
            )
        )
    return out


class DatabaseAgentBackend:
    """``AgentBackend`` over PostgreSQL (module docstring).

    ``taxonomy`` / ``recommendation_policy`` default to the configured policy files;
    ``clock`` stamps the transitions (injectable for tests).
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        taxonomy: TaxonomyPolicy | None = None,
        recommendation_policy: RecommendationPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._taxonomy = taxonomy if taxonomy is not None else load_taxonomy(settings.taxonomy_path)
        self._policy = (
            recommendation_policy
            if recommendation_policy is not None
            else load_recommendation_policy(settings.recommendation_policy_path)
        )
        self._av = analysis_version(self._taxonomy.version)
        self._clock = clock

    # ------------------------------------------------------------------ reads
    async def get_page(self, *, page_id: uuid.UUID) -> PageSnapshot | None:
        async with self._sessionmaker() as session:
            page = await session.get(Page, page_id)
        if page is None:
            return None
        return PageSnapshot(
            id=page.id,
            canonical_url=page.canonical_url,
            title=page.title,
            current_version=page.current_version,
            publication_state=page.publication_state,
            gone=page.gone_at is not None,
            word_count=page.word_count,
            internal_link_count=page.internal_link_count,
        )

    async def _facts(self, session: AsyncSession, page_id: uuid.UUID) -> rec_repo.PageFacts | None:
        facts = await rec_repo.page_facts(session, analysis_version=self._av, page_ids=[page_id])
        return facts.get(page_id)

    async def get_analysis(self, *, page_id: uuid.UUID) -> PageAnalysis | None:
        async with self._sessionmaker() as session:
            facts = await self._facts(session, page_id)
        if facts is None or facts.analysis is None:
            return None
        if facts.analysis.analysis_version != self._av:
            return None  # stale: only the CURRENT analysis version counts
        return facts.analysis

    async def get_eligibility(self, *, page_id: uuid.UUID) -> LinkEligibility | None:
        async with self._sessionmaker() as session:
            facts = await self._facts(session, page_id)
        if facts is None:
            return None
        return _eligibility(facts, taxonomy=self._taxonomy, av=self._av)

    async def list_proposed_plans(self, *, source_page_id: uuid.UUID, limit: int) -> list[LinkPlan]:
        if not 1 <= limit <= MAX_LIST_PLANS:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_PLANS}")
        async with self._sessionmaker() as session:
            rows = await agent_repo.unreviewed_proposed_plans(
                session, source_page_id=source_page_id, limit=limit
            )
        return [rec_repo.plan_from_row(r) for r in rows]

    # ------------------------------------------------------------- invariants
    async def check_plan(self, *, plan_id: uuid.UUID) -> PlanCheck:
        async with self._sessionmaker() as session:
            row = await session.get(LinkPlanRow, plan_id)
            if row is None:
                return PlanCheck(ok=False, problems=(NOT_PROPOSED,))
            problems = await self._problems(session, row)
        return PlanCheck(ok=not problems, problems=problems)

    async def _problems(
        self, session: AsyncSession, row: LinkPlanRow, *, include_status: bool = True
    ) -> tuple[str, ...]:
        found: set[str] = set()
        if include_status and LinkPlanStatus(row.status) is not _PROPOSED:
            found.add(NOT_PROPOSED)
        facts = await rec_repo.page_facts(
            session,
            analysis_version=self._av,
            page_ids=[row.source_page_id, row.target_page_id],
        )
        source = facts.get(row.source_page_id)
        target = facts.get(row.target_page_id)
        if source is None:  # pragma: no cover - the plan's FK pins the source page
            return tuple(c for c in PROBLEM_CODES if c in found | {SOURCE_INELIGIBLE})
        plan = rec_repo.plan_from_row(row)
        body = await rec_repo.version_body(
            session, page_id=source.id, version=source.current_version
        )
        blocks = extract_blocks(body or "")
        if plan.source_page_version == source.current_version:
            locator = plan.locator
            verbatim = any(
                sha256_hex(b.text) == locator.context_hash
                and b.text[locator.start : locator.end] == plan.anchor_text
                for b in blocks
            )
            if not verbatim:
                found.add(ANCHOR_NOT_VERBATIM)
        else:
            freshness = check_plan_freshness(
                plan_source_version=plan.source_page_version,
                current_version=source.current_version,
                locator=plan.locator,
                anchor_text=plan.anchor_text,
                current_blocks=blocks,
                excluded_ancestors=self._policy.placement.excluded_ancestors,
            )
            if PlanFreshness(freshness.freshness) is PlanFreshness.STALE:
                found.add(SOURCE_VERSION_CHANGED)
        if not _eligibility(source, taxonomy=self._taxonomy, av=self._av).as_source:
            found.add(SOURCE_INELIGIBLE)
        if (
            target is None
            or not _eligibility(target, taxonomy=self._taxonomy, av=self._av).as_target
        ):
            found.add(TARGET_INELIGIBLE)
        links = await rec_repo.version_links(
            session, page_id=source.id, version=source.current_version
        )
        if any(link.target_page_id == row.target_page_id for link in links):
            found.add(ALREADY_LINKED)
        return tuple(c for c in PROBLEM_CODES if c in found)

    # ------------------------------------------------------------ policy gate
    async def hand_off(
        self,
        *,
        agent_run_id: uuid.UUID,
        reviews: Sequence[PlanReviewInput],
        reviewed_at: datetime,
    ) -> HandOffResult:
        checked = _validated(reviews)
        handed_off: list[uuid.UUID] = []
        recorded: list[uuid.UUID] = []
        conflicts: dict[uuid.UUID, str] = {}
        async with self._sessionmaker() as session, session.begin():
            run = await agent_repo.get_run(session, run_id=agent_run_id, lock=True)
            if run is None:
                raise ValueError("unknown agent run")
            if run.status != "running":
                raise ValueError("the agent run has already finished")
            if checked:
                owners = dict(
                    (
                        await session.execute(
                            select(LinkPlanRow.id, LinkPlanRow.source_page_id).where(
                                LinkPlanRow.id.in_([r.plan_id for r in checked])
                            )
                        )
                    ).all()
                )
                if any(owners.get(r.plan_id) != run.source_page_id for r in checked):
                    raise ValueError("every reviewed plan must belong to the run's source page")
            for review in checked:
                savepoint = await session.begin_nested()
                try:
                    conflict = await self._hand_off_one(session, run.id, review, reviewed_at)
                except IllegalTransitionError:
                    conflict = NOT_PROPOSED
                except IntegrityError:
                    conflict = ALREADY_REVIEWED
                if conflict is not None:
                    await savepoint.rollback()
                    conflicts[review.plan_id] = conflict
                    continue
                await savepoint.commit()
                recorded.append(review.plan_id)
                if review.verdict in _HANDED_OFF:
                    handed_off.append(review.plan_id)
        return HandOffResult(
            handed_off=tuple(handed_off), recorded=tuple(recorded), conflicts=conflicts
        )

    async def _hand_off_one(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        review: PlanReviewInput,
        reviewed_at: datetime,
    ) -> str | None:
        """One plan, inside its savepoint. A conflict code, or None when recorded."""
        row = await rec_repo.lock_plan(session, plan_id=review.plan_id)
        if row is None or LinkPlanStatus(row.status) is not _PROPOSED:
            return NOT_PROPOSED
        verdict = CriticVerdict(review.verdict)
        hand = verdict in _HANDED_OFF
        if hand:
            problems = await self._problems(session, row, include_status=False)
            if problems:
                return problems[0]
            note = f"agent review: {verdict.value}"
            steps = (
                (LinkPlanStatus.CRITIQUED, CRITIC_ACTOR, _PROPOSED),
                (LinkPlanStatus.POLICY_CHECKED, POLICY_GATE_ACTOR, LinkPlanStatus.CRITIQUED),
                (
                    LinkPlanStatus.AWAITING_APPROVAL,
                    POLICY_GATE_ACTOR,
                    LinkPlanStatus.POLICY_CHECKED,
                ),
            )
            for to_status, actor, expected in steps:
                await transition(
                    session,
                    plan_id=review.plan_id,
                    to_status=to_status,
                    actor=actor,
                    note=note,
                    expected_from=expected,
                    clock=self._clock,
                )
        await agent_repo.insert_review(
            session,
            run_id=run_id,
            plan_id=review.plan_id,
            verdict=verdict,
            reasons=review.reasons,
            explanation=review.explanation,
            handed_off=hand,
            fallback_reason=review.fallback_reason,
            model=review.model,
            reviewed_at=reviewed_at,
        )
        return None
