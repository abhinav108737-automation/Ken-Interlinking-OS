"""The deterministic hard blocks: :class:`DeterministicBlocks` (directive s9, ADR-0018 s1).

THE RULE THAT MATTERS MOST. Every :class:`~ken_contracts.autonomy.BlockCode` is computed
from stored facts by :func:`evaluate_blocks` -- a pure, synchronous function with no I/O, no
clock, no randomness and **no model call**. It is evaluated FIRST, and any code it returns
short-circuits the decision to :attr:`~ken_contracts.autonomy.AutonomyDecision.BLOCK`.
Nothing downstream can clear a code: not a confidence, not a risk tier, not a critic that
answered ``accept``, not an operator's convenience, and not a database row -- the
``blocks_force_block`` CHECK on ``autonomy_decisions`` refuses the record that would claim
otherwise.

Why synchronous: a function with no ``await`` cannot smuggle in an inference call. Why pure:
the same facts must give the same codes with the model server switched off, which is what
makes the evals and the audit trail mean anything.

The import graph of this module and of :mod:`ken_services.autonomy.facts` carries no model
provider, no agent package and no HTTP inference client, and a static test asserts it
(``tests/unit/autonomy/test_block_determinism.py``). If someone later adds an "LLM safety
check" here, that test fails before the safety rule does.

Order is :class:`~ken_contracts.autonomy.BlockCode` enum order, which the
:class:`~ken_contracts.autonomy.AutonomyEvaluation` contract also requires. Codes are
ACCUMULATED rather than short-circuited inside the evaluator: an operator investigating a
block wants every reason it fired, not the first one.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.autonomy import AutonomyPolicy, BlockCode
from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.recommendations import RecommendationPolicy
from ken_core.config import Settings
from ken_services.autonomy.facts import load_plan_facts, snapshot
from ken_services.autonomy.protocol import PlanFacts
from ken_services.inventory import urls

__all__ = ["BLOCK_ORDER", "DeterministicBlocks", "evaluate_blocks"]

BLOCK_ORDER: tuple[BlockCode, ...] = tuple(BlockCode)
"""The enum order a stored ``blocks`` array must be in. Derived, never hand-written."""

_OK_STATUSES: frozenset[int] = frozenset({200, 203, 204})
"""HTTP statuses the inventory may hold for a page that resolves. Anything else recorded --
404, 410, a 5xx -- is a broken target; ``None`` means the page was never fetched over HTTP
(a CMS or file-export import), which is not evidence of breakage."""


def evaluate_blocks(facts: PlanFacts) -> tuple[BlockCode, ...]:
    """Every hard block these facts fire, in :class:`BlockCode` enum order. Pure and total.

    Each rule maps one-to-one onto a code, so the table below IS the implementation and an
    operator reading a code knows exactly which fact produced it.
    """
    fired: set[BlockCode] = set()

    # 1. The pair itself.
    if facts.source_page_id == facts.target_page_id:
        fired.add(BlockCode.SELF_LINK)
    if not facts.destination_internal:
        fired.add(BlockCode.EXTERNAL_DESTINATION)

    # 2. Protection covers EITHER side (protection is checked before anything else, so a
    #    protected page can never be reasoned past).
    if facts.source_protected or facts.target_protected:
        fired.add(BlockCode.PROTECTED_PAGE)

    # 3. The destination's publication state and reachability.
    if not facts.target_published:
        fired.add(BlockCode.UNPUBLISHED_DESTINATION)
    if facts.target_gone or (
        facts.target_http_status is not None and facts.target_http_status not in _OK_STATUSES
    ):
        fired.add(BlockCode.BROKEN_TARGET)

    # 4. The source version the plan was built against.
    if facts.source_current_version == 0 or (
        facts.source_page_version != facts.source_current_version and not facts.anchor_valid
    ):
        # The plan cannot be rebased onto the current source: PlanFreshness.STALE, which
        # `facts.anchor_valid` already folds in (a rebasable plan keeps a valid anchor).
        fired.add(BlockCode.STALE_SOURCE_VERSION)

    # 5. Duplicates and page types.
    if facts.already_linked and not facts.duplicate_allowed:
        fired.add(BlockCode.DUPLICATE_CONTEXTUAL_LINK)
    if not facts.page_types_compatible:
        fired.add(BlockCode.PROHIBITED_PAGE_TYPE)
    if not facts.destination_allowed_by_policy:
        fired.add(BlockCode.POLICY_DISALLOWED_DESTINATION)

    # 6. Canonicality, redirects and page state.
    if not facts.target_canonical_ok:
        fired.add(BlockCode.INVALID_CANONICAL)
    if not facts.target_redirect_supported:
        fired.add(BlockCode.UNSUPPORTED_REDIRECT)
    if facts.target_noindex or facts.target_thin:
        fired.add(BlockCode.INVALID_PAGE_STATE)

    # 7. The anchor, the rollback path and the vector the retrieval rested on.
    if not facts.anchor_valid:
        fired.add(BlockCode.INVALID_ANCHOR)
    if not facts.rollback_metadata_present:
        fired.add(BlockCode.MISSING_ROLLBACK_METADATA)
    if not facts.vector_fresh:
        fired.add(BlockCode.STALE_VECTOR)

    return tuple(code for code in BLOCK_ORDER if code in fired)


class DeterministicBlocks:
    """:class:`~ken_services.autonomy.protocol.DeterministicBlockEvaluator` over PostgreSQL.

    ``facts`` is the only I/O; ``evaluate`` is :func:`evaluate_blocks`. The constructor takes
    no model provider and the class has no attribute that could hold one.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: RecommendationPolicy,
        taxonomy: TaxonomyPolicy,
        autonomy_policy: AutonomyPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._policy = policy
        self._taxonomy = taxonomy
        self._autonomy_policy = autonomy_policy
        self._clock = clock

    def scope_hosts(self) -> set[str]:
        """The hosts that count as INTERNAL right now: the crawl scope plus deploy hosts."""
        raw = f"{self._settings.crawl_allowed_hosts},{self._settings.deploy_allowed_hosts}"
        hosts = {h.strip().split(":", 1)[0].lower() for h in raw.split(",") if h.strip()}
        base = self._settings.crawl_base_url
        if base:
            scope = urls.build_scope(base, [])
            # build_scope returns netlocs; the comparison is on HOSTNAMES, so a configured
            # "site.test:8080" and a stored "https://site.test/..." still match.
            hosts |= {h.split(":", 1)[0].lower() for h in scope.allowed_hosts}
        return hosts

    async def facts(self, *, plan_id: uuid.UUID) -> PlanFacts:
        """One snapshot read (:func:`~ken_services.autonomy.facts.load_plan_facts`).

        Raises:
            PlanNotFoundError: no such plan.
        """
        async with self._sessionmaker() as session:
            await snapshot(session)
            facts, _identity = await load_plan_facts(
                session,
                plan_id=plan_id,
                policy=self._policy,
                taxonomy=self._taxonomy,
                now=self._clock(),
                scope_hosts=self.scope_hosts(),
                max_redirect_hops=self._autonomy_policy.blocks.max_redirect_hops,
            )
            await session.rollback()  # reads only, always
        return facts

    def evaluate(self, facts: PlanFacts) -> tuple[BlockCode, ...]:
        return evaluate_blocks(facts)
