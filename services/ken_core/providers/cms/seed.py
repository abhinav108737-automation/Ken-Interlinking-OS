"""Deterministic seed pages for :class:`~ken_core.providers.cms.mock.MockCMSProvider`.

Defined in Python rather than as data files so they ship inside the container
image without any package-data configuration.

Every page lives on ``https://example.com``: these are illustrative Ken
Research-style pages, never real Ken Research URLs. Values are fixed (including
timestamps and version tokens) so that two processes seeded from here start
from byte-identical state.

The seed covers the three shapes Phase 0 needs:

* a market report page (the usual link *source*),
* a related regional page (a natural link *target*),
* a ``protected=True`` page, which no mutation may ever touch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from ken_contracts.cms import CMSPage

INDIA_EV_MARKET_PAGE_ID: Final = "page-india-ev-market-2030"
SEA_EV_MARKET_PAGE_ID: Final = "page-sea-ev-market-2030"
RESEARCH_METHODOLOGY_PAGE_ID: Final = "page-research-methodology"

SEED_PAGE_IDS: Final = (
    INDIA_EV_MARKET_PAGE_ID,
    SEA_EV_MARKET_PAGE_ID,
    RESEARCH_METHODOLOGY_PAGE_ID,
)

# Opaque tokens. They look human-readable only so fixtures are easy to read;
# no caller may parse them (ken_contracts.cms.CMSPage.version).
_SEED_VERSION: Final = "seed-v1"

_PUBLISHED_AT: Final = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_UPDATED_AT: Final = datetime(2026, 3, 2, 12, 30, tzinfo=UTC)


def seed_pages() -> list[CMSPage]:
    """Return fresh copies of the seed pages.

    A new list of new models on every call, so one provider mutating its state
    can never leak into another provider seeded from here.
    """
    return [
        CMSPage(
            page_id=INDIA_EV_MARKET_PAGE_ID,
            url="https://example.com/industry-reports/india-electric-vehicle-market-outlook-to-2030",
            canonical_url=(
                "https://example.com/industry-reports/india-electric-vehicle-market-outlook-to-2030"
            ),
            title="India Electric Vehicle Market Outlook to 2030",
            body_html=(
                '<p id="p-intro">The India electric vehicle market is expanding rapidly, '
                "driven by state purchase incentives, falling battery pack prices and "
                "a growing public charging network.</p>"
                '<p id="p-regional">Manufacturers are also benchmarking India against '
                "neighbouring Southeast Asian markets, where two-wheeler electrification "
                "is following a similar trajectory.</p>"
            ),
            version=_SEED_VERSION,
            protected=False,
            published_at=_PUBLISHED_AT,
            updated_at=_UPDATED_AT,
            metadata={
                "content_type": "market_report",
                "geography": "India",
                "sector": "Automotive",
            },
        ),
        CMSPage(
            page_id=SEA_EV_MARKET_PAGE_ID,
            url="https://example.com/industry-reports/southeast-asia-electric-vehicle-market-outlook-to-2030",
            canonical_url=(
                "https://example.com/industry-reports/"
                "southeast-asia-electric-vehicle-market-outlook-to-2030"
            ),
            title="Southeast Asia Electric Vehicle Market Outlook to 2030",
            body_html=(
                '<p id="p-intro">Southeast Asia\'s electric vehicle market is led by '
                "Thailand, Indonesia and Vietnam, each pursuing local assembly and "
                "battery supply-chain investment.</p>"
                '<p id="p-charging">Charging infrastructure remains concentrated in major '
                "metropolitan areas, limiting adoption outside urban centres.</p>"
            ),
            version=_SEED_VERSION,
            protected=False,
            published_at=_PUBLISHED_AT,
            updated_at=_UPDATED_AT,
            metadata={
                "content_type": "market_report",
                "geography": "Southeast Asia",
                "sector": "Automotive",
            },
        ),
        CMSPage(
            page_id=RESEARCH_METHODOLOGY_PAGE_ID,
            url="https://example.com/research-methodology",
            canonical_url="https://example.com/research-methodology",
            title="Research Methodology",
            body_html=(
                '<p id="p-overview">Our research combines primary interviews with '
                "industry participants and structured secondary research.</p>"
                '<p id="p-validation">Market estimates are triangulated using top-down '
                "and bottom-up approaches before publication.</p>"
            ),
            version=_SEED_VERSION,
            protected=True,
            published_at=_PUBLISHED_AT,
            updated_at=_UPDATED_AT,
            metadata={"content_type": "corporate"},
        ),
    ]
