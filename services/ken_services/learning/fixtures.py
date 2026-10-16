"""Turning an eval case's declarative fixture into the real objects the engine consumes.

An :class:`~ken_contracts.learning.EvalCase` carries no Python. Its ``inputs.extra`` is a JSON
object describing pages, HTML bodies and plans; this module builds
:class:`~ken_contracts.intelligence.PageAnalysis`,
:class:`~ken_services.inventory.normalize.ContentBlock`,
:class:`~ken_core.db.repositories.recommendations.PageFacts` and
:class:`~ken_contracts.recommendations.LinkPlan` objects from it, so that every scorer calls
the SAME production functions the pipeline calls.

Three rules make the fixtures honest:

* **Identifiers are derived, never invented.** A fixture names a page by a readable key
  (``india-ev-report``) and the page id is ``uuid5(NAMESPACE_URL, "ken-eval:page:<key>")``.
  The same key therefore yields the same UUID in every process and on every machine -- which
  is what lets a report be byte-identical across runs -- and a case never carries a UUID that
  means nothing to a reader.
* **Blocks come from HTML through** :func:`~ken_services.inventory.normalize.extract_blocks`.
  A hand-built :class:`ContentBlock` has no text runs, and ``span_is_safe`` refuses every span
  in a block without runs -- so hand-building blocks would make every placement case
  vacuously "no placement". Parsing the fixture's markup with the production normaliser also
  means a case cannot declare block text that its HTML does not actually produce.
* **Protection is COMPUTED**, not just declared: a fixture's ``protected`` flag is OR-ed with
  :func:`~ken_services.intelligence.protection.match_protections` over the live taxonomy, so a
  page at ``/privacy-policy`` is protected because the shipped policy says so.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from ken_contracts.intelligence import (
    GeographyMention,
    Intent,
    LinkEligibility,
    PageAnalysis,
    RelationshipType,
    SalientBlock,
    TaxonomyPolicy,
    TopicScore,
)
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_contracts.recommendations import (
    LinkPlan,
    LinkPlanStatus,
    PlacementLocator,
    PlacementPreview,
    ReasonCode,
    RetrievalChannel,
    ScoreBreakdown,
    ScoreComponent,
)
from ken_core.db.repositories.recommendations import PageFacts
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility
from ken_services.intelligence.protection import match_protections
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock, extract_blocks

__all__ = [
    "ANALYSIS_VERSION",
    "DEFAULT_SITE",
    "ENGINE_VERSION",
    "EVAL_NAMESPACE",
    "FixtureError",
    "PageFixture",
    "blocks_of",
    "context_hash",
    "eval_uuid",
    "link_plan_of",
    "mapping_of",
    "page_fixtures",
    "preview_of",
    "require",
    "sequence_of",
]

EVAL_NAMESPACE: Final = "ken-eval"
ANALYSIS_VERSION: Final = "eval-1"
ENGINE_VERSION: Final = "eval-runner"
DEFAULT_SITE: Final = "https://research.example.com"
type PlanAnchorSource = Literal["target_topic", "target_title", "target_keyword", "edited"]
"""Copy of ``LinkPlan.anchor_source``, so a fixture's string is narrowed before it is used."""
_ANCHOR_SOURCES: Final = ("target_topic", "target_title", "target_keyword", "edited")
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
"""A fixed timestamp on every fixture object. No scorer reads it, and a wall clock would make
the report non-deterministic."""


class FixtureError(ValueError):
    """A case's ``inputs.extra`` does not describe what its suite needs.

    Always a dataset bug and never a decision: the case fails loudly rather than being scored.
    """


def eval_uuid(kind: str, key: str) -> uuid.UUID:
    """A stable UUID for a fixture identifier (``uuid5``, so it never varies by process)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{EVAL_NAMESPACE}:{kind}:{key}")


def require(extra: Mapping[str, Any], name: str) -> Any:
    if name not in extra:
        raise FixtureError(f"the case fixture needs {name!r}")
    return extra[name]


def mapping_of(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FixtureError(f"{what} must be a JSON object")
    return value


def sequence_of(value: Any, what: str) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FixtureError(f"{what} must be a JSON array")
    items: Sequence[Any] = value
    return items


def context_hash(text: str) -> str:
    return sha256_hex(text)


def blocks_of(body_html: str) -> tuple[ContentBlock, ...]:
    """The production normaliser's blocks, with text runs and enclosing elements."""
    return extract_blocks(body_html)


def preview_of(text: str, start: int, end: int) -> PlacementPreview:
    return PlacementPreview(before=text[:start], anchor=text[start:end], after=text[end:])


@dataclass(frozen=True, slots=True)
class PageFixture:
    """One page as a case declares it: its analysis, its live facts and its body HTML."""

    key: str
    page_id: uuid.UUID
    canonical_url: str
    path: str
    title: str | None
    current_version: int
    analysis: PageAnalysis
    facts: PageFacts
    body_html: str
    embedding_text: str
    links_into_market: int
    protection_rules: tuple[str, ...]

    @property
    def protected(self) -> bool:
        return self.facts.protected

    def eligibility(self, *, taxonomy: TaxonomyPolicy) -> LinkEligibility:
        """The page's LIVE eligibility, through the Phase 2 production computation."""
        return compute_eligibility(
            EligibilityState(
                publication_state=PublicationState(self.facts.publication_state),
                gone=self.facts.gone,
                protected=self.facts.protected,
                robots_directives=self.facts.robots_directives,
                canonical_status=CanonicalStatus(self.facts.canonical_status or "self"),
                http_status=self.facts.http_status,
                word_count=self.facts.word_count,
                page_type=self.analysis.page_type,
                has_analysis=True,
                analysis_fresh=True,
            ),
            taxonomy=taxonomy,
        )

    def blocks(self) -> tuple[ContentBlock, ...]:
        return blocks_of(self.body_html)


def _topics(raw: Any) -> tuple[TopicScore, ...]:
    return tuple(TopicScore.model_validate(item) for item in sequence_of(raw, "topics"))


def _geographies(raw: Any) -> tuple[GeographyMention, ...]:
    return tuple(GeographyMention.model_validate(item) for item in sequence_of(raw, "geographies"))


def _salient(raw: Any) -> tuple[SalientBlock, ...]:
    return tuple(SalientBlock.model_validate(item) for item in sequence_of(raw, "salient_blocks"))


def _page_fixture(
    key: str, raw: Mapping[str, Any], *, site: str, taxonomy: TaxonomyPolicy
) -> PageFixture:
    page_id = eval_uuid("page", key)
    path = str(raw.get("path") or f"/{key}")
    url = f"{site}{path}"
    title = None if raw.get("title") is None else str(raw["title"])
    version = int(raw.get("current_version", 1))
    declared = mapping_of(raw.get("metadata", {}), "metadata")
    matches = match_protections(
        url=url,
        metadata={str(key): str(value) for key, value in declared.items()},
        taxonomy=taxonomy,
    )
    protected = bool(raw.get("protected", False)) or bool(matches)
    analysis = PageAnalysis(
        page_id=page_id,
        page_version=version,
        analysis_version=ANALYSIS_VERSION,
        page_type=str(raw.get("page_type", "report")),
        intent=Intent(str(raw.get("intent", "commercial_research"))),
        commercial_importance=int(raw.get("commercial_importance", 2)),
        industry_id=raw.get("industry_id"),
        market_id=raw.get("market_id"),
        topics=_topics(raw.get("topics", ())),
        geographies=_geographies(raw.get("geographies", ())),
        keywords=tuple(str(item) for item in sequence_of(raw.get("keywords", ()), "keywords")),
        summary=str(raw.get("summary", "")),
        salient_blocks=_salient(raw.get("salient_blocks", ())),
    )
    facts = PageFacts(
        id=page_id,
        canonical_url=url,
        title=title,
        current_version=version,
        publication_state=str(raw.get("publication_state", "published")),
        gone=bool(raw.get("gone", False)),
        word_count=int(raw.get("word_count", 400)),
        internal_link_count=int(raw.get("internal_link_count", 0)),
        robots_directives=tuple(
            str(item) for item in sequence_of(raw.get("robots_directives", ()), "robots_directives")
        ),
        canonical_status=str(raw.get("canonical_status", "self")),
        http_status=raw.get("http_status", 200),
        protected=protected,
        has_any_analysis=True,
        analysis=analysis,
    )
    return PageFixture(
        key=key,
        page_id=page_id,
        canonical_url=url,
        path=path,
        title=title,
        current_version=version,
        analysis=analysis,
        facts=facts,
        body_html=str(raw.get("body_html") or ""),
        embedding_text=str(
            raw.get("embedding_text")
            or " ".join(part for part in (title, str(raw.get("summary", "")), path) if part)
        ),
        links_into_market=int(raw.get("links_into_market", 0)),
        protection_rules=tuple(sorted(match.rule_id for match in matches)),
    )


def page_fixtures(
    extra: Mapping[str, Any], *, taxonomy: TaxonomyPolicy, site: str = DEFAULT_SITE
) -> dict[str, PageFixture]:
    """Every page the case declares under ``extra.pages``, keyed by its fixture key."""
    pages = mapping_of(require(extra, "pages"), "pages")
    return {
        str(key): _page_fixture(
            str(key), mapping_of(value, f"pages.{key}"), site=site, taxonomy=taxonomy
        )
        for key, value in pages.items()
    }


def _breakdown(total: float) -> ScoreBreakdown:
    """A minimal valid breakdown. Scorers that score for real compute their own."""
    return ScoreBreakdown(
        components=(ScoreComponent(name="semantic", value=total, weight=1.0, contribution=total),),
        relevance=total,
        factors={},
        total=total,
        semantic_floor=0.0,
        policy_name="recommendation",
        policy_version=1,
    )


def link_plan_of(
    raw: Mapping[str, Any], *, source: PageFixture, target: PageFixture, plan_key: str
) -> LinkPlan:
    """A :class:`LinkPlan` as the engine would have written it for this fixture.

    The locator and preview are derived from the SOURCE's real block text, so a plan whose
    anchor is not present in the page cannot be built at all -- the same property the engine
    guarantees and the anchor-selection suite checks.
    """
    anchor = str(require(raw, "anchor_text"))
    block_id = str(raw.get("block_id", "b1"))
    block = next((b for b in source.blocks() if b.block_id == block_id), None)
    if block is None:
        raise FixtureError(f"plan {plan_key!r} names block {block_id!r}, absent from the source")
    start = int(raw["start"]) if "start" in raw else block.text.find(anchor)
    if start < 0:
        raise FixtureError(f"plan {plan_key!r} anchor is not present in block {block_id!r}")
    end = int(raw.get("end", start + len(anchor)))
    relationship = raw.get("relationship_type")
    anchor_source = str(raw.get("anchor_source", "target_topic"))
    if anchor_source not in _ANCHOR_SOURCES:
        raise FixtureError(f"plan {plan_key!r} has an unknown anchor_source")
    return LinkPlan(
        id=eval_uuid("plan", plan_key),
        source_page_id=source.page_id,
        source_page_version=source.current_version,
        source_url=source.canonical_url,
        target_page_id=target.page_id,
        target_page_version=target.current_version,
        target_url=target.canonical_url,
        target_title=target.title,
        relationship_type=None if relationship is None else RelationshipType(str(relationship)),
        channels=tuple(
            RetrievalChannel(str(item))
            for item in sequence_of(raw.get("channels", ["semantic"]), "channels")
        ),
        anchor_text=anchor,
        anchor_source=cast("PlanAnchorSource", anchor_source),
        locator=PlacementLocator(
            block_id=block_id,
            block_kind=block.kind,
            occurrence_index=int(raw.get("occurrence_index", 0)),
            start=start,
            end=end,
            context_hash=context_hash(block.text),
        ),
        preview=preview_of(block.text, start, end),
        score=_breakdown(float(raw.get("score", 0.6))),
        confidence=float(raw.get("confidence", 0.7)),
        reason_codes=tuple(
            ReasonCode(str(item))
            for item in sequence_of(
                raw.get("reason_codes", ["semantically_similar"]), "reason_codes"
            )
        ),
        rationale=str(raw.get("rationale", "Deterministic eval fixture.")),
        engine_version=ENGINE_VERSION,
        policy_version=1,
        ranking_policy_version=1,
        analysis_version=ANALYSIS_VERSION,
        status=LinkPlanStatus(str(raw.get("status", "proposed"))),
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )
