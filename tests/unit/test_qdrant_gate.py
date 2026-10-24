"""KEN_REQUIRE_QDRANT_TESTS turns "Qdrant is down" from a skip into a failure (one helper)."""

from __future__ import annotations

import pytest

from tests.support import qdrant


@pytest.fixture
def qdrant_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qdrant, "_REACHABLE", False)


class _Item:
    def __init__(self, marked: bool) -> None:
        self._marked = marked

    def get_closest_marker(self, name: str) -> object | None:
        return object() if self._marked and name == "qdrant" else None


def test_unreachable_without_the_requirement_skips(
    qdrant_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEN_REQUIRE_QDRANT_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        qdrant.require_real_qdrant()
    qdrant.gate_collection([_Item(True)])  # no error at collection either


def test_unreachable_with_the_requirement_fails_never_skips(
    qdrant_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEN_REQUIRE_QDRANT_TESTS", "1")
    with pytest.raises(pytest.fail.Exception):
        qdrant.require_real_qdrant()
    with pytest.raises(pytest.UsageError, match="KEN_REQUIRE_QDRANT_TESTS"):
        qdrant.gate_collection([_Item(False), _Item(True)])
    qdrant.gate_collection([_Item(False)])  # nothing marked is selected: nothing to enforce


def test_reachable_never_interferes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEN_REQUIRE_QDRANT_TESTS", "1")
    monkeypatch.setattr(qdrant, "_REACHABLE", True)
    qdrant.require_real_qdrant()
    qdrant.gate_collection([_Item(True)])
