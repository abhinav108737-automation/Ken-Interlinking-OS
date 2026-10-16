"""``make analytics-sim-serve``: the simulated site AND the collector in ONE process.

    python -m ken_services.analytics.collector.serve

Two verified loopback listeners at OS-assigned ports (never 8765), both printed:

1. the **collector** starts first, because its URL is what the beacon tag must point at;
2. the **simulated site** then serves every simulated-CMS page with
   ``<script src="{collector}/k.js" defer data-ken-collector="{collector}/e"></script>``
   injected (:func:`ken_services.deployment.sim_site.render_page`), which changes no content
   hash: inventory normalisation and the deployment source map both drop ``<script>``;
3. the site's own origin is then added to the collector's allowed origins -- its port exists
   only once the socket is bound. Both origins are loopback, so every event the collector
   stores is flagged ``synthetic`` (ADR-0015 §12.6.5): a local demo is never real data.

A standalone collector target was deliberately CUT (§12.2): a production collector is an
ASK-level deployment, so locally the collector exists only alongside the simulated site.

Exit codes: 0 done; 1 the command could not complete (class name only); 2 usage or
configuration error. Nothing here prints a database URL, credentials or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Final

from pydantic import ValidationError

from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_readonly_sessionmaker, create_sessionmaker
from ken_core.providers.cms.simulated import SimulatedCMSProvider, assert_safe_sim_url
from ken_services.analytics.collector.app import CollectorApp, CollectorConfigError
from ken_services.analytics.collector.index import SqlLiveLinkIndex
from ken_services.analytics.collector.server import CollectorError, CollectorServer
from ken_services.analytics.collector.sink import SqlLinkEventSink
from ken_services.analytics.policy import PolicyError, load_configured_analytics_policy
from ken_services.analytics.protocol import CollectorConfig
from ken_services.deployment.sim_site import SimSiteError, SimulatedSite

__all__ = ["main", "serve_line"]

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2


def serve_line(
    *, site_url: str, collector_url: str, pages: int, live_plans: int, synthetic: bool
) -> str:
    """One JSON line (stdout): the two verified URLs and what is behind them."""
    return json.dumps(
        {
            "site_url": site_url,
            "collector_url": collector_url,
            "beacon_url": f"{collector_url}/k.js",
            "events_url": f"{collector_url}/e",
            "pages": pages,
            "live_plans": live_plans,
            "synthetic": synthetic,
        },
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.analytics.collector.serve",
        description="Serve the simulated CMS with the first-party beacon and the collector.",
    )
    parser.add_argument(
        "--startup-timeout-s", type=float, default=10.0, help="Listener verification timeout."
    )
    return parser


async def _serve(settings: Settings, startup_timeout_s: float) -> int:
    try:
        assert_safe_sim_url(settings.cms_sim_database_url)
        policy = load_configured_analytics_policy(settings)
        config = CollectorConfig.from_settings(settings, policy)
    except (ConfigurationError, PolicyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    engine = create_engine(settings)  # no I/O until first use
    index = SqlLiveLinkIndex(create_readonly_sessionmaker(engine))
    sink = SqlLinkEventSink(create_sessionmaker(engine))
    try:
        app = CollectorApp(config=config, index=index, sink=sink)
    except CollectorConfigError as exc:
        await engine.dispose()
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    provider = SimulatedCMSProvider(
        database_url=settings.cms_sim_database_url,
        html_engine=None,  # read-only: the site never mutates
        connect_timeout_s=settings.database_connect_timeout_s,
    )
    collector = CollectorServer(app, startup_timeout_s=startup_timeout_s)
    site: SimulatedSite | None = None
    try:
        try:
            pages = len(await provider.list_pages())
        except Exception as exc:
            print(
                f"the simulated CMS is not readable ({type(exc).__name__}); "
                "run make cms-sim-seed first",
                file=sys.stderr,
            )
            return EXIT_RUN_ERRORS
        try:
            collector_url = await collector.start()
            site = SimulatedSite(
                provider, startup_timeout_s=startup_timeout_s, beacon_src=collector_url
            )
            site_url = await site.start()
            app.add_allowed_origin(site_url)
        except (CollectorError, CollectorConfigError, SimSiteError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_RUN_ERRORS
        print(
            serve_line(
                site_url=site_url,
                collector_url=collector_url,
                pages=pages,
                live_plans=index.size,
                synthetic=app.synthetic,
            ),
            flush=True,
        )
        if index.size == 0:
            print(
                "no live link plans were found: the collector will drop every event as "
                "link_not_live until a deployment is applied",
                file=sys.stderr,
                flush=True,
            )
        print("serving until interrupted (Ctrl-C)", file=sys.stderr, flush=True)
        await site.wait()
        return EXIT_OK
    finally:
        if site is not None:
            await site.stop()
        await collector.stop()
        await provider.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse has already printed its usage message
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    try:
        return asyncio.run(_serve(settings, float(args.startup_timeout_s)))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
