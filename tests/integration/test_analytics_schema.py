"""Migration 0008 (ADR-0015 s2 as amended by s12) against the ``*_test`` database.

* structure: tables, the ONE shared writer guard, triggers, the exact ``link_events`` column
  set (no PII);
* ``link_events`` / ``link_metrics_daily`` / ``page_metrics_daily``: UPDATE always rejected;
  DELETE refused unless the SAME transaction ran ``set_config('ken.analytics_writer', 'on',
  true)``; the setting does not leak to the next transaction on a pooled connection;
* ``analytics_imports`` and ``link_measurements`` are append-only even for the writer;
* the CHECK constraints (minute-truncated ``received_at``, UTC ``event_date``, source sets,
  ledger consistency, bounded sample, position sum, non-empty measurement imports);
* ``dedupe_key`` makes ingest idempotent (``ON CONFLICT DO NOTHING``);
* ``verified -> measured`` is already an allowed transition (0007), unchanged here;
* an exact downgrade to 0007 and back.
"""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ken_contracts.analytics import (
    LINK_SOURCES,
    PAGE_SOURCES,
    AnalyticsSource,
    Dataset,
    ImportVia,
    LinkEventType,
    MatchMode,
)
from ken_contracts.recommendations import ALLOWED_TRANSITIONS, LinkPlanStatus
from ken_core.db.models.analytics import (
    APPEND_ONLY_ANALYTICS_TABLES,
    LINK_EVENT_COLUMNS,
    WRITER_GATED_TABLES,
)
from tests.integration.conftest import (
    ANALYTICS_FUNCTIONS,
    ANALYTICS_TABLES,
    ANALYTICS_TRIGGERS,
    LEARNING_FUNCTIONS,
    LEARNING_TABLES,
    LEARNING_TRIGGERS,
    SyncDb,
)
from tests.integration.test_review_schema import PLAN, SOURCE, _seed, _state

pytestmark = pytest.mark.integration

JOB = uuid.UUID("00000000-0000-4000-8000-000000000801")
IMPORT = uuid.UUID("00000000-0000-4000-8000-000000000802")
SHA = sha256(b"file").hexdigest()
WRITER_ON = "SELECT set_config('ken.analytics_writer', 'on', true)"


def _migration(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _job(c: Connection) -> None:
    c.execute(
        text(
            "INSERT INTO job_runs (id, kind, status, triggered_by) "
            "VALUES (:id, 'analytics_import', 'running', 'cli')"
        ),
        {"id": JOB},
    )


def _import(c: Connection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "id": IMPORT,
        "job": JOB,
        "source": "first_party",
        "dataset": "links",
        "via": "collector",
        "synthetic": True,
        "start": "2026-09-01",
        "end": "2026-09-03",
        "tz": "UTC",
        "file_name": None,
        "file_sha256": None,
        "rows": 0,
        "sample": "[]",
    } | overrides
    c.execute(
        text(
            "INSERT INTO analytics_imports (id, job_run_id, source, dataset, via, synthetic, "
            "date_start, date_end, source_timezone, file_name, file_sha256, rows_written, "
            "analytics_policy_version, unresolved_sample, triggered_by) VALUES (:id, :job, "
            ":source, :dataset, :via, :synthetic, :start, :end, "
            ":tz, :file_name, :file_sha256, :rows, 1, CAST(:sample AS jsonb), 'cli')"
        ),
        values | {"start": _d(values["start"]), "end": _d(values["end"])},
    )
    return uuid.UUID(str(values["id"]))


def _event(c: Connection, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "type": "internal_link_click",
        "plan": PLAN,
        "mode": "attr",
        "date": "2026-09-01",
        "at": "2026-09-01 23:59:00+00",
        "key": sha256(uuid.uuid4().bytes).hexdigest(),
    } | overrides
    return c.execute(
        text(
            "INSERT INTO link_events (event_type, plan_id, match_mode, synthetic, event_date, "
            "received_at, dedupe_key) VALUES (:type, :plan, :mode, true, :date, :at, :key) "
            "ON CONFLICT (dedupe_key) DO NOTHING"
        ),
        values | {"date": _d(values["date"]), "at": datetime.fromisoformat(values["at"])},
    ).rowcount


def _link_row(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "source": "first_party",
        "plan": PLAN,
        "imp": 10,
        "clk": 1,
    } | overrides
    c.execute(
        text(
            "INSERT INTO link_metrics_daily (source, metric_date, plan_id, impressions, clicks, "
            "import_id, synthetic) VALUES (:source, DATE '2026-09-01', :plan, :imp, :clk, "
            ":imp_id, true)"
        ),
        {"imp_id": IMPORT, **values},
    )


def _page_row(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "source": "search_console",
        "imp": 100,
        "clk": 5,
        "pos": 350.0,
        "pv": 0,
    } | overrides
    c.execute(
        text(
            "INSERT INTO page_metrics_daily (source, metric_date, page_id, impressions, clicks, "
            "position_sum, pageviews, sessions, import_id, synthetic) VALUES (:source, "
            "DATE '2026-09-01', :page, :imp, :clk, :pos, :pv, 0, :imp_id, true)"
        ),
        {"page": SOURCE, "imp_id": IMPORT, **values},
    )


def _measurement(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {"imports": json.dumps([str(IMPORT)])} | overrides
    c.execute(
        text(
            "INSERT INTO link_measurements (plan_id, analytics_policy_version, window_start, "
            "window_end, link_totals, target_comparison, source_comparison, flags, import_ids) "
            "VALUES (:plan, 1, DATE '2026-09-01', DATE '2026-09-29', '{}'::jsonb, '[]'::jsonb, "
            "'[]'::jsonb, '[]'::jsonb, CAST(:imports AS jsonb))"
        ),
        {"plan": PLAN, **values},
    )


def _seed_all(c: Connection) -> None:
    _seed(c)
    _job(c)
    _import(c)
    _event(c)
    _link_row(c)
    _page_row(c)


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    sync_db.run(_seed_all)
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


def _sqlstate(error: DBAPIError) -> str | None:
    origin: Any = error.orig
    for candidate in (origin, getattr(origin, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None)
        if isinstance(code, str):
            return code
    return None


def _count(db: SyncDb, table: str) -> int:
    return int(db.run(lambda c: c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()))  # noqa: S608


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_tables_guard_function_and_triggers(db: SyncDb) -> None:
    state = db.run(_state)
    assert state["tables"] >= ANALYTICS_TABLES
    assert state["functions"] >= ANALYTICS_FUNCTIONS
    assert state["triggers"] >= ANALYTICS_TRIGGERS
    assert set(WRITER_GATED_TABLES) | set(APPEND_ONLY_ANALYTICS_TABLES) == ANALYTICS_TABLES

    def _guards(c: Connection) -> dict[str, set[str]]:
        rows = c.execute(
            text(
                "SELECT event_object_table, action_statement FROM information_schema.triggers "
                "WHERE event_object_table = ANY(:t)"
            ),
            {"t": sorted(ANALYTICS_TABLES)},
        ).all()
        out: dict[str, set[str]] = {}
        for table, statement in rows:
            out.setdefault(str(table), set()).add(str(statement))
        return out

    guards = db.run(_guards)
    for table in WRITER_GATED_TABLES:
        assert guards[table] == {"EXECUTE FUNCTION ken_analytics_writer_guard()"}
    for table in APPEND_ONLY_ANALYTICS_TABLES:
        assert guards[table] == {"EXECUTE FUNCTION ken_reject_mutation()"}


def test_link_events_has_exactly_the_pseudonymous_columns(db: SyncDb) -> None:
    columns = db.run(lambda c: {col["name"] for col in inspect(c).get_columns("link_events")})
    assert (
        columns
        == LINK_EVENT_COLUMNS
        == {
            "id",
            "event_type",
            "plan_id",
            "match_mode",
            "synthetic",
            "event_date",
            "received_at",
            "dedupe_key",
        }
    )
    forbidden = ("ip", "ua", "agent", "pv", "referrer", "referer", "query", "cookie", "user")
    assert not any(part in forbidden for name in columns for part in name.split("_"))
    assert not any(name.startswith("experiment") for name in columns)


def test_measured_is_reachable_only_from_verified_and_0008_leaves_transitions_alone() -> None:
    pairs = _migration("0007_controlled_deployment").PLAN_TRANSITIONS
    assert ("verified", "measured") in pairs
    assert [a for a, b in pairs if b == "measured"] == ["verified"]
    assert LinkPlanStatus.MEASURED in ALLOWED_TRANSITIONS[LinkPlanStatus.VERIFIED]
    source = (
        Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0008_analytics.py"
    ).read_text()
    assert "ken_link_plan_status_guard" not in source
    assert "link_plans_status_integrity" not in source


# --------------------------------------------------------------------------- #
# The writer gate
# --------------------------------------------------------------------------- #
_UPDATES = {
    "link_events": "UPDATE link_events SET match_mode = 'href'",
    "link_metrics_daily": "UPDATE link_metrics_daily SET clicks = clicks + 1",
    "page_metrics_daily": "UPDATE page_metrics_daily SET clicks = clicks + 1",
}


@pytest.mark.parametrize("table", WRITER_GATED_TABLES)
@pytest.mark.parametrize("writer", [False, True])
def test_update_is_always_rejected(db: SyncDb, table: str, writer: bool) -> None:
    def _go(c: Connection) -> None:
        if writer:
            c.execute(text(WRITER_ON))
        c.execute(text(_UPDATES[table]))

    error = _fails(db, _go)
    assert _sqlstate(error) == "55000"
    assert "append-only outside the analytics writer" in str(error.orig)


@pytest.mark.parametrize("table", WRITER_GATED_TABLES)
def test_delete_needs_the_writer_setting_in_the_same_transaction(db: SyncDb, table: str) -> None:
    error = _fails(db, lambda c: c.execute(text(f"DELETE FROM {table}")))  # noqa: S608
    assert _sqlstate(error) == "55000"
    assert _count(db, table) == 1

    def _session_level_off(c: Connection) -> None:
        c.execute(text("SELECT set_config('ken.analytics_writer', 'off', true)"))
        c.execute(text(f"DELETE FROM {table}"))  # noqa: S608

    assert _sqlstate(_fails(db, _session_level_off)) == "55000"

    def _writer(c: Connection) -> None:
        c.execute(text(WRITER_ON))
        c.execute(text(f"DELETE FROM {table}"))  # noqa: S608

    db.run(_writer)
    assert _count(db, table) == 0


async def test_the_writer_setting_never_leaks_to_the_next_transaction(
    db: SyncDb, test_database_url: str
) -> None:
    """One pooled connection (pool_size=1): the setting made in transaction 1 is gone in
    transaction 2 on the SAME backend."""
    engine: AsyncEngine = create_async_engine(test_database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            pid_1: Any = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await conn.execute(text(WRITER_ON))
            await conn.execute(text("DELETE FROM link_events"))
            assert (
                await conn.execute(text("SELECT current_setting('ken.analytics_writer', true)"))
            ).scalar_one() == "on"
        async with engine.connect() as conn:
            pid_2: Any = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert pid_2 == pid_1
            setting: Any = (
                await conn.execute(text("SELECT current_setting('ken.analytics_writer', true)"))
            ).scalar_one()
            assert setting in ("", None)
            await conn.rollback()
            with pytest.raises(DBAPIError) as info:
                async with conn.begin():
                    await conn.execute(text("DELETE FROM link_metrics_daily"))
            assert _sqlstate(info.value) == "55000"
        async with engine.connect() as conn:
            events: Any = (
                await conn.execute(text("SELECT count(*) FROM link_events"))
            ).scalar_one()
            daily: Any = (
                await conn.execute(text("SELECT count(*) FROM link_metrics_daily"))
            ).scalar_one()
        assert (events, daily) == (0, 1)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", APPEND_ONLY_ANALYTICS_TABLES)
@pytest.mark.parametrize(
    "statement", ["UPDATE {t} SET analytics_policy_version = 2", "DELETE FROM {t}"]
)
def test_ledger_and_measurements_are_append_only_even_for_the_writer(
    db: SyncDb, table: str, statement: str
) -> None:
    db.run(_measurement)

    def _go(c: Connection) -> None:
        c.execute(text(WRITER_ON))
        c.execute(text(statement.format(t=table)))

    error = _fails(db, _go)
    assert _sqlstate(error) == "55000"
    assert "append-only" in str(error.orig)


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #
def test_enum_checks_match_the_contract_vocabulary(db: SyncDb) -> None:
    """PostgreSQL reports the alphabetically first violated CHECK, and an invalid source also
    violates the dataset/source pairing checks -- so the vocabulary CHECKs are compared with the
    contract enums through the catalog instead."""

    def _defs(c: Connection) -> dict[str, str]:
        rows = c.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype = 'c' AND conrelid = ANY(ARRAY['analytics_imports'::regclass, "
                "'link_events'::regclass, 'link_metrics_daily'::regclass, "
                "'page_metrics_daily'::regclass])"
            )
        ).all()
        return {str(name): str(definition) for name, definition in rows}

    defs = db.run(_defs)
    expected = {
        "ck_analytics_imports_source_valid": [s.value for s in AnalyticsSource],
        "ck_analytics_imports_dataset_valid": [d.value for d in Dataset],
        "ck_analytics_imports_via_valid": [v.value for v in ImportVia],
        "ck_link_events_event_type_valid": [e.value for e in LinkEventType],
        "ck_link_events_match_mode_valid": [m.value for m in MatchMode],
        "ck_link_metrics_daily_source_valid": sorted(s.value for s in LINK_SOURCES),
        "ck_page_metrics_daily_source_valid": sorted(s.value for s in PAGE_SOURCES),
    }
    for name, values in expected.items():
        found = re.findall(r"'([a-z0-9_]+)'::text", defs[name])
        assert sorted(found) == sorted(values), name


def test_dedupe_key_makes_ingest_idempotent(db: SyncDb) -> None:
    key = sha256(b"pv|clk|plan").hexdigest()
    assert db.run(lambda c: _event(c, key=key)) == 1
    assert db.run(lambda c: _event(c, key=key)) == 0
    assert _count(db, "link_events") == 2


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"at": "2026-09-01 10:00:30+00"}, "ck_link_events_received_at_minute"),
        ({"at": "2026-09-01 10:00:00.5+00"}, "ck_link_events_received_at_minute"),
        (
            {"at": "2026-09-02 00:30:00+05:30", "date": "2026-09-02"},
            "ck_link_events_event_date_is_utc",
        ),
        ({"date": "2026-09-02"}, "ck_link_events_event_date_is_utc"),
        ({"type": "internal_link_landing"}, "ck_link_events_event_type_valid"),
        ({"mode": "css"}, "ck_link_events_match_mode_valid"),
        ({"key": "ABC"}, "ck_link_events_dedupe_key_sha256"),
    ],
)
def test_link_event_checks(db: SyncDb, overrides: dict[str, Any], constraint: str) -> None:
    assert constraint in str(_fails(db, lambda c: _event(c, **overrides)).orig)


def test_received_at_minute_check_is_independent_of_the_session_time_zone(db: SyncDb) -> None:
    def _go(c: Connection) -> int:
        c.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))
        return int(_event(c, at="2026-09-01 18:59:00+00", date="2026-09-01"))

    assert db.run(_go) == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"source": "search_console", "via": "csv", "file_name": "a.csv", "file_sha256": SHA},
         "ck_analytics_imports_link_dataset_source"),
        ({"source": "first_party", "dataset": "pages", "via": "csv", "file_name": "a.csv",
          "file_sha256": SHA}, "ck_analytics_imports_page_dataset_source"),
        ({"source": "ga4"}, "ck_analytics_imports_collector_is_first_party_links"),
        ({"via": "api"}, "ck_analytics_imports_api_is_google"),
        ({"via": "synthetic", "synthetic": False}, "ck_analytics_imports_synthetic_via_is_synthetic"),  # noqa: E501
        ({"end": "2026-08-31"}, "ck_analytics_imports_date_range_ordered"),
        ({"start": "2025-08-01", "end": "2026-09-05"}, "ck_analytics_imports_date_range_bounded"),
        ({"tz": ""}, "ck_analytics_imports_source_timezone_bounded"),
        ({"via": "csv"}, "ck_analytics_imports_file_sha256_iff_csv"),
        ({"file_name": "a.csv", "file_sha256": SHA}, "ck_analytics_imports_file_sha256_iff_csv"),
        ({"via": "csv", "file_sha256": SHA}, "ck_analytics_imports_file_name_iff_sha256"),
        ({"via": "csv", "file_name": "a.csv", "file_sha256": "XYZ"},
         "ck_analytics_imports_file_sha256_hex"),
        ({"via": "csv", "file_name": "dir/a.csv", "file_sha256": SHA},
         "ck_analytics_imports_file_name_is_basename"),
        ({"via": "csv", "file_name": "dir\\a.csv", "file_sha256": SHA},
         "ck_analytics_imports_file_name_is_basename"),
        ({"via": "csv", "file_name": "..", "file_sha256": SHA},
         "ck_analytics_imports_file_name_is_basename"),
        ({"rows": -1}, "ck_analytics_imports_rows_written_non_negative"),
        ({"sample": "{}"}, "ck_analytics_imports_unresolved_sample_bounded_array"),
        ({"sample": json.dumps([f"/{i}" for i in range(101)])},
         "ck_analytics_imports_unresolved_sample_bounded_array"),
    ],
)  # fmt: skip
def test_ledger_checks(db: SyncDb, overrides: dict[str, Any], constraint: str) -> None:
    error = _fails(db, lambda c: _import(c, id=uuid.uuid4(), **overrides))
    assert constraint in str(error.orig)


def test_valid_ledger_variants(db: SyncDb) -> None:
    def _go(c: Connection) -> None:
        _import(c, id=uuid.uuid4(), source="ga4", dataset="pages", via="api", synthetic=False)
        _import(
            c,
            id=uuid.uuid4(),
            source="first_party",
            via="csv",
            synthetic=False,
            file_name="ken_link_daily.csv",
            file_sha256=SHA,
            start="2025-08-01",
            end="2026-09-04",  # exactly 400 days, inclusive
        )
        _import(c, id=uuid.uuid4(), source="search_console", dataset="pages", via="synthetic")

    db.run(_go)
    assert _count(db, "analytics_imports") == 4


@pytest.mark.parametrize(
    ("fn", "constraint"),
    [
        (lambda c: _link_row(c, source="search_console"), "ck_link_metrics_daily_source_valid"),
        (lambda c: _link_row(c, source="ga4", clk=-1), "ck_link_metrics_daily_counts_non_negative"),
        (lambda c: _page_row(c, source="first_party"), "ck_page_metrics_daily_source_valid"),
        (lambda c: _page_row(c, source="ga4", pv=-1), "ck_page_metrics_daily_counts_non_negative"),
        (lambda c: _page_row(c, source="ga4", imp=0, pos=1.0), "ck_page_metrics_daily_position_sum_valid"),  # noqa: E501
        (lambda c: _page_row(c, source="ga4", pos=-1.0), "ck_page_metrics_daily_position_sum_valid"),  # noqa: E501
        (lambda c: _measurement(c, imports="[]"), "ck_link_measurements_import_ids_non_empty_array"),  # noqa: E501
    ],
)  # fmt: skip
def test_daily_and_measurement_checks(
    db: SyncDb, fn: Callable[[Connection], Any], constraint: str
) -> None:
    assert constraint in str(_fails(db, fn).orig)


def test_primary_keys_and_foreign_keys(db: SyncDb) -> None:
    assert _sqlstate(_fails(db, _link_row)) == "23505"
    assert _sqlstate(_fails(db, lambda c: _link_row(c, plan=uuid.uuid4()))) == "23503"
    assert _sqlstate(_fails(db, lambda c: _import(c, id=uuid.uuid4(), job=uuid.uuid4()))) == "23503"
    db.run(_measurement)
    assert _sqlstate(_fails(db, _measurement)) == "23505"


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def test_downgrade_to_0007_restores_exactly_the_deployment_schema(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    head = sync_db.run(_state)
    command.downgrade(alembic_config, "0007_controlled_deployment")
    try:
        state = sync_db.run(_state)
        # A downgrade below 0008 also takes the Phase 8 learning schema with it (0009).
        assert state["tables"] == head["tables"] - ANALYTICS_TABLES - LEARNING_TABLES
        assert state["functions"] == head["functions"] - ANALYTICS_FUNCTIONS - LEARNING_FUNCTIONS
        assert state["triggers"] == head["triggers"] - ANALYTICS_TRIGGERS - LEARNING_TRIGGERS
        assert state["revision"] == "0007_controlled_deployment"
    finally:
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert state == head
    assert state["revision"] == "0009_learning"
