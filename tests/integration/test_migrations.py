"""Migrations 0001-0009 against a real PostgreSQL (the ``*_test`` database)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.dialects.postgresql import base as postgresql_base

import ken_core.db.models  # noqa: F401 -- registers every model
from ken_core.db.base import Base
from ken_core.db.types import Xid8
from tests.integration.conftest import TRUNCATE_ALL, SyncDb

pytestmark = pytest.mark.integration

APP_TABLES = frozenset(
    {
        "events",
        "ingestion_runs",
        "ingestion_issues",
        "pages",
        "page_versions",
        "existing_links",
        "known_urls",
        "job_runs",
        "page_analyses",
        "page_embeddings",
        "page_relationships",
        "page_protections",
        "link_plans",
        "link_plan_transitions",
        "link_candidates",
        "opportunities",
        "agent_runs",
        "agent_steps",
        "agent_run_plans",
        "users",
        "auth_sessions",
        "review_actions",
        "deployments",
        "deployment_items",
        "deployment_verifications",
        "analytics_imports",
        "link_events",
        "link_metrics_daily",
        "page_metrics_daily",
        "link_measurements",
        "learning_proposals",
        "learning_proposal_transitions",
        "policy_candidates",
        "skills",
        "policy_evaluations",
        "experiments",
        "experiment_assignments",
        "page_graph_metrics",
        "autonomy_decisions",
        "publish_attempts",
    }
)


def _tables(db: SyncDb) -> set[str]:
    return db.run(lambda c: set(inspect(c).get_table_names()))


def _extensions(db: SyncDb) -> set[str]:
    return db.run(lambda c: set(c.execute(text("SELECT extname FROM pg_extension")).scalars()))


def _revision(db: SyncDb) -> str | None:
    return db.run(lambda c: MigrationContext.configure(c).get_current_revision())


def test_upgrade_downgrade_upgrade_round_trip(alembic_config: Config, sync_db: SyncDb) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    assert head == "0010_qdrant_and_autonomy"

    command.upgrade(alembic_config, "head")
    assert _tables(sync_db) >= APP_TABLES
    assert _revision(sync_db) == head

    # This test is about the schema. Rows left by earlier tests (job_runs of a kind that the
    # older revision's CHECK does not allow, such as vector_index) would make the downgrade
    # refuse, which is the right behaviour for real data and the wrong input for this test.
    sync_db.run(lambda connection: connection.execute(text(TRUNCATE_ALL)))

    command.downgrade(alembic_config, "base")
    assert not APP_TABLES & _tables(sync_db)
    assert _revision(sync_db) is None
    assert "vector" not in _extensions(sync_db)

    command.upgrade(alembic_config, "head")
    assert _tables(sync_db) >= APP_TABLES
    assert _revision(sync_db) == head

    # ADR-0017: no migration creates the vector extension any more, at any revision.
    assert "vector" not in _extensions(sync_db)


# Alembic compares a Computed column's expression only as crude TEXT (PostgreSQL
# re-renders it as "left"(COALESCE(title, ''::text), 2000) ...) and can only warn.
# test_recommendations_schema checks page_versions.search_vector SEMANTICALLY.
@pytest.mark.filterwarnings("ignore:Computed default on page_versions.search_vector")
def test_orm_matches_migration(
    alembic_config: Config, sync_db: SyncDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(alembic_config, "head")
    # ADR-0017: page_embeddings.embedding is a plain real[], which reflection already
    # understands, so no type needs teaching. xid8 still does.
    monkeypatch.setitem(postgresql_base.ischema_names, "xid8", Xid8)

    def _diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return list(compare_metadata(context, Base.metadata))

    assert sync_db.run(_diff) == []


def test_constraint_names_match_naming_convention(alembic_config: Config, sync_db: SyncDb) -> None:
    """Autogenerate does not compare CHECK constraints, so assert them directly."""
    command.upgrade(alembic_config, "head")
    table = Base.metadata.tables["events"]

    def _reflect(
        connection: Connection,
    ) -> tuple[object, set[str], set[str], list[dict[str, object]]]:
        inspector = inspect(connection)
        return (
            inspector.get_pk_constraint("events")["name"],
            {str(c["name"]) for c in inspector.get_unique_constraints("events")},
            {str(c["name"]) for c in inspector.get_check_constraints("events")},
            [dict(i) for i in inspector.get_indexes("events")],
        )

    pk_name, uniques, checks, indexes = sync_db.run(_reflect)
    assert pk_name == table.primary_key.name
    assert uniques == {"uq_events_event_id"}
    assert checks == {"ck_events_event_version_positive"}
    # PostgreSQL backs a UNIQUE constraint with an index; that one is not ours.
    assert {i["name"] for i in indexes if not i.get("duplicates_constraint")} == {
        str(index.name) for index in table.indexes
    }


def test_identity_is_generated_always(alembic_config: Config, sync_db: SyncDb) -> None:
    command.upgrade(alembic_config, "head")
    identity = sync_db.run(
        lambda c: tuple(
            c.execute(
                text(
                    "SELECT is_identity, identity_generation FROM information_schema.columns "
                    "WHERE table_name = 'events' AND column_name = 'id'"
                )
            ).one()
        )
    )
    assert identity == ("YES", "ALWAYS")


@pytest.mark.parametrize("argv_form", ["split", "single_word"])
def test_dburl_is_honoured_however_the_shell_passes_it(argv_form: str) -> None:
    """Phase 2 incident: '-x dburl=...' as ONE argv word used to fall back to the dev DB."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    target = "postgresql+asyncpg://ken:ken@localhost:5433/ken_no_such_database_test"
    args = ["-x", f"dburl={target}"] if argv_form == "split" else [f"-x dburl={target}"]
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), *args, "current"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=root,
    )
    # It must try the named (non-existent) database, never fall back to KEN_DATABASE_URL.
    assert result.returncode != 0
    assert "ken_no_such_database_test" in result.stderr or "does not exist" in result.stderr


def test_unknown_x_argument_is_refused() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(root / "alembic.ini"),
            "-x",
            "db_url=x",
            "current",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=root,
    )
    assert result.returncode != 0
    assert "unknown -x argument" in result.stderr + result.stdout


@pytest.mark.parametrize("value", ["", " "])
def test_empty_dburl_is_refused(value: str) -> None:
    """QA B1: '-x dburl=' with an empty value (e.g. an unset shell variable) must not fall back."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(root / "alembic.ini"),
            "-x",
            f"dburl={value}",
            "current",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=root,
    )
    assert result.returncode != 0
    assert "dburl= is empty" in result.stderr + result.stdout
