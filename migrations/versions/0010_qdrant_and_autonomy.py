"""Phase 9: pgvector removal, link-graph metrics, autonomy decisions, publish attempts.

Revision ID: 0010_qdrant_and_autonomy
Revises: 0009_learning
Create Date: 2026-10-16

Column-for-column with ``ken_core.db.models.autonomy`` and the new
``page_graph_metrics`` / ``link_candidates`` columns of
``ken_core.db.models.intelligence`` / ``.recommendations``. Design:
docs/adr/ADR-0017-qdrant-vector-architecture.md (which **supersedes ADR-0003 / ADR-0010's
pgvector choice and overrides CLAUDE.md #5**) and
docs/adr/ADR-0018-autonomy-first-publishing.md (which **supersedes the human-approval
workflow ADR-0013 section 1 made the normal path**).

**1. pgvector is removed, not bypassed (ADR-0017 section 2).**

``page_embeddings.embedding`` becomes a plain ``real[]``, keeping every value: pgvector
stores float4 internally, so ``real[]`` is lossless, and the dimension CHECK moves from
``vector_dims(embedding)`` to ``array_length(embedding, 1)``. The conversion is GUARDED by
the column's current type, because migration 0003 was amended to create ``real[]`` directly
(see its docstring): on a fresh database there is nothing to convert and this step is a no-op,
while a database already at 0003..0009 still holds a ``vector`` column and is converted here.
Both paths end at exactly the same schema, which is what keeps ``alembic check`` honest.

``ALTER TABLE ... ALTER COLUMN TYPE`` is DDL, so the append-only row trigger on
``page_embeddings`` does not fire and the immutability rule is untouched.

**2. New: ``page_graph_metrics``** (completion directive section 5) -- per page VERSION:
degrees, internal PageRank, hub/authority, crawl depth, the orphan / nearly-orphan / bridge
flags, the component and cluster ordinals, the recipe version and when it was computed.
RECOMPUTABLE BY REPLACE, not append-only: see the model docstring for why (every value is a
pure function of the current graph, and a run touches the whole corpus, so history would cost
one corpus per run and buy nothing an audit could use). Identity is
``(page_id, page_version, metrics_version)``, so two recipe versions coexist for a comparison
and a published number stays reproducible.

**3. New: ``autonomy_decisions``** (ADR-0018 section 6) -- APPEND-ONLY, one row per
evaluation: decision, mode, risk tier, confidence (with its calibration version, NULL meaning
RAW), critic outcome, policy version, the deterministic block codes that FIRED as a jsonb
array, rollback availability, the verification profile, and -- in ``shadow`` -- the
counterfactual. CHECKs make the dangerous combinations unrepresentable rather than merely
discouraged: a fired block forces ``block``; a publish requires ``rollback_available``; a
``shadow`` row can never be a publish and must carry a counterfactual; the strict profile is
exactly the extra-verification decision; the actor is never an ``agent:`` (ADR-0012 stands);
and a ``user:`` actor must record a reason.

It carries ``txid`` plus the DEFERRED ``ken_autonomy_decision_guard()``, the 0007 / 0009
pattern: whenever a decision row claims to have moved the plan, a matching
``link_plan_transitions`` row must have been written **by the same transaction**. Without it
the CHECKs test one row's SHAPE only, so a caller could insert the decision in one
transaction and move the plan in another and nothing would notice.

**4. New: ``publish_attempts``** (ADR-0018 section 4) -- APPEND-ONLY, the retry budget and
cool-down per (source, target). A UNIQUE index on (source, target, attempt) is what makes the
budget unspendable twice by a race: the second INSERT of the same attempt number is REFUSED,
not merged. A separate table rather than columns on ``deployments`` because the budget belongs
to the PAIR and must outlive every plan and deployment that ever touched it.

**5. The frozen literal enum copies older migrations hold** (the trap the Phase 8 freeze
found) are all replaced here, or the first write of a new value would fail a CHECK:

* 0004's ``ck_link_plans_status_valid`` and ``ck_link_plan_transitions_{to,from}_status_valid``
  -- ``blocked`` and ``autonomy_decided`` are new ``LinkPlanStatus`` members;
* 0006's ``ck_review_actions_{from,to,new_plan}_status_valid`` -- same enum, second copy;
* 0004's partial unique indexes ``uq_link_plans_open_pair`` / ``uq_link_plans_open_span``,
  whose predicate is a literal copy of ``OPEN_STATUSES`` and must now include
  ``autonomy_decided`` (``blocked`` is deliberately NOT open: it would hold the pair and a
  slice of the target's diversity budget for ever);
* 0007's ``ken_link_plan_status_guard()``, whose body is a literal copy of
  ``ALLOWED_TRANSITIONS``;
* 0003's ``ck_job_runs_kind_valid`` -- ``autonomy``, ``graph_metrics`` and ``vector_index``
  are new ``JobKind`` members (0009 replaced the same CHECK for ``reflection``).

``link_candidates`` also gains ``attribution`` and ``graph_signals`` (jsonb), so the
per-channel audit trail and the target's graph position are stored per candidate instead of a
single opaque score.

Known limitation, stated plainly: every guard here is a trigger, so they defend against
application bugs and not against a DB superuser (``session_replication_role = replica``
disables all of them). No ``SECURITY DEFINER`` function exists in this schema.

Downgrade restores the 0003/0004/0006/0007 CHECKs, indexes and guard exactly, drops the three
new tables and the two new columns, and converts ``page_embeddings.embedding`` back to a
pgvector ``vector`` ONLY if the extension is available (it recreates nothing: a database whose
0003 ran after this amendment has no extension, and the column correctly stays ``real[]``).
It therefore returns to exactly the 0009 state for the database it is run against.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_qdrant_and_autonomy"
down_revision: str | Sequence[str] | None = "0009_learning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES: Final = ("autonomy_decisions", "publish_attempts")

# --------------------------------------------------------------------------- #
# Frozen literal copies of the contract enums AT THIS REVISION.
# --------------------------------------------------------------------------- #
JOB_KINDS_0009: Final[tuple[str, ...]] = (
    "analysis",
    "recommendation",
    "agent",
    "verification",
    "analytics_import",
    "evaluation",
    "autonomy_shadow",
    "reflection",
)
"""``ck_job_runs_kind_valid`` as migration 0009 left it."""
JOB_KINDS_0010: Final[tuple[str, ...]] = (
    *JOB_KINDS_0009,
    "autonomy",
    "graph_metrics",
    "vector_index",
)
"""Phase 9 adds three kinds, so the CHECK is REPLACED here. ``JobKind`` is a Pydantic enum,
but ``job_runs.kind`` carries a frozen literal copy of it in the database."""

PLAN_STATUSES_0009: Final[tuple[str, ...]] = (
    "proposed",
    "critiqued",
    "policy_checked",
    "awaiting_approval",
    "approved",
    "rejected",
    "superseded",
    "validated",
    "deployed",
    "verified",
    "failed",
    "rolled_back",
    "expired",
    "measured",
    "learning_captured",
)
"""``LinkPlanStatus`` as migrations 0004 and 0006 wrote it."""
PLAN_STATUSES_0010: Final[tuple[str, ...]] = (
    "proposed",
    "critiqued",
    "policy_checked",
    "autonomy_decided",
    "blocked",
    "awaiting_approval",
    "approved",
    "rejected",
    "superseded",
    "validated",
    "deployed",
    "verified",
    "failed",
    "rolled_back",
    "expired",
    "measured",
    "learning_captured",
)
"""Literal copy of ``ken_contracts.recommendations.LinkPlanStatus`` at this revision."""

OPEN_STATUSES_0009: Final[tuple[str, ...]] = (
    "proposed",
    "critiqued",
    "policy_checked",
    "awaiting_approval",
    "approved",
    "validated",
)
OPEN_STATUSES_0010: Final[tuple[str, ...]] = (
    "proposed",
    "critiqued",
    "policy_checked",
    "autonomy_decided",
    "awaiting_approval",
    "approved",
    "validated",
)
"""Literal copy of ``OPEN_STATUSES``: a plan in one of these occupies its (source, target)
pair and its anchor span. ``blocked`` is absent deliberately (ADR-0018 / the contract)."""

PLAN_TRANSITIONS_0007: Final[tuple[tuple[str, str], ...]] = (
    ("proposed", "critiqued"),
    ("proposed", "policy_checked"),
    ("proposed", "awaiting_approval"),
    ("proposed", "rejected"),
    ("proposed", "superseded"),
    ("proposed", "expired"),
    ("critiqued", "policy_checked"),
    ("critiqued", "awaiting_approval"),
    ("critiqued", "rejected"),
    ("critiqued", "superseded"),
    ("critiqued", "expired"),
    ("policy_checked", "awaiting_approval"),
    ("policy_checked", "rejected"),
    ("policy_checked", "superseded"),
    ("policy_checked", "expired"),
    ("awaiting_approval", "approved"),
    ("awaiting_approval", "rejected"),
    ("awaiting_approval", "superseded"),
    ("awaiting_approval", "expired"),
    ("approved", "validated"),
    ("approved", "rejected"),
    ("approved", "superseded"),
    ("approved", "expired"),
    ("validated", "deployed"),
    ("validated", "failed"),
    ("validated", "expired"),
    ("deployed", "verified"),
    ("deployed", "failed"),
    ("deployed", "rolled_back"),
    ("verified", "measured"),
    ("verified", "rolled_back"),
    ("failed", "rolled_back"),
    ("measured", "learning_captured"),
    ("measured", "rolled_back"),
    ("learning_captured", "rolled_back"),
)
"""``ken_link_plan_status_guard()`` as migration 0007 wrote it (restored on downgrade)."""

PLAN_TRANSITIONS_0010: Final[tuple[tuple[str, str], ...]] = (
    ("proposed", "critiqued"),
    ("proposed", "policy_checked"),
    ("proposed", "awaiting_approval"),
    ("proposed", "blocked"),
    ("proposed", "rejected"),
    ("proposed", "superseded"),
    ("proposed", "expired"),
    ("critiqued", "policy_checked"),
    ("critiqued", "awaiting_approval"),
    ("critiqued", "blocked"),
    ("critiqued", "rejected"),
    ("critiqued", "superseded"),
    ("critiqued", "expired"),
    ("policy_checked", "autonomy_decided"),
    ("policy_checked", "awaiting_approval"),
    ("policy_checked", "blocked"),
    ("policy_checked", "rejected"),
    ("policy_checked", "superseded"),
    ("policy_checked", "expired"),
    ("autonomy_decided", "validated"),
    ("autonomy_decided", "awaiting_approval"),
    ("autonomy_decided", "blocked"),
    ("autonomy_decided", "rejected"),
    ("autonomy_decided", "superseded"),
    ("autonomy_decided", "expired"),
    ("blocked", "autonomy_decided"),
    ("blocked", "superseded"),
    ("blocked", "expired"),
    ("awaiting_approval", "approved"),
    ("awaiting_approval", "blocked"),
    ("awaiting_approval", "rejected"),
    ("awaiting_approval", "superseded"),
    ("awaiting_approval", "expired"),
    ("approved", "validated"),
    ("approved", "blocked"),
    ("approved", "rejected"),
    ("approved", "superseded"),
    ("approved", "expired"),
    ("validated", "deployed"),
    ("validated", "failed"),
    ("validated", "expired"),
    ("deployed", "verified"),
    ("deployed", "failed"),
    ("deployed", "rolled_back"),
    ("verified", "measured"),
    ("verified", "rolled_back"),
    ("failed", "rolled_back"),
    ("measured", "learning_captured"),
    ("measured", "rolled_back"),
    ("learning_captured", "rolled_back"),
)
"""Literal copy of ``ken_contracts.recommendations.ALLOWED_TRANSITIONS`` at this revision
(parity-tested against the contract)."""

AUTONOMY_DECISIONS: Final[tuple[str, ...]] = (
    "block",
    "auto_publish",
    "auto_publish_with_extra_verification",
)
AUTONOMY_MODES: Final[tuple[str, ...]] = ("shadow", "guarded", "autonomous")
RISK_TIERS: Final[tuple[str, ...]] = ("low", "medium", "high")
CRITIC_OUTCOMES: Final[tuple[str, ...]] = ("accept", "reject", "no_action")
CRITIC_VERDICTS: Final[tuple[str, ...]] = ("accept", "reject", "uncertain")
VERIFICATION_PROFILES: Final[tuple[str, ...]] = ("standard", "strict")

ACTOR_PATTERN: Final = (
    r"^(user:\S{1,200}|agent:[a-z0-9_-]{1,64}@[0-9A-Za-z._-]{1,32}|system:[a-z0-9_-]{1,64})$"
)
"""Literal copy of ``ken_contracts.recommendations.ACTOR_PATTERN``."""

MAX_OVERRIDE_REASON_CHARS: Final = 500
MAX_ENGINE_VERSION_CHARS: Final = 64
MAX_FAILURE_CODE_CHARS: Final = 64
MAX_CALIBRATION_VERSION_CHARS: Final = 64
MAX_METRICS_VERSION_CHARS: Final = 64


# --------------------------------------------------------------------------- #
# Helpers (identical in spirit to 0008 / 0009)
# --------------------------------------------------------------------------- #
def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(name))


def _jsonb(name: str, *, default: str | None = None) -> sa.Column[Any]:
    return sa.Column(
        name,
        postgresql.JSONB(),
        server_default=sa.text(f"'{default}'::jsonb") if default is not None else None,
        nullable=False,
    )


def _pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return "ARRAY[" + ", ".join(f"'{a}>{b}'" for a, b in pairs) + "]::text[]"


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# --------------------------------------------------------------------------- #
# 1. page_embeddings.embedding: vector -> real[]
# --------------------------------------------------------------------------- #
def _convert_embedding_to_real_array() -> None:
    """Convert only if the column is STILL a pgvector ``vector`` (ADR-0017 section 2).

    pgvector renders a vector as ``'[0.1,0.2]'``, which is not a ``real[]`` literal, so the
    cast goes through ``string_to_array`` on the text form. float4 text output round-trips
    exactly, so no value changes. A fresh database created by the amended 0003 already has
    ``real[]`` and skips this entirely.
    """
    op.execute(
        """
        DO $$
        DECLARE
            column_type text;
        BEGIN
            SELECT format_type(a.atttypid, a.atttypmod) INTO column_type
            FROM pg_attribute a
            WHERE a.attrelid = 'public.page_embeddings'::regclass
              AND a.attname = 'embedding'
              AND NOT a.attisdropped;
            IF column_type IS NULL THEN
                RAISE EXCEPTION 'page_embeddings.embedding is missing';
            END IF;
            IF column_type <> 'real[]' THEN
                ALTER TABLE page_embeddings
                    DROP CONSTRAINT IF EXISTS ck_page_embeddings_dimensions_match;
                ALTER TABLE page_embeddings
                    ALTER COLUMN embedding TYPE real[]
                    USING string_to_array(trim(both '[]' from embedding::text), ',')::real[];
            END IF;
            ALTER TABLE page_embeddings
                DROP CONSTRAINT IF EXISTS ck_page_embeddings_dimensions_match;
            ALTER TABLE page_embeddings
                ADD CONSTRAINT ck_page_embeddings_dimensions_match
                CHECK (array_length(embedding, 1) = dimensions);
        END;
        $$
        """
    )


def _convert_embedding_to_vector() -> None:
    """Downgrade counterpart. Converts back ONLY when the extension is installed: a database
    whose 0003 never created it must stay ``real[]``, which is the correct 0009 state for it.
    """
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE page_embeddings
                DROP CONSTRAINT IF EXISTS ck_page_embeddings_dimensions_match;
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
                ALTER TABLE page_embeddings
                    ALTER COLUMN embedding TYPE vector
                    USING ('[' || array_to_string(embedding, ',') || ']')::vector;
                ALTER TABLE page_embeddings
                    ADD CONSTRAINT ck_page_embeddings_dimensions_match
                    CHECK (vector_dims(embedding) = dimensions);
            ELSE
                ALTER TABLE page_embeddings
                    ADD CONSTRAINT ck_page_embeddings_dimensions_match
                    CHECK (array_length(embedding, 1) = dimensions);
            END IF;
        END;
        $$
        """
    )


# --------------------------------------------------------------------------- #
# 5. Replacing the frozen literal enum copies
# --------------------------------------------------------------------------- #
def _replace_plan_status_checks(statuses: Sequence[str]) -> None:
    op.drop_constraint(op.f("ck_link_plans_status_valid"), "link_plans", type_="check")
    op.create_check_constraint(
        op.f("ck_link_plans_status_valid"), "link_plans", _one_of("status", statuses)
    )
    op.drop_constraint(
        op.f("ck_link_plan_transitions_to_status_valid"), "link_plan_transitions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_link_plan_transitions_to_status_valid"),
        "link_plan_transitions",
        _one_of("to_status", statuses),
    )
    op.drop_constraint(
        op.f("ck_link_plan_transitions_from_status_valid"),
        "link_plan_transitions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_link_plan_transitions_from_status_valid"),
        "link_plan_transitions",
        "from_status IS NULL OR " + _one_of("from_status", statuses),
    )
    # Second copy of the same enum, written by 0006.
    for column, name in (
        ("from_status", "ck_review_actions_from_status_valid"),
        ("to_status", "ck_review_actions_to_status_valid"),
    ):
        op.drop_constraint(op.f(name), "review_actions", type_="check")
        op.create_check_constraint(op.f(name), "review_actions", _one_of(column, statuses))
    op.drop_constraint(
        op.f("ck_review_actions_new_plan_status_valid"), "review_actions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_review_actions_new_plan_status_valid"),
        "review_actions",
        "new_plan_status IS NULL OR " + _one_of("new_plan_status", statuses),
    )


def _replace_open_plan_indexes(open_statuses: Sequence[str]) -> None:
    predicate = f"status IN ({_sql_list(open_statuses)})"
    op.drop_index(op.f("uq_link_plans_open_pair"), table_name="link_plans")
    op.create_index(
        op.f("uq_link_plans_open_pair"),
        "link_plans",
        ["source_page_id", "target_page_id"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )
    op.drop_index(op.f("uq_link_plans_open_span"), table_name="link_plans")
    op.create_index(
        op.f("uq_link_plans_open_span"),
        "link_plans",
        ["source_page_id", "context_hash", "start_offset"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def _replace_plan_status_guard(transitions: Sequence[tuple[str, str]]) -> None:
    """0007's guard, with the transition table swapped. The function is REPLACED rather than
    dropped so the constraint trigger that references it survives untouched."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ken_link_plan_status_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT ((OLD.status || '>' || NEW.status) = ANY ({_pairs(transitions)})) THEN
                RAISE EXCEPTION 'link_plans status % -> % is not an allowed transition',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM link_plan_transitions t
                WHERE t.plan_id = NEW.id
                  AND t.from_status = OLD.status
                  AND t.to_status = NEW.status
                  AND t.txid = pg_current_xact_id()
            ) THEN
                RAISE EXCEPTION
                    'link_plans status % -> % has no transition record in this transaction',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )


# --------------------------------------------------------------------------- #
def upgrade() -> None:
    # --------------------------------------------------- 1. pgvector removal
    _convert_embedding_to_real_array()

    # ------------------------------------------- 5. frozen literal copies
    op.drop_constraint(op.f("ck_job_runs_kind_valid"), "job_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_job_runs_kind_valid"), "job_runs", _one_of("kind", JOB_KINDS_0010)
    )
    _replace_plan_status_checks(PLAN_STATUSES_0010)
    _replace_open_plan_indexes(OPEN_STATUSES_0010)
    _replace_plan_status_guard(PLAN_TRANSITIONS_0010)

    # ----------------------------------- candidate attribution + graph signals
    # A non-volatile default, so PostgreSQL adds these without rewriting the table (and
    # therefore without firing the append-only row trigger).
    op.add_column(
        "link_candidates",
        _jsonb("attribution", default="[]"),
    )
    op.add_column(
        "link_candidates",
        _jsonb("graph_signals", default="{}"),
    )
    op.create_check_constraint(
        op.f("ck_link_candidates_attribution_array"),
        "link_candidates",
        "jsonb_typeof(attribution) = 'array'",
    )
    op.create_check_constraint(
        op.f("ck_link_candidates_graph_signals_object"),
        "link_candidates",
        "jsonb_typeof(graph_signals) = 'object'",
    )

    # -------------------------------------------------- 2. page_graph_metrics
    # op.f(): names below are final (the naming convention must not be re-applied).
    op.create_table(
        "page_graph_metrics",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("page_version", sa.Integer(), nullable=False),
        sa.Column("in_degree", sa.Integer(), nullable=False),
        sa.Column("out_degree", sa.Integer(), nullable=False),
        sa.Column("internal_pagerank", sa.Float(), nullable=False),
        sa.Column("hub_score", sa.Float(), nullable=False),
        sa.Column("authority_score", sa.Float(), nullable=False),
        sa.Column("crawl_depth", sa.Integer(), nullable=True),
        sa.Column("is_orphan", sa.Boolean(), nullable=False),
        sa.Column("is_nearly_orphan", sa.Boolean(), nullable=False),
        sa.Column("is_bridge", sa.Boolean(), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=True),
        sa.Column("cluster_id", sa.Integer(), nullable=True),
        sa.Column("metrics_version", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        _ts("computed_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_graph_metrics")),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["pages.id"],
            name=op.f("fk_page_graph_metrics_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            name=op.f("fk_page_graph_metrics_page_id_page_version_page_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["job_runs.id"],
            name=op.f("fk_page_graph_metrics_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "page_id",
            "page_version",
            "metrics_version",
            name=op.f("uq_page_graph_metrics_page_id_page_version_metrics_version"),
        ),
        _check("in_degree >= 0 AND out_degree >= 0", "ck_page_graph_metrics_degrees_non_negative"),
        _check(
            "internal_pagerank >= 0 AND internal_pagerank <= 1",
            "ck_page_graph_metrics_pagerank_range",
        ),
        _check("hub_score >= 0 AND hub_score <= 1", "ck_page_graph_metrics_hub_score_range"),
        _check(
            "authority_score >= 0 AND authority_score <= 1",
            "ck_page_graph_metrics_authority_score_range",
        ),
        _check("crawl_depth IS NULL OR crawl_depth >= 0", "ck_page_graph_metrics_crawl_depth_valid"),
        _check(
            "component_id IS NULL OR component_id >= 0",
            "ck_page_graph_metrics_component_id_valid",
        ),
        _check("cluster_id IS NULL OR cluster_id >= 0", "ck_page_graph_metrics_cluster_id_valid"),
        # The flag and the count can never disagree.
        _check("is_orphan = (in_degree = 0)", "ck_page_graph_metrics_orphan_iff_no_inbound"),
        _check("NOT is_orphan OR is_nearly_orphan", "ck_page_graph_metrics_orphan_implies_nearly"),
        _check(
            f"length(metrics_version) BETWEEN 1 AND {MAX_METRICS_VERSION_CHARS}",
            "ck_page_graph_metrics_metrics_version_bounded",
        ),
    )
    op.create_index(
        op.f("ix_page_graph_metrics_metrics_version_page_id"),
        "page_graph_metrics",
        ["metrics_version", "page_id"],
    )
    op.create_index(
        op.f("ix_page_graph_metrics_orphans"),
        "page_graph_metrics",
        ["metrics_version", "page_id"],
        postgresql_where=sa.text("is_orphan OR is_nearly_orphan"),
    )

    # ------------------------------------------------- 3. autonomy_decisions
    op.create_table(
        "autonomy_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("risk_tier", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("calibration_version", sa.Text(), nullable=True),
        sa.Column("critic_outcome", sa.Text(), nullable=False),
        sa.Column("critic_verdict", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        _jsonb("blocks", default="[]"),
        sa.Column("rollback_available", sa.Boolean(), nullable=False),
        sa.Column("verification_profile", sa.Text(), nullable=False),
        sa.Column("would_have_decision", sa.Text(), nullable=True),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("override_reason", sa.Text(), nullable=True),
        # txid is added below: xid8 has no SQLAlchemy type (0007 does the same).
        _ts("evaluated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autonomy_decisions")),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_autonomy_decisions_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_run_id"],
            ["job_runs.id"],
            name=op.f("fk_autonomy_decisions_job_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        _check(_one_of("decision", AUTONOMY_DECISIONS), "ck_autonomy_decisions_decision_valid"),
        _check(_one_of("mode", AUTONOMY_MODES), "ck_autonomy_decisions_mode_valid"),
        _check(_one_of("risk_tier", RISK_TIERS), "ck_autonomy_decisions_risk_tier_valid"),
        _check(
            _one_of("critic_outcome", CRITIC_OUTCOMES),
            "ck_autonomy_decisions_critic_outcome_valid",
        ),
        _check(
            "critic_verdict IS NULL OR " + _one_of("critic_verdict", CRITIC_VERDICTS),
            "ck_autonomy_decisions_critic_verdict_valid",
        ),
        _check(
            _one_of("verification_profile", VERIFICATION_PROFILES),
            "ck_autonomy_decisions_verification_profile_valid",
        ),
        _check(
            "would_have_decision IS NULL OR "
            + _one_of("would_have_decision", AUTONOMY_DECISIONS),
            "ck_autonomy_decisions_would_have_decision_valid",
        ),
        _check("confidence >= 0 AND confidence <= 1", "ck_autonomy_decisions_confidence_range"),
        _check("policy_version >= 1", "ck_autonomy_decisions_policy_version_positive"),
        _check("jsonb_typeof(blocks) = 'array'", "ck_autonomy_decisions_blocks_array"),
        # A fired block can never coexist with a publish.
        _check(
            "jsonb_array_length(blocks) = 0 OR decision = 'block'",
            "ck_autonomy_decisions_blocks_force_block",
        ),
        # Nothing is published that cannot be undone.
        _check(
            "rollback_available OR decision = 'block'",
            "ck_autonomy_decisions_no_publish_without_rollback",
        ),
        _check(
            "(decision = 'auto_publish_with_extra_verification') "
            "<= (verification_profile = 'strict')",
            "ck_autonomy_decisions_extra_verification_is_strict",
        ),
        # Shadow records a counterfactual and publishes nothing.
        _check(
            "(mode = 'shadow') = (would_have_decision IS NOT NULL)",
            "ck_autonomy_decisions_counterfactual_iff_shadow",
        ),
        _check(
            "mode <> 'shadow' OR decision = 'block'",
            "ck_autonomy_decisions_shadow_never_publishes",
        ),
        _check(f"actor ~ '{ACTOR_PATTERN}'", "ck_autonomy_decisions_actor_shape"),
        # An agent may neither decide a publish nor block one (ADR-0012 stands).
        _check("actor NOT LIKE 'agent:%'", "ck_autonomy_decisions_actor_is_not_an_agent"),
        _check(
            "(actor LIKE 'user:%') = (override_reason IS NOT NULL)",
            "ck_autonomy_decisions_override_records_a_reason",
        ),
        _check(
            "override_reason IS NULL OR length(override_reason) BETWEEN 1 AND "
            f"{MAX_OVERRIDE_REASON_CHARS}",
            "ck_autonomy_decisions_override_reason_bounded",
        ),
        _check(
            f"length(engine_version) BETWEEN 1 AND {MAX_ENGINE_VERSION_CHARS}",
            "ck_autonomy_decisions_engine_version_bounded",
        ),
        _check(
            "calibration_version IS NULL OR length(calibration_version) BETWEEN 1 AND "
            f"{MAX_CALIBRATION_VERSION_CHARS}",
            "ck_autonomy_decisions_calibration_version_bounded",
        ),
    )
    # xid8 has no SQLAlchemy type, so it is declared in raw DDL exactly as 0007 does for
    # link_plan_transitions.txid. The table is empty here, so no rewrite concern arises.
    op.execute(
        "ALTER TABLE autonomy_decisions "
        "ADD COLUMN txid xid8 NOT NULL DEFAULT pg_current_xact_id()"
    )
    op.create_index(
        op.f("ix_autonomy_decisions_plan_id_id"), "autonomy_decisions", ["plan_id", "id"]
    )
    op.create_index(
        op.f("ix_autonomy_decisions_decision_evaluated_at"),
        "autonomy_decisions",
        ["decision", "evaluated_at"],
    )
    op.create_index(
        op.f("ix_autonomy_decisions_blocks"),
        "autonomy_decisions",
        ["blocks"],
        postgresql_using="gin",
        postgresql_ops={"blocks": "jsonb_path_ops"},
    )

    # -------------------------------------------------- 4. publish_attempts
    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("verification_profile", sa.Text(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column(
            "rolled_back", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("failure_code", sa.Text(), nullable=True),
        _ts("cooldown_until", nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        _ts("occurred_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_attempts")),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["pages.id"],
            name=op.f("fk_publish_attempts_source_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["pages.id"],
            name=op.f("fk_publish_attempts_target_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["link_plans.id"],
            name=op.f("fk_publish_attempts_plan_id_link_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_publish_attempts_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        _check("source_page_id <> target_page_id", "ck_publish_attempts_not_self"),
        _check("attempt >= 1", "ck_publish_attempts_attempt_positive"),
        _check("max_attempts BETWEEN 1 AND 10", "ck_publish_attempts_max_attempts_bounded"),
        _check("attempt <= max_attempts", "ck_publish_attempts_attempt_within_budget"),
        _check(_one_of("decision", AUTONOMY_DECISIONS), "ck_publish_attempts_decision_valid"),
        _check(_one_of("mode", AUTONOMY_MODES), "ck_publish_attempts_mode_valid"),
        _check(
            _one_of("verification_profile", VERIFICATION_PROFILES),
            "ck_publish_attempts_verification_profile_valid",
        ),
        # Shadow publishes nothing, so it can never record an attempt.
        _check("mode <> 'shadow'", "ck_publish_attempts_shadow_never_attempts"),
        _check("decision <> 'block'", "ck_publish_attempts_a_block_is_never_attempted"),
        _check("succeeded OR failure_code IS NOT NULL", "ck_publish_attempts_failure_has_a_code"),
        _check(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND "
            f"{MAX_FAILURE_CODE_CHARS}",
            "ck_publish_attempts_failure_code_bounded",
        ),
        _check(
            "NOT rolled_back OR NOT succeeded", "ck_publish_attempts_rolled_back_is_not_success"
        ),
        _check(f"actor ~ '{ACTOR_PATTERN}'", "ck_publish_attempts_actor_shape"),
        _check("actor NOT LIKE 'agent:%'", "ck_publish_attempts_actor_is_not_an_agent"),
    )
    # The budget cannot be spent twice by a race: the second INSERT of the same attempt
    # number for a pair is REFUSED, not merged.
    op.create_index(
        op.f("uq_publish_attempts_pair_attempt"),
        "publish_attempts",
        ["source_page_id", "target_page_id", "attempt"],
        unique=True,
    )
    op.create_index(
        op.f("ix_publish_attempts_plan_id_id"), "publish_attempts", ["plan_id", "id"]
    )
    op.create_index(
        op.f("ix_publish_attempts_cooldown"),
        "publish_attempts",
        ["source_page_id", "target_page_id", "cooldown_until"],
    )

    # --------------------------------------------------------------- guards
    # A decision row that CLAIMS to have moved the plan needs the matching transition row in
    # the SAME transaction (the 0007 / 0009 pattern). DEFERRABLE INITIALLY DEFERRED, so the
    # two rows may be written in either order.
    op.execute(
        f"""
        CREATE FUNCTION ken_autonomy_decision_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            expected text;
        BEGIN
            expected := CASE NEW.decision
                WHEN 'block' THEN 'blocked'
                ELSE 'autonomy_decided'
            END;
            IF NEW.mode = 'shadow' THEN
                -- A shadow pass records what WOULD have happened and moves nothing.
                IF EXISTS (
                    SELECT 1 FROM link_plan_transitions t
                    WHERE t.plan_id = NEW.plan_id
                      AND t.to_status IN ({_sql_list(("autonomy_decided", "blocked"))})
                      AND t.txid = pg_current_xact_id()
                ) THEN
                    RAISE EXCEPTION
                        'a shadow autonomy decision must not move plan %', NEW.plan_id
                        USING ERRCODE = '55000';
                END IF;
                RETURN NULL;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM link_plan_transitions t
                WHERE t.plan_id = NEW.plan_id
                  AND t.to_status = expected
                  AND t.txid = pg_current_xact_id()
            ) THEN
                RAISE EXCEPTION
                    'autonomy decision % for plan % has no same-transaction transition to %',
                    NEW.decision, NEW.plan_id, expected
                    USING ERRCODE = '55000';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER autonomy_decisions_plan_integrity "
        "AFTER INSERT ON autonomy_decisions "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION ken_autonomy_decision_guard()"
    )

    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ken_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS autonomy_decisions_plan_integrity ON autonomy_decisions"
    )
    op.execute("DROP FUNCTION IF EXISTS ken_autonomy_decision_guard()")
    # Dropping a table drops its indexes and constraints with it.
    op.drop_table("publish_attempts")
    op.drop_table("autonomy_decisions")
    op.drop_table("page_graph_metrics")

    op.drop_constraint(
        op.f("ck_link_candidates_graph_signals_object"), "link_candidates", type_="check"
    )
    op.drop_constraint(
        op.f("ck_link_candidates_attribution_array"), "link_candidates", type_="check"
    )
    op.drop_column("link_candidates", "graph_signals")
    op.drop_column("link_candidates", "attribution")

    _replace_plan_status_guard(PLAN_TRANSITIONS_0007)
    _replace_open_plan_indexes(OPEN_STATUSES_0009)
    _replace_plan_status_checks(PLAN_STATUSES_0009)
    op.drop_constraint(op.f("ck_job_runs_kind_valid"), "job_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_job_runs_kind_valid"), "job_runs", _one_of("kind", JOB_KINDS_0009)
    )

    _convert_embedding_to_vector()
