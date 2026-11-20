"""Migration 0013 (governed priority designations) against the ``*_test`` database.

The domain dataclass (:class:`ken_services.recommendations.priority.PriorityDesignation`)
already refuses bad input, but that guards only writers that go through it. "Governed" must
also hold for anything that writes the table directly, so every rule is asserted here at the
DATABASE: the closed type list, the tier range, a non-blank reason / owner / audit actor, an
ordered window, one ACTIVE designation per (page, type) while retired ones are kept, and a page
that cannot be deleted out from under its designations.

Each refusal test is paired with an acceptance of the nearest valid row, so a constraint that
refused everything could not make the suite green.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from ken_core.priority import PriorityType
from ken_services.recommendations.priority import PriorityDesignation
from ken_services.recommendations.priority_store import violated_constraint
from tests.integration.conftest import SyncDb

HASH = sha256(b"x").hexdigest()
PAGE = uuid.UUID("00000000-0000-4000-8000-000000000131")
START = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


def _constraint(error: DBAPIError) -> str:
    """The violated constraint by NAME, via the SAME helper the CLI uses to phrase refusals.

    Deliberately not a parallel implementation: if unwrapping ever breaks, the CLI would
    silently fall back to a generic message, and these tests would go green. Sharing the helper
    makes that failure visible here.
    """
    return violated_constraint(error.orig or error)


def _page(c: Connection, page_id: uuid.UUID = PAGE) -> None:
    c.execute(
        text(
            "INSERT INTO pages (id, canonical_url, current_version, content_hash, "
            "publication_state, word_count, internal_link_count, external_link_count, "
            "first_seen_at, last_seen_at, last_changed_at) "
            "VALUES (:id, :u, 1, :h, 'published', 0, 0, 0, now(), now(), now())"
        ),
        {"id": page_id, "u": f"https://s.test/{page_id}", "h": HASH},
    )
    # pages.current_version references page_versions (DEFERRABLE, checked at commit), so a
    # page without its version row fails only when an otherwise-valid test commits.
    c.execute(
        text(
            "INSERT INTO page_versions (id, page_id, version, content_hash, hash_version, "
            "source_type, source_id, requested_url, url, canonical_url, canonical_status, "
            "publication_state, body_html, body_text, word_count, internal_link_count, "
            "external_link_count, fetched_at) VALUES (:id, :p, 1, :h, 1, 'cms', 's', 'u', 'u', "
            "'u', 'self', 'published', '', '', 0, 0, 0, now())"
        ),
        {"id": uuid.uuid4(), "p": page_id, "h": HASH},
    )


def _designation(c: Connection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "page_id": PAGE,
        "priority_type": PriorityType.HIGH_PRIORITY_REPORT.value,
        "tier": 1,
        "reason": "owner designated this report a commercial priority",
        "owner": "abhinav.kumar",
        "effective_from": START,
        "effective_to": None,
        "retired_at": None,
        "created_by": "abhinav.kumar",
        "updated_by": "abhinav.kumar",
    } | overrides
    c.execute(
        text(
            "INSERT INTO page_priorities (id, page_id, priority_type, tier, reason, owner, "
            "effective_from, effective_to, retired_at, created_by, updated_by) VALUES (:id, "
            ":page_id, :priority_type, :tier, :reason, :owner, :effective_from, "
            ":effective_to, :retired_at, :created_by, :updated_by)"
        ),
        values,
    )
    designation_id: uuid.UUID = values["id"]
    return designation_id


def _count(db: SyncDb) -> int:
    value: int = db.run(
        lambda c: c.execute(text("SELECT count(*) FROM page_priorities")).scalar_one()
    )
    return value


def _with_page(*rows: dict[str, Any]) -> Callable[[Connection], None]:
    def run(c: Connection) -> None:
        _page(c)
        for row in rows:
            _designation(c, **row)

    return run


# --------------------------------------------------------------------------- #
# The vocabulary and the tier range
# --------------------------------------------------------------------------- #
def test_every_governed_type_is_accepted(db: SyncDb) -> None:
    db.run(_with_page(*({"priority_type": t.value} for t in PriorityType)))
    count = db.run(lambda c: c.execute(text("SELECT count(*) FROM page_priorities")).scalar())
    assert count == len(PriorityType) == 9


def test_an_ungoverned_type_is_refused(db: SyncDb) -> None:
    error = _fails(db, _with_page({"priority_type": "vip_page"}))
    assert "priority_type_valid" in str(error)


@pytest.mark.parametrize("tier", [1, 5])
def test_the_tier_bounds_are_accepted(db: SyncDb, tier: int) -> None:
    db.run(_with_page({"tier": tier}))


@pytest.mark.parametrize("tier", [0, 6])
def test_a_tier_outside_the_range_is_refused(db: SyncDb, tier: int) -> None:
    error = _fails(db, _with_page({"tier": tier}))
    assert "tier_range" in str(error)


# --------------------------------------------------------------------------- #
# Explainability and audit fields cannot be blank
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("reason", "reason_present"),
        ("owner", "owner_present"),
        ("created_by", "created_by_present"),
        ("updated_by", "updated_by_present"),
    ],
)
def test_a_blank_reason_owner_or_actor_is_refused(db: SyncDb, column: str, constraint: str) -> None:
    error = _fails(db, _with_page({column: "   "}))
    assert constraint in str(error)


def test_audit_timestamps_default_to_now(db: SyncDb) -> None:
    db.run(_with_page({}))
    created, updated = db.run(
        lambda c: c.execute(text("SELECT created_at, updated_at FROM page_priorities")).one()
    )
    assert created is not None
    assert updated is not None


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #
def test_an_open_ended_window_is_accepted(db: SyncDb) -> None:
    db.run(_with_page({"effective_to": None}))


def test_a_window_ending_after_it_starts_is_accepted(db: SyncDb) -> None:
    db.run(_with_page({"effective_to": datetime(2026, 12, 1, tzinfo=UTC)}))


@pytest.mark.parametrize("effective_to", [START, datetime(2026, 10, 1, tzinfo=UTC)])
def test_an_empty_or_inverted_window_is_refused(db: SyncDb, effective_to: datetime) -> None:
    error = _fails(db, _with_page({"effective_to": effective_to}))
    assert "window_ordered" in str(error)


# --------------------------------------------------------------------------- #
# DEFECT 2: non-overlap, not uniqueness
# --------------------------------------------------------------------------- #
def test_overlapping_windows_of_one_type_are_rejected(db: SyncDb) -> None:
    error = _fails(
        db,
        _with_page(
            {"effective_from": START, "effective_to": datetime(2026, 12, 1, tzinfo=UTC)},
            {"effective_from": datetime(2026, 11, 15, tzinfo=UTC), "effective_to": None},
        ),
    )
    assert _constraint(error) == "page_priorities_no_overlap"


def test_two_open_ended_designations_of_one_type_are_rejected(db: SyncDb) -> None:
    error = _fails(db, _with_page({}, {}))
    assert _constraint(error) == "page_priorities_no_overlap"


def test_adjacent_half_open_windows_are_accepted(db: SyncDb) -> None:
    """``[Nov, Dec)`` then ``[Dec, ...)`` touch but do not overlap."""
    boundary = datetime(2026, 12, 1, tzinfo=UTC)
    db.run(
        _with_page(
            {"effective_from": START, "effective_to": boundary},
            {"effective_from": boundary, "effective_to": None, "tier": 1},
        )
    )
    assert _count(db) == 2


def test_a_future_replacement_can_be_scheduled_before_the_incumbent_expires(
    db: SyncDb,
) -> None:
    """The incumbent is open-ended and still in force; recording a FUTURE retirement truncates
    its validity there, so a successor starting then is adjacent, not overlapping. Nothing is
    edited away: both rows remain."""
    handover = datetime(2026, 12, 1, tzinfo=UTC)

    def run(c: Connection) -> None:
        _page(c)
        incumbent = _designation(c, effective_from=START, effective_to=None)
        c.execute(
            text("UPDATE page_priorities SET retired_at = :t WHERE id = :id"),
            {"t": handover, "id": incumbent},
        )
        _designation(c, effective_from=handover, effective_to=None, tier=1)

    db.run(run)
    assert _count(db) == 2


def test_an_expired_designation_does_not_block_a_successor(db: SyncDb) -> None:
    db.run(
        _with_page(
            {"effective_from": START, "effective_to": datetime(2026, 11, 10, tzinfo=UTC)},
            {"effective_from": datetime(2026, 11, 20, tzinfo=UTC), "tier": 1},
        )
    )
    assert _count(db) == 2


def test_a_cancelled_designation_blocks_nothing(db: SyncDb) -> None:
    """Retired at or before it began: an EMPTY interval, in force at no instant."""
    db.run(
        _with_page(
            {"effective_from": datetime(2026, 12, 1, tzinfo=UTC), "retired_at": START},
            {"effective_from": START, "effective_to": None, "tier": 1},
        )
    )
    assert _count(db) == 2


def test_a_retired_designation_keeps_its_row_beside_its_replacement(db: SyncDb) -> None:
    """Retire-and-replace is two rows, not a destructive edit."""
    handover = datetime(2026, 11, 19, tzinfo=UTC)
    db.run(
        _with_page(
            {"effective_from": START, "retired_at": handover, "tier": 3},
            {"effective_from": handover, "tier": 1},
        )
    )
    rows = db.run(
        lambda c: c.execute(
            text("SELECT tier, retired_at FROM page_priorities ORDER BY tier")
        ).all()
    )
    assert [(r.tier, r.retired_at) for r in rows] == [(1, None), (3, handover)]


def test_different_types_may_overlap_freely(db: SyncDb) -> None:
    """The rule is per (page, type): a page may be a revenue AND a trending priority at once."""
    db.run(
        _with_page(
            {"priority_type": PriorityType.HIGH_REVENUE_REPORT.value},
            {"priority_type": PriorityType.TRENDING_MARKET.value},
        )
    )
    assert _count(db) == 2


def test_different_pages_may_overlap_freely(db: SyncDb) -> None:
    other = uuid.UUID("00000000-0000-4000-8000-000000000132")

    def run(c: Connection) -> None:
        _page(c)
        _page(c, other)
        _designation(c)
        _designation(c, page_id=other)

    db.run(run)
    assert _count(db) == 2


def test_an_update_that_creates_an_overlap_is_rejected(db: SyncDb) -> None:
    """The guard covers UPDATE, not only INSERT."""
    boundary = datetime(2026, 12, 1, tzinfo=UTC)

    def run(c: Connection) -> None:
        _page(c)
        _designation(c, effective_from=START, effective_to=boundary)
        later = _designation(c, effective_from=boundary, effective_to=None, tier=1)
        c.execute(
            text("UPDATE page_priorities SET effective_from = :t WHERE id = :id"),
            {"t": START, "id": later},
        )

    error = _fails(db, run)
    assert _constraint(error) == "page_priorities_no_overlap"


# --------------------------------------------------------------------------- #
# The SQL validity interval, and its agreement with Python
# --------------------------------------------------------------------------- #
VALIDITY_CASES: Final = [
    ("open-ended", START, None, None),
    ("bounded", START, datetime(2026, 12, 1, tzinfo=UTC), None),
    ("retired later", START, None, datetime(2026, 11, 19, tzinfo=UTC)),
    ("retired before expiry", START, datetime(2026, 12, 1, tzinfo=UTC),
     datetime(2026, 11, 19, tzinfo=UTC)),
    ("retired after expiry", START, datetime(2026, 11, 10, tzinfo=UTC),
     datetime(2026, 11, 19, tzinfo=UTC)),
    ("cancelled before start", datetime(2026, 12, 1, tzinfo=UTC), None, START),
    ("retired at the start", START, None, START),
]  # fmt: skip


@pytest.mark.parametrize(("label", "efrom", "eto", "retired"), VALIDITY_CASES)
def test_the_sql_and_python_validity_intervals_agree(
    db: SyncDb, label: str, efrom: datetime, eto: datetime | None, retired: datetime | None
) -> None:
    """One arithmetic, two implementations: migration 0013's SQL and ``validity_end``.

    They are used independently -- the trigger decides what may be STORED, the resolver decides
    what a shadow report SEES -- so a divergence would let the database accept a pair the
    report then treats as overlapping, or the reverse.
    """
    lower, upper, empty = db.run(
        lambda c: c.execute(
            text(
                "SELECT lower(r), upper(r), isempty(r) FROM ken_priority_validity(:f, :t, :r) AS r"
            ),
            {"f": efrom, "t": eto, "r": retired},
        ).one()
    )
    designation = PriorityDesignation(
        id=uuid.uuid4(),
        page_id=PAGE,
        priority_type=PriorityType.HIGH_PRIORITY_REPORT,
        tier=1,
        reason="r",
        owner="o",
        effective_from=efrom,
        effective_to=eto,
        retired_at=retired,
        created_at=START,
        created_by="o",
        updated_at=START,
        updated_by="o",
    )

    assert designation.is_empty == empty, label
    if not empty:
        assert lower == designation.effective_from, label
        assert upper == designation.validity_end, label


# --------------------------------------------------------------------------- #
# Referential integrity
# --------------------------------------------------------------------------- #
def test_a_designation_for_an_unknown_page_is_refused(db: SyncDb) -> None:
    error = _fails(db, lambda c: _designation(c, page_id=uuid.uuid4()))
    assert "fk_page_priorities_page_id_pages" in str(error)


def test_the_page_foreign_key_is_on_delete_restrict(db: SyncDb) -> None:
    """RESTRICT: a designation is the record of an editorial decision, so deleting the page
    must not silently delete the evidence that it was prioritised.

    Asserted from the catalog, not by attempting a DELETE. A behavioural test cannot isolate
    this constraint: ``fk_page_versions_page_id_pages`` refuses the delete first, and the
    version row cannot be removed to get it out of the way because ``page_versions`` carries
    an immutability trigger. A DELETE test would therefore be refused with or without RESTRICT
    here. A first draft tried exactly that; it was caught only because it asserted the
    constraint NAME and the refusal came from ``fk_page_versions_page_id_pages`` instead.
    """
    action: str = db.run(
        lambda c: c.execute(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conname = 'fk_page_priorities_page_id_pages'"
            )
        ).scalar_one()
    )
    assert action == "r", "pg_constraint.confdeltype 'r' is ON DELETE RESTRICT"


# --------------------------------------------------------------------------- #
# Parity: the migration's frozen vocabulary IS the code's (no database needed)
# --------------------------------------------------------------------------- #
def test_the_migration_vocabulary_matches_the_code() -> None:
    """Migration 0013 holds a frozen copy of ``PriorityType`` and the tier bounds.

    If a tenth type is added to the enum without a migration widening the CHECK, the ORM
    would accept a value the database refuses. This fails first, in CI, instead of at the
    first real write. Mirrors ``test_the_trigger_transition_table_matches_allowed_transitions``.
    """
    import importlib.util
    from pathlib import Path

    from ken_core.priority import MAX_TIER, MIN_TIER

    path = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    spec = importlib.util.spec_from_file_location(
        "migration_0013", path / "0013_page_priorities.py"
    )
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert tuple(t.value for t in PriorityType) == module.PRIORITY_TYPES
    assert (module.MIN_TIER, module.MAX_TIER) == (MIN_TIER, MAX_TIER)
