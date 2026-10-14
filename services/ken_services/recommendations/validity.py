"""Whether an OPEN plan may still stand: the engine's DURABLE-invalidity rules (ADR-0011 s1,
ADR-0013 s7), in ONE place.

Used by the recommendation pipeline (expire invalid open plans before generating) and by the
review service (re-validate under the plan's row lock before an approval or an edit). It is
never duplicated: both call :func:`plan_invalidity` over :func:`page_eligibility`.

* :func:`page_eligibility` -- the Phase 2 :func:`compute_eligibility` of a page's LIVE facts
  (``analysis_fresh`` = the current version's analysis is at the current analysis version).
* :func:`plan_invalidity` -- why a plan may no longer stand, or ``None``. Only DURABLE
  reasons count (:data:`DURABLE_INELIGIBILITY`): protected, gone, unpublished, noindex,
  canonical alias, thin, excluded page type -- on either side -- or the pair is now already
  linked in the source's current version. Analysis lag (``not_analyzed``,
  ``stale_analysis``) and ``http_not_ok`` (possibly a transient 5xx) do not: expiry is
  terminal and must not destroy an approval over an operational ordering slip.
* :func:`source_lock_key` / :func:`lock_source_plans` -- the per-source writer lock
  (``pg_advisory_xact_lock``) that serialises the pipeline's per-source transaction and
  review edits on the same source.

Pure except :func:`lock_source_plans`, which executes one statement in the caller's
transaction.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.intelligence import IneligibilityReason, LinkEligibility, TaxonomyPolicy
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_core.db.repositories.recommendations import PageFacts
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility

__all__ = [
    "DURABLE_INELIGIBILITY",
    "SOURCE_LOCK_PREFIX",
    "lock_source_plans",
    "page_eligibility",
    "plan_invalidity",
    "source_lock_key",
]

DURABLE_INELIGIBILITY: Final = frozenset(
    {
        IneligibilityReason.NOT_PUBLISHED,
        IneligibilityReason.GONE,
        IneligibilityReason.PROTECTED,
        IneligibilityReason.NOINDEX,
        IneligibilityReason.NOT_CANONICAL,
        IneligibilityReason.THIN_CONTENT,
        IneligibilityReason.PAGE_TYPE_EXCLUDED,
    }
)
"""Ineligibility reasons that expire an OPEN plan. ``not_analyzed``, ``stale_analysis``
(analysis lag) and ``http_not_ok`` (possibly a transient 5xx) do not: they only stop new
plans being generated."""
SOURCE_LOCK_PREFIX: Final = "ken.plans.source:"


def page_eligibility(
    facts: PageFacts, *, taxonomy: TaxonomyPolicy, analysis_version: str
) -> LinkEligibility:
    """The Phase 2 eligibility of a page NOW, from its live facts (the engine's computation)."""
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
            analysis_fresh=analysis is not None and analysis.analysis_version == analysis_version,
        ),
        taxonomy=taxonomy,
    )


def plan_invalidity(
    *,
    source_eligibility: LinkEligibility | None,
    target_eligibility: LinkEligibility | None,
    already_linked: bool,
) -> str | None:
    """Why an OPEN plan may no longer stand, whatever its freshness (fail closed: a plan must
    never wait in the review queue -- or be approved -- for a protected, gone or unlinkable
    page). ``None`` = still valid. An eligibility of ``None`` is a page that no longer exists
    in the inventory.

    The text is fixed vocabulary: ``source_ineligible (<reasons>)``,
    ``target_ineligible (<reasons>)`` (``unknown`` for a missing page) or ``already_linked``.
    """
    if source_eligibility is None:
        return "source_ineligible (unknown)"
    source_reasons = DURABLE_INELIGIBILITY.intersection(source_eligibility.source_reasons)
    if source_reasons:
        return f"source_ineligible ({', '.join(sorted(r.value for r in source_reasons))})"
    if target_eligibility is None:
        return "target_ineligible (unknown)"
    target_reasons = DURABLE_INELIGIBILITY.intersection(target_eligibility.target_reasons)
    if target_reasons:
        return f"target_ineligible ({', '.join(sorted(r.value for r in target_reasons))})"
    if already_linked:
        return "already_linked"
    return None


def source_lock_key(source_page_id: uuid.UUID) -> int:
    """``pg_advisory_xact_lock`` key: first 8 bytes of sha256("ken.plans.source:" + id), signed."""
    digest = hashlib.sha256((SOURCE_LOCK_PREFIX + str(source_page_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def lock_source_plans(session: AsyncSession, *, source_page_id: uuid.UUID) -> None:
    """Take the per-source plan-writer lock until the caller's transaction ends.

    Every writer that inserts or supersedes plans of a source holds it (the pipeline's
    per-source transaction, review edits), so their open-pair / open-span reads and inserts
    never interleave. Lock ORDER: this lock first, then plan row locks.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": source_lock_key(source_page_id)}
    )
