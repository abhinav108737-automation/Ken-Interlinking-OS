"""Build the configured :class:`~ken_contracts.cms.CMSProvider`.

FROZEN SIGNATURE: ``build_cms_provider(settings) -> CMSProvider``. The API imports exactly
this. ``KEN_CMS_PROVIDER`` (ADR-0014 s4):

* ``mock`` (default) -- :class:`MockCMSProvider` seeded from
  :mod:`ken_core.providers.cms.seed`, with REAL HTML mutation through the deterministic
  engine;
* ``simulated`` -- :class:`SimulatedCMSProvider` over its own database
  (``KEN_CMS_SIM_DATABASE_URL``; ``make cms-sim-seed`` first);
* ``http`` -- :class:`HttpCMSProvider` for TRD section 13, gated (loopback unless
  ``KEN_CMS_ALLOW_REMOTE``; writes need ``KEN_CMS_WRITES_ENABLED``).

This is the composition root of the HTML engine: ``ken_core`` never imports ``ken_services``
statically, so the engine (``ken_services.deployment.changes.ChangeEngine`` over
``ken_services.deployment.mutation.HtmlMutator``) is resolved lazily BY NAME here. Each call
returns a NEW provider: build one at application startup and ``aclose()`` it at shutdown.
"""

from __future__ import annotations

import importlib
from typing import Any, Final, assert_never

from ken_contracts.cms import CMSProvider
from ken_core.config import Settings
from ken_core.logging import get_logger
from ken_core.providers.cms.html_engine import LinkHtmlEngine
from ken_core.providers.cms.http import HttpCMSProvider
from ken_core.providers.cms.mock import MockCMSProvider
from ken_core.providers.cms.seed import seed_pages
from ken_core.providers.cms.simulated import SimulatedCMSProvider

CHANGE_ENGINE: Final = "ken_services.deployment.changes"
MUTATOR: Final = "ken_services.deployment.mutation"

logger = get_logger(__name__)


def build_html_engine() -> LinkHtmlEngine:
    """The deterministic engine over the real ``HtmlMutator`` (resolved by name)."""
    changes: Any = importlib.import_module(CHANGE_ENGINE)
    mutation: Any = importlib.import_module(MUTATOR)
    engine: LinkHtmlEngine = changes.ChangeEngine(mutation.HtmlMutator())
    return engine


def _mock_engine() -> LinkHtmlEngine | None:
    """The mock's engine; while the mutation module is absent (it is a separate workstream),
    the in-memory mock degrades to the Phase 0 protocol-only behaviour, LOGGED. The simulated
    provider never degrades: it raises."""
    try:
        return build_html_engine()
    except ModuleNotFoundError as exc:
        if exc.name != MUTATOR:
            raise
        logger.warning("cms_mock_without_html_engine", missing=MUTATOR)
        return None


def build_cms_provider(settings: Settings) -> CMSProvider:
    """Return the CMS provider selected by ``KEN_CMS_PROVIDER``."""
    match settings.cms_provider:
        case "mock":
            return MockCMSProvider(seed_pages(), html_engine=_mock_engine())
        case "simulated":
            return SimulatedCMSProvider(
                database_url=settings.cms_sim_database_url,
                html_engine=build_html_engine(),
                connect_timeout_s=settings.database_connect_timeout_s,
            )
        case "http":
            return HttpCMSProvider(
                base_url=settings.cms_base_url,
                token=settings.cms_token,
                writes_enabled=settings.cms_writes_enabled,
                allow_remote=settings.cms_allow_remote,
                timeout_s=settings.cms_timeout_s,
            )
        case _:  # pragma: no cover - exhaustive over the Literal
            assert_never(settings.cms_provider)
