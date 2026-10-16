"""Phase 8 learning: proposals, transitions, candidates, skills, evaluations, experiments.

Revision ID: 0009_learning
Revises: 0008_analytics
Create Date: 2026-10-16

Column-for-column with ``ken_core.db.models.learning`` (docs/adr/ADR-0016-learning.md section 9
as amended by the binding section 15; section 16 records the orchestrator's decisions).

**The two amendments that make "no self-promotion" a mechanism rather than a docstring:**

* ``learning_proposal_transitions.txid xid8 NOT NULL DEFAULT pg_current_xact_id()`` plus
  ``ken_learning_proposal_guard()``, a DEFERRABLE INITIALLY DEFERRED constraint trigger that
  requires a transition row ``(proposal, from, to)`` written by the SAME transaction --
  exactly the 0007 ``ken_link_plan_status_guard()`` pattern (section 15.7.1). Without the
  ``txid`` the transitions table is append-only, so any writer could INSERT a
  ``proposed -> promoted`` row with ``actor = 'user:admin'`` in one transaction and UPDATE the
  proposal in the next: the CHECK tests an actor's STRING SHAPE only.
* A transaction-local GUC, ``ken.learning_promoter``, on the 0008 ``ken.analytics_writer``
  model (section 15.7.2). ``SELECT set_config('ken.learning_promoter', 'on', true)`` must have
  run in the SAME transaction or the ``promoted`` transition is refused -- by
  ``ken_learning_promoter_guard()`` on INSERT and again by the deferred guard at commit. So
  exactly one code path (``ken_services.learning.promote``) can promote; a CLI one-off, a data
  fix or a test helper cannot. Being transaction-local, it never leaks to the next transaction
  on a pooled connection (asserted by a test, as in Phase 7).

The rest:

* ``learning_proposals`` -- content columns are IMMUTABLE (generic ``to_jsonb`` comparison, so
  a later column is immutable automatically); only ``status`` and ``updated_at`` may change,
  and only along :data:`PROPOSAL_TRANSITIONS`; never deleted. A partial UNIQUE index on
  ``fingerprint`` over the OPEN statuses makes a reflection re-run idempotent.
  ``proposed_by`` is CHECKed against ``^(system|agent):``: a proposal can never claim a human
  author. There is NO ``change_signature`` column -- automatic superseding was cut (15.3),
  because auto-withdrawing a human-visible proposal is a silent state change.
* ``learning_proposal_transitions`` -- APPEND-ONLY. CHECKs: a move into
  ``promoted``/``rejected``/``rolled_back`` needs ``actor ~ '^user:'``; ``promoted`` needs an
  ``evaluation_run_id``.
* ``policy_candidates`` -- the full validated candidate DOCUMENT as jsonb, so ``offline_test``
  replays without a file. Append-only.
* ``skills`` -- content immutable (``ken_skill_guard()``); at most one ``promoted`` row per
  name (partial unique index); a revision is a new ``version`` row, never an edit.
* ``policy_evaluations`` -- append-only, inserted once on completion (failures live in
  ``job_runs``, like ``analytics_imports``). ``metrics`` MUST carry a ``by_decision_class``
  object (a CHECK), so Phase 9 reads per-class calibration without a schema change (15.7.11).
* ``experiments`` / ``experiment_assignments`` -- ``kind`` is capped to ``'policy_ab'``: there
  is no ``holdout`` member anywhere (16.4), so no code path can withhold links from a page.
  Assignment is allowed only while the experiment is ``draft``, and a page may have at most
  one assignment in a non-``ended`` experiment.

Known limitation, stated plainly: every guard here is a trigger, so they defend against
application bugs and not against a DB superuser (``session_replication_role = replica``
disables all of them). No ``SECURITY DEFINER`` function exists anywhere in this schema.

The value lists below are frozen copies of the contract enums at this revision.

Downgrade drops the triggers, the six new guard functions and the seven tables, returning
exactly to the 0008 state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_learning"
down_revision: str | Sequence[str] | None = "0008_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROMOTER_SETTING: Final = "ken.learning_promoter"
APPEND_ONLY_TABLES: Final = (
    "learning_proposal_transitions",
    "policy_candidates",
    "policy_evaluations",
    "experiment_assignments",
)
PROPOSAL_MUTABLE_COLUMNS: Final = ("status", "updated_at")
SKILL_MUTABLE_COLUMNS: Final = (
    "status",
    "promoted_by",
    "promoted_at",
    "evaluation_run_id",
    "evaluation_score",
)
EXPERIMENT_MUTABLE_COLUMNS: Final = ("status", "updated_at", "started_at", "ended_at")

JOB_KINDS_0008: Final = (
    "analysis",
    "recommendation",
    "agent",
    "verification",
    "analytics_import",
    "evaluation",
    "autonomy_shadow",
)
"""``ck_job_runs_kind_valid`` as migration 0003 wrote it."""
JOB_KINDS_0009: Final = (*JOB_KINDS_0008, "reflection")
"""Phase 8 adds ``reflection``, so the 0003 CHECK is REPLACED here. Without this the first
``make reflect`` would fail on a check violation: ``JobKind`` is a Pydantic enum, but
``job_runs.kind`` carries a frozen literal copy in the database."""

MAX_TITLE_CHARS: Final = 200
MAX_RATIONALE_CHARS: Final = 2000
MAX_NOTE_CHARS: Final = 500
MAX_DESCRIPTION_CHARS: Final = 500
MAX_INSTRUCTIONS_CHARS: Final = 8000
MAX_VERSION_CHARS: Final = 64

PROPOSAL_STATUSES: Final[tuple[str, ...]] = (
    "proposed",
    "offline_test",
    "shadow",
    "canary",
    "experiment",
    "promoted",
    "rejected",
    "rolled_back",
    "withdrawn",
)
"""Literal copy of ``ken_contracts.learning.ProposalStatus`` at this revision. The full ladder
CLAUDE.md's Learning Design Rule, PRD.md section 10 and evals/README.md mandate: ``canary`` and
``experiment`` are stage-defined with their machinery deferred (ADR-0016 section 16)."""

OPEN_STATUSES: Final[tuple[str, ...]] = (
    "proposed",
    "offline_test",
    "shadow",
    "canary",
    "experiment",
)
HUMAN_ONLY_TRANSITION_TARGETS: Final[tuple[str, ...]] = ("promoted", "rejected", "rolled_back")

PROPOSAL_TRANSITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("proposed", "offline_test"),
    ("proposed", "rejected"),
    ("proposed", "withdrawn"),
    ("offline_test", "shadow"),
    ("offline_test", "canary"),
    ("offline_test", "experiment"),
    ("offline_test", "promoted"),
    ("offline_test", "rejected"),
    ("offline_test", "withdrawn"),
    ("shadow", "canary"),
    ("shadow", "experiment"),
    ("shadow", "promoted"),
    ("shadow", "rejected"),
    ("shadow", "withdrawn"),
    ("canary", "experiment"),
    ("canary", "promoted"),
    ("canary", "rejected"),
    ("canary", "withdrawn"),
    ("experiment", "promoted"),
    ("experiment", "rejected"),
    ("experiment", "withdrawn"),
    ("promoted", "rolled_back"),
)
"""Literal copy of ``ken_contracts.learning.PROPOSAL_TRANSITIONS`` (parity-tested).
``proposed -> promoted`` is absent: at least one ``offline_test`` run stands between a
hypothesis and production."""

SKILL_STATUSES: Final[tuple[str, ...]] = (
    "draft",
    "offline_test",
    "promoted",
    "rejected",
    "retired",
)
SKILL_TRANSITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("draft", "offline_test"),
    ("draft", "rejected"),
    ("offline_test", "promoted"),
    ("offline_test", "rejected"),
    ("promoted", "retired"),
)
EXPERIMENT_STATUSES: Final[tuple[str, ...]] = ("draft", "active", "ended")
EXPERIMENT_TRANSITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("draft", "active"),
    ("draft", "ended"),
    ("active", "ended"),
)

PROPOSAL_KINDS: Final = "'policy', 'skill'"
TARGET_KINDS: Final = "'ranking', 'recommendation', 'agents'"
SKILL_SOURCES: Final = "'human', 'reflection'"
MODEL_ROLES: Final = "'fast', 'reasoning', 'embedding'"
EVALUATION_MODES: Final = "'offline', 'shadow', 'canary'"
GATE_RESULTS: Final = "'pass', 'fail', 'inconclusive'"
EXPERIMENT_KINDS: Final = "'policy_ab'"
"""``holdout`` is CUT, not defaulted off (ADR-0016 16.4): no code path can withhold links."""
EXPERIMENT_ARMS: Final = "'control', 'treatment'"
PROPOSAL_REASONS: Final = (
    "'insufficient_evidence', 'gate_failed', 'frozen_field', 'out_of_scope', "
    "'risk_too_high', 'superseded_by_newer', 'demonstration_only', "
    "'regression_observed', 'operational_incident'"
)

_SHA256: Final = "~ '^[0-9a-f]{64}$'"
_ACTOR: Final = (
    r"~ '^(user:\S{1,200}|agent:[a-z0-9_-]{1,64}@[0-9A-Za-z._-]{1,32}"
    r"|system:[a-z0-9_-]{1,64})$'"
)
"""Literal copy of ``ken_contracts.recommendations.ACTOR_PATTERN``."""
_PROPOSER: Final = (
    r"~ '^(system:[a-z0-9_-]{1,64}|agent:[a-z0-9_-]{1,64}@[0-9A-Za-z._-]{1,32})$'"
)
_PROMOTER_RE: Final = "'^user:[a-z0-9][a-z0-9._-]{2,63}$'"
_PROMOTER: Final = f"~ {_PROMOTER_RE}"
"""``^user:`` + ``ken_contracts.auth.USERNAME_PATTERN``. The database can only check an
actor's SHAPE; authenticity comes from the session (ADR-0016 15.2b)."""
_SKILL_NAME: Final = "~ '^[a-z][a-z0-9_]{2,63}$'"
_EXPERIMENT_KEY: Final = "~ '^[a-z0-9][a-z0-9_-]{2,63}$'"


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default_now else None,
        nullable=nullable,
    )


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(name))


def _jsonb(name: str, *, nullable: bool = False, default: str | None = None) -> sa.Column[Any]:
    return sa.Column(
        name,
        postgresql.JSONB(),
        server_default=sa.text(f"'{default}'::jsonb") if default is not None else None,
        nullable=nullable,
    )


def _array(columns: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(f"'{column}'" for column in columns) + "]::text[]"


def _pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return "ARRAY[" + ", ".join(f"'{a}>{b}'" for a, b in pairs) + "]::text[]"


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    # job_runs.kind gains 'reflection' (ADR-0016 section 3). The CHECK is a frozen literal
    # copy of JobKind, so a new member is a migration, not just a contract edit.
    op.drop_constraint(op.f("ck_job_runs_kind_valid"), "job_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_job_runs_kind_valid"), "job_runs", _one_of("kind", JOB_KINDS_0009)
    )

    # ------------------------------------------------------------------ proposals
    op.create_table(
        "learning_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=True),
        sa.Column("base_version", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        _jsonb("body"),
        _jsonb("evidence"),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("supersedes", sa.Uuid(), nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_proposals")),
        sa.ForeignKeyConstraint(
            ["job_run_id"],
            ["job_runs.id"],
            name=op.f("fk_learning_proposals_job_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes"],
            ["learning_proposals.id"],
            name=op.f("fk_learning_proposals_supersedes_learning_proposals"),
            ondelete="RESTRICT",
        ),
        _check(_one_of("kind", ("policy", "skill")), "ck_learning_proposals_kind_valid"),
        _check(_one_of("status", PROPOSAL_STATUSES), "ck_learning_proposals_status_valid"),
        _check(
            f"target_kind IS NULL OR {_one_of('target_kind', ('ranking', 'recommendation', 'agents'))}",
            "ck_learning_proposals_target_kind_valid",
        ),
        _check(
            "(kind = 'policy') = (target_kind IS NOT NULL)",
            "ck_learning_proposals_target_kind_iff_policy",
        ),
        _check(
            f"length(base_version) BETWEEN 1 AND {MAX_VERSION_CHARS}",
            "ck_learning_proposals_base_version_bounded",
        ),
        _check(
            f"length(title) BETWEEN 1 AND {MAX_TITLE_CHARS}",
            "ck_learning_proposals_title_bounded",
        ),
        _check(
            f"length(rationale) BETWEEN 1 AND {MAX_RATIONALE_CHARS}",
            "ck_learning_proposals_rationale_bounded",
        ),
        _check("jsonb_typeof(body) = 'object'", "ck_learning_proposals_body_object"),
        _check("jsonb_typeof(evidence) = 'object'", "ck_learning_proposals_evidence_object"),
        _check(f"fingerprint {_SHA256}", "ck_learning_proposals_fingerprint_sha256"),
        # An agent may PROPOSE; it can never be the author of a promotion (ADR-0012).
        _check(f"proposed_by {_PROPOSER}", "ck_learning_proposals_proposed_by_not_human"),
        _check("supersedes IS NULL OR supersedes <> id", "ck_learning_proposals_supersedes_other"),
        _check(
            "model IS NULL OR length(model) BETWEEN 1 AND 200",
            "ck_learning_proposals_model_bounded",
        ),
        _check(
            "NOT fallback_used OR model IS NULL", "ck_learning_proposals_fallback_has_no_model"
        ),
    )
    op.create_index(
        op.f("ix_learning_proposals_status_created_at"),
        "learning_proposals",
        ["status", "created_at"],
    )
    op.create_index(
        op.f("ix_learning_proposals_kind_status"), "learning_proposals", ["kind", "status"]
    )
    # Idempotent reflection: one OPEN proposal per fingerprint. Terminal rows do not collide,
    # so the same hypothesis may legitimately be re-proposed after a rejection.
    op.execute(
        "CREATE UNIQUE INDEX ix_learning_proposals_open_fingerprint ON learning_proposals "
        "(fingerprint) WHERE status IN (" + ", ".join(f"'{s}'" for s in OPEN_STATUSES) + ")"
    )

    # ------------------------------------------------------------- policy candidates
    op.create_table(
        "policy_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("policy_kind", sa.Text(), nullable=False),
        sa.Column("base_version", sa.Text(), nullable=False),
        sa.Column("candidate_version", sa.Text(), nullable=False),
        _jsonb("document"),
        sa.Column("document_sha256", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_candidates")),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["learning_proposals.id"],
            name=op.f("fk_policy_candidates_proposal_id_learning_proposals"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "candidate_version", name=op.f("uq_policy_candidates_candidate_version")
        ),
        _check(
            _one_of("policy_kind", ("ranking", "recommendation", "agents")),
            "ck_policy_candidates_policy_kind_valid",
        ),
        _check("jsonb_typeof(document) = 'object'", "ck_policy_candidates_document_object"),
        _check(f"document_sha256 {_SHA256}", "ck_policy_candidates_document_sha256"),
        _check(
            f"length(base_version) BETWEEN 1 AND {MAX_VERSION_CHARS}",
            "ck_policy_candidates_base_version_bounded",
        ),
        _check(
            f"length(candidate_version) BETWEEN 1 AND {MAX_VERSION_CHARS}",
            "ck_policy_candidates_candidate_version_bounded",
        ),
    )
    op.create_index(
        op.f("ix_policy_candidates_proposal_id_id"), "policy_candidates", ["proposal_id", "id"]
    )

    # --------------------------------------------------------------------- skills
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        _jsonb("input_schema"),
        _jsonb("output_schema"),
        _jsonb("suites"),
        sa.Column("model_role", sa.Text(), server_default="reasoning", nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_score", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("promoted_by", sa.Text(), nullable=True),
        _ts("promoted_at", nullable=True),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["learning_proposals.id"],
            name=op.f("fk_skills_proposal_id_learning_proposals"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("name", "version", name=op.f("uq_skills_name_version")),
        _check(f"name {_SKILL_NAME}", "ck_skills_name_valid"),
        _check("version >= 1", "ck_skills_version_positive"),
        _check(
            f"length(description) BETWEEN 1 AND {MAX_DESCRIPTION_CHARS}",
            "ck_skills_description_bounded",
        ),
        _check(
            f"length(instructions) BETWEEN 1 AND {MAX_INSTRUCTIONS_CHARS}",
            "ck_skills_instructions_bounded",
        ),
        _check("jsonb_typeof(input_schema) = 'object'", "ck_skills_input_schema_object"),
        _check("jsonb_typeof(output_schema) = 'object'", "ck_skills_output_schema_object"),
        _check(
            "jsonb_typeof(suites) = 'array' AND jsonb_array_length(suites) >= 1",
            "ck_skills_suites_non_empty_array",
        ),
        _check(_one_of("model_role", ("fast", "reasoning", "embedding")), "ck_skills_model_role_valid"),
        _check(_one_of("source", ("human", "reflection")), "ck_skills_source_valid"),
        _check(_one_of("status", SKILL_STATUSES), "ck_skills_status_valid"),
        _check(
            "evaluation_score IS NULL OR (evaluation_score >= 0.0 AND evaluation_score <= 1.0)",
            "ck_skills_evaluation_score_normalised",
        ),
        _check(f"promoted_by IS NULL OR promoted_by {_PROMOTER}", "ck_skills_promoted_by_human"),
        _check("(promoted_by IS NULL) = (promoted_at IS NULL)", "ck_skills_promoted_pair"),
        # A promoted skill always names its human authoriser and its passing evaluation.
        _check(
            "status <> 'promoted' OR (promoted_by IS NOT NULL AND promoted_at IS NOT NULL "
            "AND evaluation_run_id IS NOT NULL)",
            "ck_skills_promoted_is_audited",
        ),
        _check(
            "source <> 'reflection' OR proposal_id IS NOT NULL", "ck_skills_reflection_has_proposal"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_skills_promoted_name ON skills (name) WHERE status = 'promoted'"
    )
    op.create_index(op.f("ix_skills_status_name"), "skills", ["status", "name"])

    # ---------------------------------------------------------- policy evaluations
    op.create_table(
        "policy_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_run_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_fingerprint", sa.Text(), nullable=True),
        _jsonb("suites"),
        _jsonb("dataset_manifests"),
        sa.Column("baseline_evaluation_id", sa.Uuid(), nullable=True),
        _jsonb("metrics"),
        sa.Column("gate_result", sa.Text(), nullable=False),
        _jsonb("gate_reasons", default="[]"),
        sa.Column("runner_version", sa.Text(), nullable=False),
        sa.Column("model_provider", sa.Text(), nullable=False),
        _jsonb("model_role_models", default="{}"),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("git_sha", sa.Text(), nullable=True),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_evaluations")),
        sa.ForeignKeyConstraint(
            ["job_run_id"],
            ["job_runs.id"],
            name=op.f("fk_policy_evaluations_job_run_id_job_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["learning_proposals.id"],
            name=op.f("fk_policy_evaluations_proposal_id_learning_proposals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["policy_candidates.id"],
            name=op.f("fk_policy_evaluations_candidate_id_policy_candidates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name=op.f("fk_policy_evaluations_skill_id_skills"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_evaluation_id"],
            ["policy_evaluations.id"],
            name=op.f("fk_policy_evaluations_baseline_evaluation_id_policy_evaluations"),
            ondelete="RESTRICT",
        ),
        _check(_one_of("mode", ("offline", "shadow", "canary")), "ck_policy_evaluations_mode_valid"),
        _check(
            _one_of("gate_result", ("pass", "fail", "inconclusive")),
            "ck_policy_evaluations_gate_result_valid",
        ),
        _check(
            "(proposal_id IS NOT NULL) <> (skill_id IS NOT NULL)",
            "ck_policy_evaluations_exactly_one_subject",
        ),
        _check(
            "candidate_id IS NULL OR proposal_id IS NOT NULL",
            "ck_policy_evaluations_candidate_needs_proposal",
        ),
        _check(
            f"proposal_fingerprint IS NULL OR proposal_fingerprint {_SHA256}",
            "ck_policy_evaluations_fingerprint_sha256",
        ),
        _check(
            "jsonb_typeof(suites) = 'array' AND jsonb_array_length(suites) >= 1",
            "ck_policy_evaluations_suites_non_empty_array",
        ),
        _check(
            "jsonb_typeof(dataset_manifests) = 'object'",
            "ck_policy_evaluations_dataset_manifests_object",
        ),
        _check("jsonb_typeof(metrics) = 'object'", "ck_policy_evaluations_metrics_object"),
        # Phase 9 reads per-decision-class calibration from here; requiring the key in the
        # DATABASE is what keeps that true without a later schema change (ADR-0016 15.7.11).
        _check(
            "metrics ? 'by_decision_class' "
            "AND jsonb_typeof(metrics -> 'by_decision_class') = 'object'",
            "ck_policy_evaluations_metrics_by_decision_class",
        ),
        _check("jsonb_typeof(gate_reasons) = 'array'", "ck_policy_evaluations_gate_reasons_array"),
        _check(
            "jsonb_typeof(model_role_models) = 'object'",
            "ck_policy_evaluations_model_role_models_object",
        ),
        _check(
            "finished_at IS NULL OR finished_at >= started_at",
            "ck_policy_evaluations_finished_after_started",
        ),
        _check(
            "baseline_evaluation_id IS NULL OR baseline_evaluation_id <> id",
            "ck_policy_evaluations_baseline_is_other",
        ),
        _check(
            "length(runner_version) BETWEEN 1 AND 64", "ck_policy_evaluations_runner_version_bounded"
        ),
        _check(
            "length(model_provider) BETWEEN 1 AND 64",
            "ck_policy_evaluations_model_provider_bounded",
        ),
    )
    op.create_index(
        op.f("ix_policy_evaluations_proposal_id_id"), "policy_evaluations", ["proposal_id", "id"]
    )
    op.create_index(
        op.f("ix_policy_evaluations_gate_result_started_at"),
        "policy_evaluations",
        ["gate_result", "started_at"],
    )
    # skills.evaluation_run_id -> policy_evaluations.id closes a mutual reference, so it is
    # added after both tables exist rather than inside either CREATE TABLE.
    op.create_foreign_key(
        op.f("fk_skills_evaluation_run_id_policy_evaluations"),
        "skills",
        "policy_evaluations",
        ["evaluation_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ----------------------------------------------------- proposal transitions
    op.create_table(
        "learning_proposal_transitions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=False),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=True),
        _ts("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_proposal_transitions")),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["learning_proposals.id"],
            name=op.f("fk_learning_proposal_transitions_proposal_id_learning_proposals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["policy_evaluations.id"],
            name=op.f("fk_learning_proposal_transitions_evaluation_run_id_policy_evaluations"),
            ondelete="RESTRICT",
        ),
        _check(
            _one_of("from_status", PROPOSAL_STATUSES),
            "ck_learning_proposal_transitions_from_status_valid",
        ),
        _check(
            _one_of("to_status", PROPOSAL_STATUSES),
            "ck_learning_proposal_transitions_to_status_valid",
        ),
        _check("from_status <> to_status", "ck_learning_proposal_transitions_status_changes"),
        _check(f"actor {_ACTOR}", "ck_learning_proposal_transitions_actor_valid"),
        # THE no-self-promotion CHECKs (ADR-0016 section 9.2). Shape only -- the txid guard and
        # the ken.learning_promoter setting are what make them unforgeable.
        _check(
            f"to_status NOT IN ({', '.join(repr(s) for s in HUMAN_ONLY_TRANSITION_TARGETS)}) "
            f"OR actor {_PROMOTER}",
            "ck_learning_proposal_transitions_human_only_targets",
        ),
        _check(
            "to_status <> 'promoted' OR evaluation_run_id IS NOT NULL",
            "ck_learning_proposal_transitions_promoted_cites_evaluation",
        ),
        _check(
            f"reason IS NULL OR reason IN ({PROPOSAL_REASONS})",
            "ck_learning_proposal_transitions_reason_valid",
        ),
        _check(
            f"note IS NULL OR length(note) <= {MAX_NOTE_CHARS}",
            "ck_learning_proposal_transitions_note_bounded",
        ),
    )
    op.create_index(
        op.f("ix_learning_proposal_transitions_proposal_id_id"),
        "learning_proposal_transitions",
        ["proposal_id", "id"],
    )
    # Volatile default: added as its own statement, exactly as 0007 does for
    # link_plan_transitions.txid. The DEFERRABLE guard below compares it in SQL.
    op.execute(
        "ALTER TABLE learning_proposal_transitions "
        "ADD COLUMN txid xid8 NOT NULL DEFAULT pg_current_xact_id()"
    )

    # ---------------------------------------------------------------- experiments
    op.create_table(
        "experiments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), server_default="source_page", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("treatment_fraction", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        _ts("started_at", nullable=True),
        _ts("ended_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiments")),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["policy_candidates.id"],
            name=op.f("fk_experiments_candidate_id_policy_candidates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["learning_proposals.id"],
            name=op.f("fk_experiments_proposal_id_learning_proposals"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("key", name=op.f("uq_experiments_key")),
        _check(f"key {_EXPERIMENT_KEY}", "ck_experiments_key_valid"),
        _check(
            f"length(description) BETWEEN 1 AND {MAX_DESCRIPTION_CHARS}",
            "ck_experiments_description_bounded",
        ),
        _check("unit = 'source_page'", "ck_experiments_unit_is_source_page"),
        # Capped to one member on purpose: there is no holdout arm anywhere in the schema.
        _check(_one_of("kind", ("policy_ab",)), "ck_experiments_kind_valid"),
        _check(_one_of("status", EXPERIMENT_STATUSES), "ck_experiments_status_valid"),
        _check(
            "kind <> 'policy_ab' OR candidate_id IS NOT NULL",
            "ck_experiments_policy_ab_has_candidate",
        ),
        _check(
            "treatment_fraction > 0.0 AND treatment_fraction <= 1.0",
            "ck_experiments_treatment_fraction_bounded",
        ),
        _check(f"created_by {_PROMOTER}", "ck_experiments_created_by_human"),
        _check(
            "status <> 'draft' OR (started_at IS NULL AND ended_at IS NULL)",
            "ck_experiments_draft_has_no_timestamps",
        ),
        _check(
            "status <> 'active' OR (started_at IS NOT NULL AND ended_at IS NULL)",
            "ck_experiments_active_is_started",
        ),
        _check("status <> 'ended' OR ended_at IS NOT NULL", "ck_experiments_ended_has_ended_at"),
        _check(
            "started_at IS NULL OR ended_at IS NULL OR ended_at >= started_at",
            "ck_experiments_interval_ordered",
        ),
    )
    op.create_index(op.f("ix_experiments_status_key"), "experiments", ["status", "key"])

    op.create_table(
        "experiment_assignments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("arm", sa.Text(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        _ts("assigned_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_assignments")),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_assignments_experiment_id_experiments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["pages.id"],
            name=op.f("fk_experiment_assignments_source_page_id_pages"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "source_page_id",
            name=op.f("uq_experiment_assignments_experiment_id_source_page_id"),
        ),
        _check(_one_of("arm", ("control", "treatment")), "ck_experiment_assignments_arm_valid"),
        _check(f"assigned_by {_ACTOR}", "ck_experiment_assignments_assigned_by_valid"),
    )
    op.create_index(
        op.f("ix_experiment_assignments_source_page_id"),
        "experiment_assignments",
        ["source_page_id"],
    )

    _create_guards()


def _create_guards() -> None:
    # learning_proposals: never deleted; content immutable; status only along the ladder.
    op.execute(
        f"""
        CREATE FUNCTION ken_learning_content_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(PROPOSAL_MUTABLE_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(PROPOSAL_MUTABLE_COLUMNS)}) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
                AND NOT ((OLD.status || '>' || NEW.status) = ANY ({_pairs(PROPOSAL_TRANSITIONS)}))
            THEN
                RAISE EXCEPTION 'learning_proposals status % -> % is not allowed',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    # The same-transaction requirement (ADR-0016 15.7.1) plus the promoter setting (15.7.2).
    # DEFERRABLE INITIALLY DEFERRED so the transition row may be written in either order.
    op.execute(
        f"""
        CREATE FUNCTION ken_learning_proposal_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM learning_proposal_transitions t
                WHERE t.proposal_id = NEW.id
                  AND t.from_status = OLD.status
                  AND t.to_status = NEW.status
                  AND t.txid = pg_current_xact_id()
            ) THEN
                RAISE EXCEPTION
                    'learning_proposals % -> % has no same-transaction transition record',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status = 'promoted'
                AND coalesce(current_setting('{PROMOTER_SETTING}', true), '') <> 'on' THEN
                RAISE EXCEPTION
                    'promoting a learning proposal requires the promotion service'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER learning_proposals_content_guard BEFORE UPDATE ON learning_proposals "
        "FOR EACH ROW EXECUTE FUNCTION ken_learning_content_guard()"
    )
    op.execute(
        "CREATE TRIGGER learning_proposals_no_delete BEFORE DELETE ON learning_proposals "
        "FOR EACH ROW EXECUTE FUNCTION ken_learning_content_guard()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER learning_proposals_status_integrity "
        "AFTER UPDATE OF status ON learning_proposals "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (OLD.status IS DISTINCT FROM NEW.status) "
        "EXECUTE FUNCTION ken_learning_proposal_guard()"
    )

    # A promoted transition row cannot even be WRITTEN outside the promotion service.
    op.execute(
        f"""
        CREATE FUNCTION ken_learning_promoter_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF coalesce(current_setting('{PROMOTER_SETTING}', true), '') <> 'on' THEN
                RAISE EXCEPTION
                    'recording a promotion requires the promotion service'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER learning_proposal_transitions_promoter_guard "
        "BEFORE INSERT ON learning_proposal_transitions FOR EACH ROW "
        "WHEN (NEW.to_status = 'promoted') EXECUTE FUNCTION ken_learning_promoter_guard()"
    )

    # skills: content immutable; only the lifecycle columns move; never deleted.
    op.execute(
        f"""
        CREATE FUNCTION ken_skill_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(SKILL_MUTABLE_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(SKILL_MUTABLE_COLUMNS)}) THEN
                RAISE EXCEPTION 'skills content is immutable: a revision is a new version row'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
                AND NOT ((OLD.status || '>' || NEW.status) = ANY ({_pairs(SKILL_TRANSITIONS)}))
            THEN
                RAISE EXCEPTION 'skills status % -> % is not allowed', OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status = 'promoted' AND (
                NEW.promoted_by IS NULL OR NEW.promoted_at IS NULL
                OR NEW.evaluation_run_id IS NULL
                OR NEW.promoted_by !~ {_PROMOTER_RE}
            ) THEN
                RAISE EXCEPTION
                    'a promoted skill needs a human promoter and a passing evaluation'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER skills_guard BEFORE UPDATE ON skills "
        "FOR EACH ROW EXECUTE FUNCTION ken_skill_guard()"
    )
    op.execute(
        "CREATE TRIGGER skills_no_delete BEFORE DELETE ON skills "
        "FOR EACH ROW EXECUTE FUNCTION ken_skill_guard()"
    )

    # experiments: content immutable after insert; status only draft -> active -> ended.
    op.execute(
        f"""
        CREATE FUNCTION ken_experiment_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_array(EXPERIMENT_MUTABLE_COLUMNS)})
                IS DISTINCT FROM (to_jsonb(OLD) - {_array(EXPERIMENT_MUTABLE_COLUMNS)}) THEN
                RAISE EXCEPTION 'table % column is immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
                AND NOT ((OLD.status || '>' || NEW.status)
                         = ANY ({_pairs(EXPERIMENT_TRANSITIONS)}))
            THEN
                RAISE EXCEPTION 'experiments status % -> % is not allowed',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER experiments_guard BEFORE UPDATE ON experiments "
        "FOR EACH ROW EXECUTE FUNCTION ken_experiment_guard()"
    )
    op.execute(
        "CREATE TRIGGER experiments_no_delete BEFORE DELETE ON experiments "
        "FOR EACH ROW EXECUTE FUNCTION ken_experiment_guard()"
    )

    # One active experiment per page, and assignment only while the experiment is a draft.
    op.execute(
        """
        CREATE FUNCTION ken_experiment_assignment_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            experiment_status text;
        BEGIN
            SELECT e.status INTO experiment_status
            FROM experiments e WHERE e.id = NEW.experiment_id FOR SHARE;
            IF experiment_status IS DISTINCT FROM 'draft' THEN
                RAISE EXCEPTION
                    'pages are assigned only while the experiment is a draft (status %)',
                    experiment_status
                    USING ERRCODE = '55000';
            END IF;
            IF EXISTS (
                SELECT 1 FROM experiment_assignments a
                JOIN experiments e ON e.id = a.experiment_id
                WHERE a.source_page_id = NEW.source_page_id
                  AND a.experiment_id <> NEW.experiment_id
                  AND e.status <> 'ended'
            ) THEN
                RAISE EXCEPTION 'page % is already assigned to a live experiment',
                    NEW.source_page_id
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER experiment_assignments_guard BEFORE INSERT ON experiment_assignments "
        "FOR EACH ROW EXECUTE FUNCTION ken_experiment_assignment_guard()"
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
        "DROP TRIGGER IF EXISTS experiment_assignments_guard ON experiment_assignments"
    )
    op.execute("DROP FUNCTION IF EXISTS ken_experiment_assignment_guard()")
    op.execute("DROP TRIGGER IF EXISTS experiments_no_delete ON experiments")
    op.execute("DROP TRIGGER IF EXISTS experiments_guard ON experiments")
    op.execute("DROP FUNCTION IF EXISTS ken_experiment_guard()")
    op.execute("DROP TRIGGER IF EXISTS skills_no_delete ON skills")
    op.execute("DROP TRIGGER IF EXISTS skills_guard ON skills")
    op.execute("DROP FUNCTION IF EXISTS ken_skill_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS learning_proposal_transitions_promoter_guard "
        "ON learning_proposal_transitions"
    )
    op.execute("DROP FUNCTION IF EXISTS ken_learning_promoter_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS learning_proposals_status_integrity ON learning_proposals"
    )
    op.execute("DROP FUNCTION IF EXISTS ken_learning_proposal_guard()")
    op.execute("DROP TRIGGER IF EXISTS learning_proposals_no_delete ON learning_proposals")
    op.execute("DROP TRIGGER IF EXISTS learning_proposals_content_guard ON learning_proposals")
    op.execute("DROP FUNCTION IF EXISTS ken_learning_content_guard()")
    op.drop_table("experiment_assignments")
    op.drop_table("experiments")
    op.drop_table("learning_proposal_transitions")
    # Mutual reference between skills and policy_evaluations: drop the added FK first.
    op.drop_constraint(
        op.f("fk_skills_evaluation_run_id_policy_evaluations"), "skills", type_="foreignkey"
    )
    op.drop_table("policy_evaluations")
    op.drop_table("skills")
    op.drop_table("policy_candidates")
    op.drop_table("learning_proposals")
    op.drop_constraint(op.f("ck_job_runs_kind_valid"), "job_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_job_runs_kind_valid"), "job_runs", _one_of("kind", JOB_KINDS_0008)
    )
