"""Assembling :class:`~ken_services.autonomy.protocol.PlanFacts` from stored facts.

**No model provider is imported here, and none may ever be.** This module and
:mod:`ken_services.autonomy.blocks` are the deterministic layer: everything they answer is
read from the inventory, the analyses, the protections, the plan, the stored CMS-side page
state and the embedding rows, by SQL and pure Python. A static test
(``tests/unit/autonomy/test_block_determinism.py``) walks the import graph of both modules
and fails if a model provider, an agent package or an inference client appears anywhere in
it, so "deterministic" is a property of the build and not a sentence in a docstring.

One read, one snapshot. :func:`load_plan_facts` runs its statements inside the caller's
session, which the engine opens as a REPEATABLE READ transaction, because a decision assembled
from several READ COMMITTED snapshots can block a plan for a source version it no longer has
and publish one whose target has just been unpublished.

What each field means in terms of stored rows is documented beside it below; the rules that
turn them into :class:`~ken_contracts.autonomy.BlockCode` values live in
:mod:`ken_services.autonomy.blocks`, and nowhere else.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.intelligence import IneligibilityReason, TaxonomyPolicy, analysis_version
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_contracts.recommendations import (
    PlanFreshness,
    PlanNotFoundError,
    RecommendationPolicy,
    RetrievalChannel,
)
from ken_core.db.models.intelligence import PageEmbedding, PageProtection
from ken_core.db.models.inventory import KnownUrl, PageVersion
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import recommendations as rec_repo
from ken_services.autonomy.protocol import PlanFacts
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.filters import intent_compatibility
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.placement import anchor_rule_ok
from ken_services.recommendations.validity import page_eligibility

__all__ = ["PlanIdentity", "load_plan_facts", "plan_identity", "snapshot"]

_NOINDEX: Final = "noindex"
_CANONICAL_OK: Final[frozenset[CanonicalStatus]] = frozenset(
    {CanonicalStatus.SELF, CanonicalStatus.MISSING}
)
"""Canonical statuses a link TARGET may have. ``self`` declared its own URL; ``missing``
declared none, so the normalised final URL IS its identity (Phase 1) -- both are authoritative
for their identity URL. ``declared`` is an ALIAS of another page (the ``not_canonical``
ineligibility reason), and ``out_of_scope`` / ``invalid`` declared a canonical we had to
ignore, so none of the three may receive a contextual link: the href would point at a URL
that is not the destination's identity."""


@dataclass(frozen=True, slots=True)
class PlanIdentity:
    """The parts of a plan the engine needs beside :class:`PlanFacts`.

    Kept separate on purpose: :class:`PlanFacts` is the frozen deterministic input and must
    not grow model-shaped fields, while the engine also needs the decision class (for
    calibration), the pair (for the retry budget) and the stored critic verdict.
    """

    plan_id: uuid.UUID
    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    status: str
    anchor_source: str
    relationship_type: str | None
    channels: tuple[str, ...]
    source_url: str
    target_url: str
    anchor_text: str
    reasoning_model: str | None


async def plan_identity(session: AsyncSession, *, plan_id: uuid.UUID) -> PlanIdentity:
    row = await session.get(LinkPlanRow, plan_id)
    if row is None:
        raise PlanNotFoundError
    return PlanIdentity(
        plan_id=row.id,
        source_page_id=row.source_page_id,
        target_page_id=row.target_page_id,
        status=row.status,
        anchor_source=row.anchor_source,
        relationship_type=row.relationship_type,
        channels=tuple(str(channel) for channel in row.channels),
        source_url=row.source_url,
        target_url=row.target_url,
        anchor_text=row.anchor_text,
        reasoning_model=row.reasoning_model,
    )


async def snapshot(session: AsyncSession) -> None:
    """Put the caller's (read-only) transaction on a REPEATABLE READ snapshot.

    Must be the FIRST statement of the transaction, which is why the callers invoke it
    immediately after opening the session. A decision assembled from several READ COMMITTED
    snapshots can block a plan for a source version it no longer has, and publish one whose
    destination was unpublished between two of its own reads.
    """
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


async def _version_row(
    session: AsyncSession, *, page_id: uuid.UUID, version: int
) -> PageVersion | None:
    return (
        await session.scalars(
            select(PageVersion).where(
                PageVersion.page_id == page_id, PageVersion.version == version
            )
        )
    ).first()


async def _protected(session: AsyncSession, *, page_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(PageProtection.page_id == page_id, PageProtection.active.is_(True))
            )
        )
    )


async def _embedding_present(
    session: AsyncSession, *, page_id: uuid.UUID, version: int, model: str | None
) -> bool:
    """Is there an embedding row for exactly this page VERSION (and model, when known)?

    ADR-0017 s4: a hit is stale when its ``page_version_id`` is not the page's
    ``current_version``. Checked here against the inventory rather than against the vector
    store, because the question is whether the retrieval that produced this plan still rests
    on a current vector -- and the answer must be the same with Qdrant switched off.
    """
    statement = select(
        exists().where(PageEmbedding.page_id == page_id, PageEmbedding.page_version == version)
    )
    if model:
        statement = select(
            exists().where(
                PageEmbedding.page_id == page_id,
                PageEmbedding.page_version == version,
                PageEmbedding.model == model,
            )
        )
    return bool(await session.scalar(statement))


async def _already_linked(
    session: AsyncSession, *, source_page_id: uuid.UUID, source_version: int, target: uuid.UUID
) -> bool:
    """Does the source's CURRENT version already link to the target (aliases included)?"""
    links = await rec_repo.version_links(session, page_id=source_page_id, version=source_version)
    return any(link.target_page_id == target for link in links)


async def _internal(
    session: AsyncSession, *, target_page_id: uuid.UUID, scope_hosts: set[str]
) -> bool:
    """Is the destination an in-scope internal page of the inventory?

    A page that is in ``pages`` is internal by construction (the crawler and the importers
    only admit in-scope URLs), but a configured host list can shrink afterwards, so the
    destination's own known URLs are checked against the CURRENT scope too.
    """
    urls = (
        await session.scalars(select(KnownUrl.url).where(KnownUrl.page_id == target_page_id))
    ).all()
    if not urls:
        return False
    if not scope_hosts:
        return True
    return any(_host_of(url) in scope_hosts for url in urls)


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _path_excluded(url: str, policy: RecommendationPolicy) -> bool:
    path = urlsplit(url).path or "/"
    return any(re.search(pattern, path) for pattern in policy.excluded_target_paths)


async def load_plan_facts(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    policy: RecommendationPolicy,
    taxonomy: TaxonomyPolicy,
    now: datetime,
    scope_hosts: set[str],
    max_redirect_hops: int,
) -> tuple[PlanFacts, PlanIdentity]:
    """Every deterministic input of one plan's decision, read in the caller's transaction.

    Raises:
        PlanNotFoundError: no such plan. Everything else is reported as a FACT, never as an
            exception: the block evaluator must be total, so a plan whose source page has
            vanished from the inventory yields facts that block it rather than a crash that
            aborts a publish run.
    """
    row = await session.get(LinkPlanRow, plan_id)
    if row is None:
        raise PlanNotFoundError
    identity = await plan_identity(session, plan_id=plan_id)
    av = analysis_version(taxonomy.version)
    page_facts = await rec_repo.page_facts(
        session, analysis_version=av, page_ids=[row.source_page_id, row.target_page_id]
    )
    source = page_facts.get(row.source_page_id)
    target = page_facts.get(row.target_page_id)

    source_current = source.current_version if source is not None else 0
    target_current = target.current_version if target is not None else 0
    target_version_row = (
        None
        if target is None
        else await _version_row(session, page_id=row.target_page_id, version=target_current)
    )

    eligibility = {
        page_id: page_eligibility(facts, taxonomy=taxonomy, analysis_version=av)
        for page_id, facts in page_facts.items()
    }
    source_type = None if source is None or source.analysis is None else source.analysis.page_type
    target_type = None if target is None or target.analysis is None else target.analysis.page_type
    source_intent = None if source is None or source.analysis is None else source.analysis.intent
    target_intent = None if target is None or target.analysis is None else target.analysis.intent
    allowed_targets = policy.page_type_targets.get(source_type or "", ())
    types_compatible = bool(
        source_type is not None
        and target_type is not None
        and target_type in allowed_targets
        and intent_compatibility(source_intent, target_intent, policy=policy) > 0.0
    )

    # The anchor must still be a verbatim, placement-safe span of the CURRENT block text and
    # must satisfy the anchor rules of the policy it was written under.
    body = (
        None
        if source is None
        else await rec_repo.version_body(
            session, page_id=row.source_page_id, version=source_current
        )
    )
    blocks = tuple(extract_blocks(body or ""))
    freshness = check_plan_freshness(
        plan_source_version=row.source_page_version,
        current_version=source_current,
        locator=rec_repo.plan_from_row(row).locator,
        anchor_text=row.anchor_text,
        current_blocks=blocks,
        excluded_ancestors=policy.placement.excluded_ancestors,
    )
    anchor_kind = "target_topic" if row.anchor_source == "edited" else row.anchor_source
    # The span must still be VERBATIM in a block that hashes to the plan's context hash, in a
    # block kind the placement policy allows. Checked independently of freshness: a plan on
    # the current source version whose anchor does not actually occur at [start, end) is a
    # fabricated anchor, and `check_plan_freshness` short-circuits to FRESH before it would
    # notice.
    verbatim = any(
        block.kind in policy.placement.block_kinds
        and block.text[row.start_offset : row.end_offset] == row.anchor_text
        for block in blocks
        if sha256_hex(block.text) == row.context_hash
    )
    anchor_valid = bool(
        row.anchor_text.strip()
        and anchor_rule_ok(row.anchor_text, anchor_kind, policy=policy.placement)
        and PlanFreshness(freshness.freshness) is not PlanFreshness.STALE
        and verbatim
    )

    # Policy exclusions on the destination: excluded target paths, and the cool-down after a
    # reviewer rejected or replaced this destination for this source.
    cooldown_since = now - timedelta(days=policy.selection.rejection_cooldown_days)
    rejected = await rec_repo.rejected_targets_since(
        session, source_page_id=row.source_page_id, since=cooldown_since
    )
    target_eligible = eligibility.get(row.target_page_id)
    destination_allowed = bool(
        target is not None
        and row.target_page_id not in rejected
        and not _path_excluded(target.canonical_url, policy)
        and target_eligible is not None
        and target_eligible.as_target
    )

    already_linked = (
        False
        if source is None
        else await _already_linked(
            session,
            source_page_id=row.source_page_id,
            source_version=source_current,
            target=row.target_page_id,
        )
    )

    # Rollback availability: everything an inverse splice needs, from stored facts only. The
    # CMS page mapping is re-checked at VALIDATE time (``cms_page_unmapped``), which refuses
    # the deployment before any mutation, so nothing is published that cannot be undone.
    rollback_ready = bool(
        target is not None
        and target.canonical_url
        and row.anchor_text.strip()
        and row.end_offset > row.start_offset
        and re.fullmatch(r"[0-9a-f]{64}", row.context_hash or "")
        and body is not None
    )

    # Vector freshness: only a plan that was RETRIEVED by the embedding channel rests on a
    # vector, and then both sides must have an embedding at their CURRENT version.
    used_embeddings = RetrievalChannel.SEMANTIC.value in identity.channels
    vector_fresh = True
    if used_embeddings:
        vector_fresh = (
            source is not None
            and target is not None
            and row.target_page_version == target_current
            and await _embedding_present(
                session,
                page_id=row.source_page_id,
                version=source_current,
                model=row.embedding_model,
            )
            and await _embedding_present(
                session,
                page_id=row.target_page_id,
                version=target_current,
                model=row.embedding_model,
            )
        )

    facts = PlanFacts(
        plan_id=row.id,
        source_page_id=row.source_page_id,
        target_page_id=row.target_page_id,
        source_page_version=row.source_page_version,
        source_current_version=source_current,
        target_page_version=row.target_page_version,
        target_current_version=target_current,
        source_protected=await _protected(session, page_id=row.source_page_id),
        target_protected=await _protected(session, page_id=row.target_page_id),
        target_published=(
            target_version_row is not None
            and PublicationState(target_version_row.publication_state) is PublicationState.PUBLISHED
        ),
        target_gone=target is None or target.gone,
        target_http_status=None if target is None else target.http_status,
        target_canonical_ok=(
            target_version_row is not None
            and CanonicalStatus(target_version_row.canonical_status) in _CANONICAL_OK
        ),
        target_redirect_supported=(
            target_version_row is not None
            and len(target_version_row.redirect_chain) <= max_redirect_hops
            and all(
                _host_of(hop) == _host_of(target_version_row.canonical_url)
                for hop in target_version_row.redirect_chain
            )
        ),
        target_noindex=(
            target_version_row is not None
            and _NOINDEX in {d.lower() for d in target_version_row.robots_directives}
        ),
        target_thin=(
            target_eligible is not None
            and IneligibilityReason.THIN_CONTENT in target_eligible.target_reasons
        ),
        source_page_type=source_type,
        target_page_type=target_type,
        page_types_compatible=types_compatible,
        destination_internal=await _internal(
            session, target_page_id=row.target_page_id, scope_hosts=scope_hosts
        ),
        destination_allowed_by_policy=destination_allowed,
        already_linked=already_linked,
        duplicate_allowed=False,
        anchor_valid=anchor_valid,
        rollback_metadata_present=rollback_ready,
        vector_fresh=bool(vector_fresh),
        # The RECOMMENDATION policy version. The engine records the AUTONOMY policy's on the
        # decision row; this field only describes the plan's own provenance.
        policy_version=policy.version,
        confidence=row.confidence,
        calibration_version=row.calibration_version,
    )
    return facts, identity
