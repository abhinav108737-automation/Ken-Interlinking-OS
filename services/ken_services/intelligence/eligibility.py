"""Read-time link eligibility (ADR-0010 section 5).

Eligibility is never stored: callers build an :class:`EligibilityState` from the
page's CURRENT facts and call :func:`compute_eligibility`. Phase 3 filters link
candidates with this same function.

Reasons are listed in :class:`~ken_contracts.intelligence.IneligibilityReason`
declaration order, each at most once:

==================  ==============================================================
not_published       ``publication_state`` is not ``published``
gone                the page is gone (``gone_at`` set)
protected           any active protection (policy, CMS or manual)
noindex             robots directives contain ``noindex`` or ``none``
not_canonical       canonical status is ``declared`` (an alias of another URL)
http_not_ok         an HTTP status is known and is not 200
thin_content        word count below ``min_words_source`` / ``min_words_target``
page_type_excluded  the page type's rule has ``linkable_as_source/target: false``
not_analyzed        no analysis of the current version exists
stale_analysis      an analysis exists, but not for the current analysis version
==================  ==============================================================

``page_type_excluded`` is evaluated only for a page type known to the policy:
an unknown or missing type comes from a missing or stale analysis, which is
already a reason. ``as_source`` / ``as_target`` are true iff their list is empty.
"""

from __future__ import annotations

from dataclasses import dataclass

from ken_contracts.intelligence import (
    IneligibilityReason,
    LinkEligibility,
    PageTypeRule,
    TaxonomyPolicy,
)
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_services.intelligence.taxonomy import taxonomy_index

__all__ = ["EligibilityState", "compute_eligibility"]

_NOINDEX_DIRECTIVES = frozenset({"noindex", "none"})


@dataclass(frozen=True, slots=True)
class EligibilityState:
    publication_state: PublicationState
    gone: bool
    protected: bool
    robots_directives: tuple[str, ...]
    canonical_status: CanonicalStatus
    http_status: int | None
    word_count: int
    page_type: str | None
    has_analysis: bool
    analysis_fresh: bool


def _reasons(
    state: EligibilityState, *, rule: PageTypeRule | None, min_words: int, as_source: bool
) -> list[IneligibilityReason]:
    reasons: list[IneligibilityReason] = []
    if state.publication_state is not PublicationState.PUBLISHED:
        reasons.append(IneligibilityReason.NOT_PUBLISHED)
    if state.gone:
        reasons.append(IneligibilityReason.GONE)
    if state.protected:
        reasons.append(IneligibilityReason.PROTECTED)
    if any(d.strip().lower() in _NOINDEX_DIRECTIVES for d in state.robots_directives):
        reasons.append(IneligibilityReason.NOINDEX)
    if state.canonical_status is CanonicalStatus.DECLARED:
        reasons.append(IneligibilityReason.NOT_CANONICAL)
    if state.http_status is not None and state.http_status != 200:
        reasons.append(IneligibilityReason.HTTP_NOT_OK)
    if state.word_count < min_words:
        reasons.append(IneligibilityReason.THIN_CONTENT)
    if rule is not None and not (rule.linkable_as_source if as_source else rule.linkable_as_target):
        reasons.append(IneligibilityReason.PAGE_TYPE_EXCLUDED)
    if not state.has_analysis:
        reasons.append(IneligibilityReason.NOT_ANALYZED)
    elif not state.analysis_fresh:
        reasons.append(IneligibilityReason.STALE_ANALYSIS)
    return reasons


def compute_eligibility(state: EligibilityState, *, taxonomy: TaxonomyPolicy) -> LinkEligibility:
    """Whether the page may be a link source / target now, with every reason why not."""
    index = taxonomy_index(taxonomy)
    rule = (
        index.page_type(state.page_type)
        if state.page_type is not None and index.has_page_type(state.page_type)
        else None
    )
    thresholds = taxonomy.eligibility
    source_reasons = _reasons(
        state, rule=rule, min_words=thresholds.min_words_source, as_source=True
    )
    target_reasons = _reasons(
        state, rule=rule, min_words=thresholds.min_words_target, as_source=False
    )
    return LinkEligibility(
        as_source=not source_reasons,
        as_target=not target_reasons,
        source_reasons=source_reasons,
        target_reasons=target_reasons,
    )
