"""The first-party analytics collector (W2, ADR-0015 §1 as amended by §12).

A framework-free ASGI app plus a 150-line beacon, run only on loopback and only beside the
simulated site (``make analytics-sim-serve``). Deploying either to a real site is an
ASK-level action.

* :mod:`~ken_services.analytics.collector.app` -- the ASGI app and the ordered validation
  (every rejection still answers 204);
* :mod:`~ken_services.analytics.collector.beacon` -- ``GET /k.js`` and its policy prelude;
* :mod:`~ken_services.analytics.collector.index` -- the live-link index;
* :mod:`~ken_services.analytics.collector.ratelimit` -- the bounded per-IP fixed window;
* :mod:`~ken_services.analytics.collector.server` -- loopback, port 0, nonce-verified start;
* :mod:`~ken_services.analytics.collector.sink` -- the pseudonymous ``link_events`` insert;
* :mod:`~ken_services.analytics.collector.serve` -- the one-process demo entry point.
"""

from __future__ import annotations

from ken_services.analytics.collector.app import (
    EVENTS_PATH,
    HEALTH_PATH,
    CollectorApp,
    CollectorConfigError,
    RejectionCounter,
)
from ken_services.analytics.collector.beacon import BEACON_PATH, BeaconAsset, build_beacon
from ken_services.analytics.collector.index import (
    LiveLinkSnapshot,
    LivePlan,
    SqlLiveLinkIndex,
)
from ken_services.analytics.collector.ratelimit import FixedWindowRateLimiter
from ken_services.analytics.collector.server import (
    CollectorError,
    CollectorServer,
    serve_collector,
)
from ken_services.analytics.collector.sink import (
    LinkEventSink,
    NewLinkEvent,
    SqlLinkEventSink,
)

__all__ = [
    "BEACON_PATH",
    "EVENTS_PATH",
    "HEALTH_PATH",
    "BeaconAsset",
    "CollectorApp",
    "CollectorConfigError",
    "CollectorError",
    "CollectorServer",
    "FixedWindowRateLimiter",
    "LinkEventSink",
    "LiveLinkSnapshot",
    "LivePlan",
    "NewLinkEvent",
    "RejectionCounter",
    "SqlLinkEventSink",
    "SqlLiveLinkIndex",
    "build_beacon",
    "serve_collector",
]
