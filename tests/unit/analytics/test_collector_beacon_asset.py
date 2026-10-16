"""``GET /k.js``: the served bytes are a pure function of (static file, policy).

Also guards the §12.8 cuts at the source level: the beacon has no cookies, no
``sessionStorage``, no landing events and no injected gtag.
"""

from __future__ import annotations

import json
import re

from ken_contracts.analytics import CollectorPolicy
from ken_services.analytics.collector.beacon import (
    BEACON_FILE,
    BEACON_PATH,
    CONFIG_GLOBAL,
    beacon_config,
    beacon_source,
    build_beacon,
)
from tests.unit.analytics._collector_support import policy


def test_the_served_body_is_the_policy_prelude_plus_the_static_file() -> None:
    limits = policy().collector
    asset = build_beacon(limits)
    body = asset.body.decode("utf-8")
    prelude, _, rest = body.partition("\n")
    assert prelude == f"window.{CONFIG_GLOBAL}={json.dumps(beacon_config(limits), sort_keys=True)};"
    assert rest == beacon_source()
    assert build_beacon(limits).body == asset.body  # deterministic


def test_the_prelude_publishes_every_policy_bound() -> None:
    limits = policy().collector
    values = json.loads(
        build_beacon(limits).body.decode("utf-8").split("=", 1)[1].split(";\n", 1)[0]
    )
    assert values == {
        "flushSeconds": limits.beacon_flush_seconds,
        "maxEvents": limits.max_events_per_request,
        "maxHrefLinks": limits.max_href_links_per_page,
        "maxPathChars": limits.max_path_chars,
        "maxHrefChars": limits.max_href_chars,
        "maxAnchorChars": limits.max_anchor_chars,
    }
    assert all(isinstance(value, float) for value in values.values())


def test_a_different_policy_changes_only_the_prelude() -> None:
    other = CollectorPolicy(beacon_flush_seconds=2.0, beacon_cache_max_age_seconds=60)
    asset = build_beacon(other)
    assert b'"flushSeconds": 2.0' in asset.body
    assert dict(asset.headers)[b"cache-control"] == b"max-age=60"
    assert asset.body.decode("utf-8").endswith(beacon_source())


def test_the_headers_are_complete_and_safe() -> None:
    asset = build_beacon(policy().collector)
    headers = dict(asset.headers)
    assert headers[b"content-type"] == b"application/javascript; charset=utf-8"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert int(headers[b"content-length"]) == len(asset.body)
    assert BEACON_PATH == "/k.js"


def code_only(source: str) -> str:
    """``source`` without its ``/* ... */`` comments (the banner explains the §12.8 cuts)."""
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)


def test_the_beacon_is_small_dependency_free_and_carries_no_storage_surface() -> None:
    source = beacon_source()
    code = code_only(source)
    assert BEACON_FILE.name == "ken-beacon.js"
    lines = source.splitlines()
    assert len(lines) < 200, "the beacon stays about 150 lines"
    assert all(len(line) <= 100 for line in lines)
    for forbidden in (
        "sessionStorage",
        "document.cookie",
        "localStorage.setItem",
        "XMLHttpRequest",
        "fetch(",
        "import ",
        "require(",
        "gtag",
        '"lnd"',
        "eval(",
        "innerHTML",
    ):
        assert forbidden not in code, f"the beacon must not use {forbidden}"
    # Opt-out signals, both privacy headers' client-side twins, and the only storage read.
    for required in (
        "globalPrivacyControl",
        "doNotTrack",
        "kenAnalyticsConsent",
        'getItem("ken_internal")',
        "getRandomValues",
        "IntersectionObserver",
        "sendBeacon",
        "dataLayer",
    ):
        assert required in code
    assert "ES2017" in source  # the authoring target is documented in the banner
    assert "=>" not in code and "const " not in code and "let " not in code
