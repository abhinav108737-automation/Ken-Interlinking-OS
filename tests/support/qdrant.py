"""The ONE place that decides what an unreachable real Qdrant means for a test run.

Tests that need the REAL Qdrant container carry ``@pytest.mark.qdrant`` (the in-memory store
variants are unmarked and unconditional). By default an unreachable Qdrant SKIPS them, loudly
(``-ra`` prints every reason). With ``KEN_REQUIRE_QDRANT_TESTS=1`` -- set by ``make
test-qdrant``, ``make test-all`` and so ``make verify`` -- it is a hard FAILURE, so the gate
can never go green by silently skipping the real-Qdrant tests.
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Sequence
from typing import Any

import pytest

QDRANT_URL = "http://127.0.0.1:6333"
_TRUE = {"1", "true", "yes"}


def qdrant_required() -> bool:
    return os.environ.get("KEN_REQUIRE_QDRANT_TESTS", "").lower() in _TRUE


def _probe() -> bool:
    try:
        with urllib.request.urlopen(f"{QDRANT_URL}/readyz", timeout=1) as response:  # noqa: S310
            return bool(response.status == 200)
    except Exception:
        return False


_REACHABLE: bool | None = None


def qdrant_reachable() -> bool:
    global _REACHABLE
    if _REACHABLE is None:
        _REACHABLE = _probe()
    return _REACHABLE


UNREACHABLE_REASON = (
    f"real Qdrant is not reachable at {QDRANT_URL} (run: make qdrant-up). "
    "The tests marked 'qdrant' did NOT run."
)


def require_real_qdrant() -> None:
    """Call from a fixture/test that needs the container: skip, or FAIL when required."""
    if qdrant_reachable():
        return
    if qdrant_required():
        pytest.fail(f"KEN_REQUIRE_QDRANT_TESTS is set but {UNREACHABLE_REASON}", pytrace=False)
    pytest.skip(UNREACHABLE_REASON)


def gate_collection(items: Sequence[Any]) -> None:
    """At collection: required + any selected 'qdrant' test + unreachable => usage error."""
    if not qdrant_required():
        return
    if not any(item.get_closest_marker("qdrant") for item in items):
        return
    if not qdrant_reachable():
        raise pytest.UsageError(f"KEN_REQUIRE_QDRANT_TESTS is set but {UNREACHABLE_REASON}")
