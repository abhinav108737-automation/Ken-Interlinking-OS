"""Build the configured :class:`~ken_contracts.cms.CMSProvider`.

FROZEN SIGNATURE: ``build_cms_provider(settings) -> CMSProvider``. The API
imports exactly this.

Phase 0 has no real CMS, so ``settings.cms_provider`` is ``Literal["mock"]``
and the only product is a :class:`MockCMSProvider` seeded from
:mod:`ken_core.providers.cms.seed`. Each call returns a NEW provider with its
own in-memory state: build one at application startup and ``aclose()`` it at
shutdown.
"""

from __future__ import annotations

from typing import assert_never

from ken_contracts.cms import CMSProvider
from ken_core.config import Settings
from ken_core.providers.cms.mock import MockCMSProvider
from ken_core.providers.cms.seed import seed_pages


def build_cms_provider(settings: Settings) -> CMSProvider:
    """Return the CMS provider selected by ``KEN_CMS_PROVIDER``."""
    match settings.cms_provider:
        case "mock":
            return MockCMSProvider(seed_pages())
        case _:  # pragma: no cover - exhaustive over the Literal
            assert_never(settings.cms_provider)
