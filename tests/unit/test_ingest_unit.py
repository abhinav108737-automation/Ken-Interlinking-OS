"""Pure parts of the ingestion service (no database)."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import MAX_URL_LENGTH, IssueCode, SourceType
from ken_services.inventory import ingest
from ken_services.inventory.urls import InvalidUrlError


def test_lock_key_matches_its_derivation() -> None:
    digest = hashlib.sha256(ingest.INGEST_LOCK_NAME.encode()).digest()
    assert int.from_bytes(digest[:8], "big", signed=True) == ingest.INGEST_LOCK_KEY


def test_normalisation_failures_map_to_issue_codes() -> None:
    from ken_services.inventory.normalize import NormalizationError

    assert ingest._normalisation_issue_code(InvalidUrlError("x")) is IssueCode.INVALID_URL
    assert ingest._normalisation_issue_code(NormalizationError("x")) is IssueCode.INVALID_URL
    assert ingest._normalisation_issue_code(RuntimeError("x")) is IssueCode.PARSE_ERROR
    assert ingest._normalisation_issue_code(ValueError("x")) is IssueCode.PARSE_ERROR


@pytest.mark.parametrize(
    ("value", "storable"),
    [
        ("https://site.test/a", True),
        (None, False),
        ("", False),
        ("https://site.test/\x00", False),
        ("https://site.test/" + "a" * MAX_URL_LENGTH, False),
    ],
)
def test_only_indexable_urls_become_known_urls(value: str | None, storable: bool) -> None:
    assert (ingest._storable_url(value) is not None) is storable


def test_tally_copy_is_independent() -> None:
    tally = ingest._Tally(pages_fetched=1)
    tally.issue_counts["timeout"] = 1
    clone = tally.copy()
    clone.pages_fetched += 1
    clone.issue_counts["timeout"] += 1
    assert (tally.pages_fetched, tally.issue_counts["timeout"]) == (1, 1)
    assert clone.counters().pages_fetched == 2


class _Source:
    name = "unit"
    source_type = SourceType.FILE_EXPORT

    def __init__(self, snapshot: Any) -> None:
        self.snapshot = snapshot
        self.closed = 0

    def config_snapshot(self) -> Any:
        return self.snapshot

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("snapshot", [{"x": object()}, {"x": math.nan}, ["not", "a", "dict"]])
def test_snapshot_must_be_a_json_object(snapshot: Any) -> None:
    with pytest.raises(ValueError, match="config_snapshot"):
        ingest._snapshot(_Source(snapshot))  # type: ignore[arg-type]


def test_snapshot_round_trips_to_plain_json() -> None:
    assert ingest._snapshot(_Source({"t": ("a", "b")})) == {"t": ["a", "b"]}  # type: ignore[arg-type]


def _never(doc: Any, /, *, scope: Any) -> Any:
    raise AssertionError("not reached")


async def test_invalid_arguments_still_close_the_source() -> None:
    source = _Source({})
    unbound: async_sessionmaker[AsyncSession] = async_sessionmaker()
    with pytest.raises(ValueError, match="max_stored_issues"):
        await ingest.run_ingestion(
            source=source,  # type: ignore[arg-type]
            sessionmaker=unbound,
            normalizer=_never,
            max_stored_issues=-1,
        )
    with pytest.raises(TypeError, match="AsyncEngine"):
        await ingest.run_ingestion(
            source=source,  # type: ignore[arg-type]
            sessionmaker=unbound,
            normalizer=_never,
        )
    assert source.closed == 2
