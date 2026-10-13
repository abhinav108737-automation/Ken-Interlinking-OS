"""AnalyticsProvider protocol.

FROZEN CONTRACT -- types and protocol only, with NO implementation in Phase 0.

Why it exists this early: MULTI_AGENT_ORCHESTRATION.md requires the analytics
contract to be frozen before parallel implementation, even though the Phase 0
deliverable list does not include an analytics feature. Freezing the shape now
costs one file and prevents a later phase from inventing a competing one.

Phase 7 implements GA4, Google Search Console and CSV-fallback providers behind
this protocol. All three are read-only by construction: there is no write
method, because analytics is a measurement input and never a mutation target.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field

from ken_contracts.common import AsyncClosable, ContractModel, FrozenModel, LinkId, PageId
from ken_contracts.model import ProviderHealth


class MetricSource(StrEnum):
    GA4 = "ga4"
    SEARCH_CONSOLE = "search_console"
    FIRST_PARTY = "first_party"
    CSV = "csv"


class DateRange(FrozenModel):
    start: date
    end: date


class LinkMetrics(ContractModel):
    """Observed performance of one deployed internal link."""

    link_id: str
    source: MetricSource
    range: DateRange
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    ctr: float | None = Field(default=None, ge=0.0, le=1.0)
    downstream_pageviews: int = Field(default=0, ge=0)


class PageMetrics(ContractModel):
    """Observed performance of one page."""

    page_id: str
    source: MetricSource
    range: DateRange
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    sessions: int = Field(default=0, ge=0)


@runtime_checkable
class AnalyticsProvider(AsyncClosable, Protocol):
    """Read-only measurement input. Deliberately has no write method."""

    name: str

    async def fetch_link_metrics(
        self, *, link_ids: Sequence[LinkId], date_range: DateRange
    ) -> list[LinkMetrics]: ...

    async def fetch_page_metrics(
        self, *, page_ids: Sequence[PageId], date_range: DateRange
    ) -> list[PageMetrics]: ...

    async def health(self) -> ProviderHealth: ...
