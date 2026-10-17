"""Corpus-wide link-graph read contracts (final-completion directive section 5).

ADDITIVE. The per-page position is the existing :class:`~ken_contracts.recommendations.
GraphSignals`; this module adds the three views a control-plane UI needs to see the WHOLE
graph: a corpus overview, one row per topical cluster, and a filterable page list.

Every number is read from ``page_graph_metrics``, which the ``graph_metrics`` job
(``make graph-metrics``) fills deterministically; nothing is computed on request.

**Orphan versus entry page -- read this before rendering.** ``is_orphan`` means exactly "no
internal inbound link from any other live page" and the schema ties it to ``in_degree = 0``.
The site's HOME PAGE is normally reached from outside the site, so it usually has no inbound
body link and is therefore reported ``is_orphan = true`` -- faithfully, because that is the
fact. It is not a page to rescue: it is the crawl entry, ``crawl_depth == 0``. A consumer must
use ``crawl_depth`` (or the ``is_entry_page`` convenience flag, which is exactly
``crawl_depth == 0``) to tell an entry page from a TRUE orphan; a true orphan has
``is_orphan = true`` and ``crawl_depth`` either ``None`` (unreachable from the entry) or > 0.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field

from ken_contracts.common import FrozenModel
from ken_contracts.errors import ListResponse
from ken_contracts.recommendations import GraphSignals

__all__ = [
    "GraphClusterSummary",
    "GraphOverview",
    "GraphPageRow",
    "GraphReader",
]


class GraphOverview(FrozenModel):
    """Corpus-level counts for one ``metrics_version`` (the newest computed)."""

    metrics_version: str | None = Field(
        description="Algorithm/recipe version of these numbers; None = never computed."
    )
    computed_at: AwareDatetime | None = Field(
        description="When the newest rows were computed; None = never computed."
    )
    pages: int = Field(ge=0, description="Live pages with metrics for their current version.")
    entry_pages: int = Field(
        ge=0, description="Pages with crawl_depth 0: the crawl entry, not orphans to recover."
    )
    orphans: int = Field(
        ge=0,
        description="is_orphan pages. INCLUDES the entry page when nothing links to it.",
    )
    true_orphans: int = Field(
        ge=0, description="Orphans that are not the crawl entry (is_orphan and crawl_depth != 0)."
    )
    nearly_orphans: int = Field(ge=0, description="At most the policy threshold of inbound pages.")
    bridges: int = Field(ge=0, description="Cut vertices: removing one would split a component.")
    components: int = Field(ge=0, description="Weakly connected components.")
    clusters: int = Field(ge=0, description="Topical clusters (pages with no topic are none).")


class GraphClusterSummary(FrozenModel):
    """One topical cluster. ``cluster_id`` is an ORDINAL (0 = largest), stable within one
    ``metrics_version`` only; ``None`` collects the pages that have no topic."""

    cluster_id: int | None = Field(ge=0)
    pages: int = Field(ge=1)
    orphans: int = Field(ge=0)
    true_orphans: int = Field(ge=0)
    nearly_orphans: int = Field(ge=0)
    bridges: int = Field(ge=0)
    mean_in_degree: float = Field(ge=0.0)
    top_page_id: UUID | None = Field(description="Highest internal PageRank in the cluster.")
    top_page_url: str | None = None


class GraphPageRow(FrozenModel):
    """A page's link-graph position with enough identity to render a row."""

    signals: GraphSignals
    canonical_url: str
    title: str | None = None
    is_entry_page: bool = Field(
        description="crawl_depth == 0. An entry page can be is_orphan WITHOUT being a true "
        "orphan (module docstring)."
    )


@runtime_checkable
class GraphReader(Protocol):
    """Everything the graph API reads. Async, keyword-only, read-only."""

    async def get_overview(self) -> GraphOverview: ...

    async def list_clusters(self) -> list[GraphClusterSummary]: ...

    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        cluster_id: int | None = None,
        component_id: int | None = None,
        orphans_only: bool = False,
        bridges_only: bool = False,
    ) -> ListResponse[GraphPageRow]: ...

    async def get_page(self, *, page_id: UUID) -> GraphPageRow:
        """Raises:
        IntelligenceNotFoundError: no such page, or metrics not computed for it yet.
        """
        ...
