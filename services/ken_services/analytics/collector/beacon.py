"""``GET /k.js``: the beacon, built once per collector instance.

The script itself is a static, dependency-free ES2017 file
(:data:`BEACON_FILE`) which never changes at runtime. Its bounds, however, are POLICY
(``policies/analytics/analytics.v1.yaml``), so the served body is the static source prefixed
with ONE deterministic line that publishes those bounds::

    window.KEN_BEACON_CONFIG={"flushSeconds": 5.0, ...};

That keeps the policy the single source of truth without templating the script, lets the
static file run unmodified under vitest, and makes the served bytes a pure function of
(file, policy). The beacon clamps every value it reads anyway.

Headers (ADR-0015 §1): ``application/javascript``, ``nosniff``, ``max-age`` from
``collector.beacon_cache_max_age_seconds``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ken_contracts.analytics import CollectorPolicy

__all__ = [
    "BEACON_FILE",
    "BEACON_PATH",
    "CONFIG_GLOBAL",
    "BeaconAsset",
    "beacon_config",
    "beacon_source",
    "build_beacon",
]

BEACON_PATH: Final = "/k.js"
CONFIG_GLOBAL: Final = "KEN_BEACON_CONFIG"
BEACON_FILE: Final = Path(__file__).resolve().parent / "static" / "ken-beacon.js"
_CONTENT_TYPE: Final = b"application/javascript; charset=utf-8"


def beacon_config(limits: CollectorPolicy) -> dict[str, float]:
    """The policy bounds the beacon needs (all of them numbers; no secrets, no identifiers)."""
    return {
        "flushSeconds": float(limits.beacon_flush_seconds),
        "maxEvents": float(limits.max_events_per_request),
        "maxHrefLinks": float(limits.max_href_links_per_page),
        "maxPathChars": float(limits.max_path_chars),
        "maxHrefChars": float(limits.max_href_chars),
        "maxAnchorChars": float(limits.max_anchor_chars),
    }


def beacon_source() -> str:
    """The static beacon, read from disk (once per :func:`build_beacon` call)."""
    return BEACON_FILE.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class BeaconAsset:
    """The exact bytes and headers of ``GET /k.js``."""

    body: bytes
    headers: tuple[tuple[bytes, bytes], ...]


def build_beacon(limits: CollectorPolicy) -> BeaconAsset:
    prelude = f"window.{CONFIG_GLOBAL}={json.dumps(beacon_config(limits), sort_keys=True)};\n"
    body = (prelude + beacon_source()).encode("utf-8")
    return BeaconAsset(
        body=body,
        headers=(
            (b"content-type", _CONTENT_TYPE),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", f"max-age={limits.beacon_cache_max_age_seconds}".encode("ascii")),
            (b"x-content-type-options", b"nosniff"),
        ),
    )
