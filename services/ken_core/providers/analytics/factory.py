"""Build the analytics provider a run asked for (ADR-0015 §4).

FROZEN SIGNATURE: ``build_analytics_provider(settings, *, name, resolver, file=None,
transport=None)``, mirroring ``build_cms_provider``'s ``match`` -- one composition root, each
call returning a NEW provider that the caller ``aclose()``s.

======================  ===================================================================
``name``                What it builds
======================  ===================================================================
``mock``                An EMPTY :class:`MockAnalyticsProvider`. The seeded demo generator
                        constructs its own with rows (``ken_services.analytics.synthetic``).
``csv``                 ``ken_link_daily`` / ``ken_page_daily`` from ``file`` (required).
``ga4``                 The GA4 Data API: gated, https, token file, no redirects.
``gsc``                 Search Console Search Analytics: same gating.
``first_party``         Stored rows (a reader for Phase 8/9, never an import source).
======================  ===================================================================

``resolver`` is the :class:`~ken_contracts.analytics.PageUrlResolver` built ONCE per import
and injected here, never into a method; ``mock`` and ``first_party`` carry page ids already
and ignore it. ``transport`` is for tests only: ``httpx.MockTransport`` is how the Google
providers are exercised, and Google itself is never called.

``ken_core`` must not import ``ken_services``, so the factory cannot read the analytics
POLICY file (its loader is a service). The CSV provider therefore gets the CONTRACT maximum
row count as a structural cap -- enough to stop a file exhausting memory -- and the importer,
which holds the loaded policy, enforces the configured ``imports.max_rows`` on top of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx

from ken_contracts.analytics import AnalyticsBatchProvider, ImportPolicy, PageUrlResolver
from ken_core.config import ConfigurationError, Settings
from ken_core.providers.analytics.csv import CsvAnalyticsProvider
from ken_core.providers.analytics.ga4 import Ga4AnalyticsProvider
from ken_core.providers.analytics.gsc import SearchConsoleAnalyticsProvider
from ken_core.providers.analytics.mock import MockAnalyticsProvider
from ken_core.providers.analytics.stored import StoredAnalyticsProvider

__all__ = ["ANALYTICS_PROVIDERS", "build_analytics_provider"]

ANALYTICS_PROVIDERS: Final = ("mock", "csv", "ga4", "gsc", "first_party")


def build_analytics_provider(
    settings: Settings,
    *,
    name: str,
    resolver: PageUrlResolver,
    file: str | Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AnalyticsBatchProvider:
    """Return the named provider.

    Raises:
        ConfigurationError: unknown name, a CSV run without a file, or a Google run whose
            property / site is not configured.
    """
    match name:
        case "mock":
            return MockAnalyticsProvider()
        case "csv":
            if file is None:
                raise ConfigurationError("a csv import needs FILE=<path>")
            return CsvAnalyticsProvider(
                path=file,
                resolver=resolver,
                max_file_bytes=settings.analytics_import_max_file_bytes,
                max_rows=ImportPolicy().max_rows,
            )
        case "ga4":
            if not settings.ga4_property_id:
                raise ConfigurationError("KEN_GA4_PROPERTY_ID is not set")
            return Ga4AnalyticsProvider(settings, resolver=resolver, transport=transport)
        case "gsc":
            if not settings.gsc_site_url:
                raise ConfigurationError("KEN_GSC_SITE_URL is not set")
            return SearchConsoleAnalyticsProvider(settings, resolver=resolver, transport=transport)
        case "first_party":
            return StoredAnalyticsProvider.from_settings(settings)
        case _:
            raise ConfigurationError(
                "unknown analytics provider (expected one of: "
                + ", ".join(ANALYTICS_PROVIDERS)
                + ")"
            )
