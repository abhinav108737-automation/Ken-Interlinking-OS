"""Learning tables (Phase 8, migration 0009; docs/adr/ADR-0016-learning.md section 9 as amended
by the binding section 15).

``learning_proposals``              one hypothesis. Content columns are IMMUTABLE; only
                                    ``status`` and ``updated_at`` move, and only along
                                    :data:`~ken_contracts.learning.PROPOSAL_TRANSITIONS`. Never
                                    deleted. ``proposed_by`` is CHECKed against
                                    ``^(system|agent):``, so a proposal can never claim a human
                                    author. A partial UNIQUE index over the OPEN statuses makes
                                    a reflection re-run idempotent.
``learning_proposal_transitions``   APPEND-ONLY, and where "no self-promotion" is ENFORCED. It
                                    carries ``txid`` (``pg_current_xact_id()``, the 0007
                                    ``link_plan_transitions`` pattern) and the deferred
                                    ``ken_learning_proposal_guard()`` requires a matching row in
                                    the SAME transaction; a move into ``promoted`` additionally
                                    requires the transaction-local
                                    :data:`LEARNING_PROMOTER_SETTING`.
``policy_candidates``               the full validated candidate DOCUMENT, so ``offline_test``
                                    replays without a file. Append-only.
``skills``                          the registry. Content immutable (``ken_skill_guard``); at
                                    most one ``promoted`` row per name.
``policy_evaluations``              one run, inserted once on completion. ``metrics`` must carry
                                    ``by_decision_class`` (a CHECK), so Phase 9 reads per-class
                                    calibration without a schema change.
``experiments`` /                   BOTH ``policy_ab`` and ``holdout``, with ``holdout``
``experiment_assignments``          DEFAULTED OFF rather than omitted: capped at
                                    ``treatment_fraction <= 0.20`` by CHECK, gated by
                                    ``Settings.experiments_allow_holdout`` (default False), and
                                    activatable only under the transaction-local
                                    ``ken.learning_experiment_activator`` setting. ``unit`` is a
                                    function of the kind, which is what stops a holdout
                                    comparing one page while its arms sit on another.
                                    Assignment only while ``draft``, and a page has at most one
                                    assignment in a live experiment.

Every guard is a trigger, so these defend against application bugs, not against a DB
superuser. No ``SECURITY DEFINER`` function exists in this schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ken_contracts.learning import (
    EXPERIMENT_ACTIVATOR_SETTING,
    EXPERIMENT_KEY_PATTERN,
    LEARNING_PROMOTER_SETTING,
    MAX_DESCRIPTION_CHARS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_NOTE_CHARS,
    MAX_RATIONALE_CHARS,
    MAX_TITLE_CHARS,
    OPEN_PROPOSAL_STATUSES,
    PROMOTER_ACTOR_PATTERN,
    PROPOSER_PATTERN,
    SKILL_NAME_PATTERN,
    EvaluationMode,
    ExperimentArm,
    ExperimentKind,
    ExperimentStatus,
    GateResult,
    ProposalKind,
    ProposalRejectionReason,
    ProposalStatus,
    SkillSource,
    SkillStatus,
    TargetKind,
)
from ken_contracts.model import ModelRole
from ken_contracts.recommendations import ACTOR_PATTERN
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of
from ken_core.db.types import Xid8

EXPERIMENT_ACTIVATOR_GUC: Final = EXPERIMENT_ACTIVATOR_SETTING
"""``SELECT set_config('ken.learning_experiment_activator', 'on', true)`` in the SAME
transaction is the only way to move a ``holdout`` from ``draft`` to ``active``. The lifecycle
service sets it, and only after checking ``Settings.experiments_allow_holdout``."""

LEARNING_PROMOTER_GUC: Final = LEARNING_PROMOTER_SETTING
"""Re-exported so a writer imports the GUC name from the model package, as Phase 7 does with
``ANALYTICS_WRITER_SETTING``: ``SELECT set_config('ken.learning_promoter', 'on', true)`` in the
SAME transaction is the only way to record a promotion."""

APPEND_ONLY_LEARNING_TABLES: Final = (
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

MAX_VERSION_CHARS: Final = 64
_SHA256: Final = "~ '^[0-9a-f]{64}$'"
_OPEN_STATUS_SQL: Final = ", ".join(
    f"'{status.value}'" for status in ProposalStatus if status in OPEN_PROPOSAL_STATUSES
)


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _jsonb(default: str | None = None) -> Any:
    return mapped_column(
        JSONB,
        nullable=False,
        server_default=text(f"'{default}'::jsonb") if default is not None else None,
    )


class LearningProposal(Base):
    __tablename__ = "learning_proposals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``ranking`` | ``recommendation`` | ``agents`` for a policy; NULL for a skill."""
    base_version: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict[str, Any]] = _jsonb()
    """``PolicyProposalBody`` or ``SkillProposalBody``. A policy body's change paths were
    validated against the learning allowlist at the contract boundary."""
    evidence: Mapped[dict[str, Any]] = _jsonb()
    """``ProposalEvidence``: every number computed by ``ken_services.learning.aggregate``,
    plus the FULL server-enumerated option set the model chose from."""
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by: Mapped[str] = mapped_column(Text, nullable=False)
    job_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    supersedes: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("learning_proposals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Set only by a human: automatic superseding was cut (ADR-0016 15.3)."""
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("kind", ProposalKind), name="kind_valid"),
        CheckConstraint(_one_of("status", ProposalStatus), name="status_valid"),
        CheckConstraint(
            "target_kind IS NULL OR " + _one_of("target_kind", TargetKind),
            name="target_kind_valid",
        ),
        CheckConstraint(
            "(kind = 'policy') = (target_kind IS NOT NULL)", name="target_kind_iff_policy"
        ),
        CheckConstraint(
            f"length(base_version) BETWEEN 1 AND {MAX_VERSION_CHARS}",
            name="base_version_bounded",
        ),
        CheckConstraint(f"length(title) BETWEEN 1 AND {MAX_TITLE_CHARS}", name="title_bounded"),
        CheckConstraint(
            f"length(rationale) BETWEEN 1 AND {MAX_RATIONALE_CHARS}", name="rationale_bounded"
        ),
        CheckConstraint("jsonb_typeof(body) = 'object'", name="body_object"),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
        CheckConstraint(f"fingerprint {_SHA256}", name="fingerprint_sha256"),
        CheckConstraint(f"proposed_by ~ '{PROPOSER_PATTERN}'", name="proposed_by_not_human"),
        CheckConstraint("supersedes IS NULL OR supersedes <> id", name="supersedes_other"),
        CheckConstraint("model IS NULL OR length(model) BETWEEN 1 AND 200", name="model_bounded"),
        CheckConstraint("NOT fallback_used OR model IS NULL", name="fallback_has_no_model"),
        Index("ix_learning_proposals_status_created_at", "status", "created_at"),
        Index("ix_learning_proposals_kind_status", "kind", "status"),
        Index(
            "ix_learning_proposals_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text(f"status IN ({_OPEN_STATUS_SQL})"),
        ),
    )


class PolicyCandidate(Base):
    __tablename__ = "policy_candidates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("learning_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_kind: Mapped[str] = mapped_column(Text, nullable=False)
    base_version: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_version: Mapped[str] = mapped_column(Text, nullable=False)
    document: Mapped[dict[str, Any]] = _jsonb()
    """The whole validated candidate policy document. A policy DOCUMENT, so the API serves it
    behind ``read_settings`` and never behind ``read`` (ADR-0013 section 7)."""
    document_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint("candidate_version", name="uq_policy_candidates_candidate_version"),
        CheckConstraint(_one_of("policy_kind", TargetKind), name="policy_kind_valid"),
        CheckConstraint("jsonb_typeof(document) = 'object'", name="document_object"),
        CheckConstraint(f"document_sha256 {_SHA256}", name="document_sha256"),
        CheckConstraint(
            f"length(base_version) BETWEEN 1 AND {MAX_VERSION_CHARS}",
            name="base_version_bounded",
        ),
        CheckConstraint(
            f"length(candidate_version) BETWEEN 1 AND {MAX_VERSION_CHARS}",
            name="candidate_version_bounded",
        ),
        Index("ix_policy_candidates_proposal_id_id", "proposal_id", "id"),
    )


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = _jsonb()
    output_schema: Mapped[dict[str, Any]] = _jsonb()
    suites: Mapped[list[str]] = _jsonb()
    """The eval suites this skill is scored against. Carried on the ROW, not only on the
    proposal body, so a human-authored skill (``source = 'human'``, no proposal) is still
    self-contained for the runner. Additive to ADR-0016 section 4's column list."""
    model_role: Mapped[str] = mapped_column(Text, nullable=False, server_default="reasoning")
    source: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("learning_proposals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    evaluation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "policy_evaluations.id",
            ondelete="RESTRICT",
            name="fk_skills_evaluation_run_id_policy_evaluations",
            use_alter=True,
        ),
        nullable=True,
    )
    """Mutual reference with ``policy_evaluations``, so migration 0009 adds this FK after both
    tables exist (``use_alter``)."""
    evaluation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Display scalar only. The record is the ``policy_evaluations`` run and its per-suite
    metric map (TRD section 17 amended, ADR-0016 13.1)."""
    status: Mapped[str] = mapped_column(Text, nullable=False)
    promoted_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_skills_name_version"),
        CheckConstraint(f"name ~ '{SKILL_NAME_PATTERN}'", name="name_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            f"length(description) BETWEEN 1 AND {MAX_DESCRIPTION_CHARS}",
            name="description_bounded",
        ),
        CheckConstraint(
            f"length(instructions) BETWEEN 1 AND {MAX_INSTRUCTIONS_CHARS}",
            name="instructions_bounded",
        ),
        CheckConstraint("jsonb_typeof(input_schema) = 'object'", name="input_schema_object"),
        CheckConstraint("jsonb_typeof(output_schema) = 'object'", name="output_schema_object"),
        CheckConstraint(
            "jsonb_typeof(suites) = 'array' AND jsonb_array_length(suites) >= 1",
            name="suites_non_empty_array",
        ),
        CheckConstraint(_one_of("model_role", ModelRole), name="model_role_valid"),
        CheckConstraint(_one_of("source", SkillSource), name="source_valid"),
        CheckConstraint(_one_of("status", SkillStatus), name="status_valid"),
        CheckConstraint(
            "evaluation_score IS NULL OR (evaluation_score >= 0.0 AND evaluation_score <= 1.0)",
            name="evaluation_score_normalised",
        ),
        CheckConstraint(
            f"promoted_by IS NULL OR promoted_by ~ '{PROMOTER_ACTOR_PATTERN}'",
            name="promoted_by_human",
        ),
        CheckConstraint("(promoted_by IS NULL) = (promoted_at IS NULL)", name="promoted_pair"),
        CheckConstraint(
            "status <> 'promoted' OR (promoted_by IS NOT NULL AND promoted_at IS NOT NULL "
            "AND evaluation_run_id IS NOT NULL)",
            name="promoted_is_audited",
        ),
        CheckConstraint(
            "source <> 'reflection' OR proposal_id IS NOT NULL", name="reflection_has_proposal"
        ),
        Index(
            "ix_skills_promoted_name",
            "name",
            unique=True,
            postgresql_where=text("status = 'promoted'"),
        ),
        Index("ix_skills_status_name", "status", "name"),
    )


class PolicyEvaluation(Base):
    __tablename__ = "policy_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("learning_proposals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_candidates.id", ondelete="RESTRICT"), nullable=True
    )
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("skills.id", ondelete="RESTRICT"), nullable=True
    )
    proposal_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    suites: Mapped[list[str]] = _jsonb()
    dataset_manifests: Mapped[dict[str, str]] = _jsonb()
    """Dataset name -> manifest sha256: what makes a run reproducible from a checkout."""
    baseline_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_evaluations.id", ondelete="RESTRICT"), nullable=True
    )
    metrics: Mapped[dict[str, Any]] = _jsonb()
    """``EvaluationMetrics``. A CHECK requires the ``by_decision_class`` object, so Phase 9
    reads per-class calibration without a schema change (ADR-0016 15.7.11)."""
    gate_result: Mapped[str] = mapped_column(Text, nullable=False)
    gate_reasons: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    runner_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_role_models: Mapped[dict[str, str]] = _jsonb("{}")
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    git_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        CheckConstraint(_one_of("mode", EvaluationMode), name="mode_valid"),
        CheckConstraint(_one_of("gate_result", GateResult), name="gate_result_valid"),
        CheckConstraint(
            "(proposal_id IS NOT NULL) <> (skill_id IS NOT NULL)",
            name="exactly_one_subject",
        ),
        CheckConstraint(
            "candidate_id IS NULL OR proposal_id IS NOT NULL", name="candidate_needs_proposal"
        ),
        CheckConstraint(
            f"proposal_fingerprint IS NULL OR proposal_fingerprint {_SHA256}",
            name="fingerprint_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(suites) = 'array' AND jsonb_array_length(suites) >= 1",
            name="suites_non_empty_array",
        ),
        CheckConstraint(
            "jsonb_typeof(dataset_manifests) = 'object'", name="dataset_manifests_object"
        ),
        CheckConstraint("jsonb_typeof(metrics) = 'object'", name="metrics_object"),
        CheckConstraint(
            "metrics ? 'by_decision_class' "
            "AND jsonb_typeof(metrics -> 'by_decision_class') = 'object'",
            name="metrics_by_decision_class",
        ),
        CheckConstraint("jsonb_typeof(gate_reasons) = 'array'", name="gate_reasons_array"),
        CheckConstraint(
            "jsonb_typeof(model_role_models) = 'object'", name="model_role_models_object"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finished_after_started"
        ),
        CheckConstraint(
            "baseline_evaluation_id IS NULL OR baseline_evaluation_id <> id",
            name="baseline_is_other",
        ),
        CheckConstraint("length(runner_version) BETWEEN 1 AND 64", name="runner_version_bounded"),
        CheckConstraint("length(model_provider) BETWEEN 1 AND 64", name="model_provider_bounded"),
        Index("ix_policy_evaluations_proposal_id_id", "proposal_id", "id"),
        Index("ix_policy_evaluations_gate_result_started_at", "gate_result", "started_at"),
    )


class LearningProposalTransition(Base):
    __tablename__ = "learning_proposal_transitions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("learning_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(Text, nullable=False)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_evaluations.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(default_now=True)
    txid: Mapped[str] = mapped_column(
        Xid8(),
        nullable=False,
        server_default=text("pg_current_xact_id()"),
        deferred=True,  # never written (server default) nor loaded: compared in SQL only
    )
    """Migration 0009: the writing transaction's id. Without it the append-only CHECKs are
    forgeable across two transactions -- INSERT a ``proposed -> promoted`` row with
    ``actor = 'user:admin'`` in one, UPDATE the proposal in the next. The DEFERRABLE
    ``learning_proposals_status_integrity`` trigger requires a (proposal, from, to, txid) row
    written by the SAME transaction (ADR-0016 15.7.1)."""

    __table_args__ = (
        CheckConstraint(_one_of("from_status", ProposalStatus), name="from_status_valid"),
        CheckConstraint(_one_of("to_status", ProposalStatus), name="to_status_valid"),
        CheckConstraint("from_status <> to_status", name="status_changes"),
        CheckConstraint(f"actor ~ '{ACTOR_PATTERN}'", name="actor_valid"),
        CheckConstraint(
            "to_status NOT IN ('promoted', 'rejected', 'rolled_back') "
            f"OR actor ~ '{PROMOTER_ACTOR_PATTERN}'",
            name="human_only_targets",
        ),
        CheckConstraint(
            "to_status <> 'promoted' OR evaluation_run_id IS NOT NULL",
            name="promoted_cites_evaluation",
        ),
        CheckConstraint(
            "reason IS NULL OR " + _one_of("reason", ProposalRejectionReason),
            name="reason_valid",
        ),
        CheckConstraint(f"note IS NULL OR length(note) <= {MAX_NOTE_CHARS}", name="note_bounded"),
        Index("ix_learning_proposal_transitions_proposal_id_id", "proposal_id", "id"),
    )


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False, server_default="source_page")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    """``policy_ab`` or ``holdout``. A holdout withholds inbound links from its control pages,
    so it is capped, setting-gated and GUC-gated rather than merely discouraged."""
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_candidates.id", ondelete="RESTRICT"), nullable=True
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("learning_proposals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    treatment_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    ended_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        UniqueConstraint("key", name="uq_experiments_key"),
        CheckConstraint(f"key ~ '{EXPERIMENT_KEY_PATTERN}'", name="key_valid"),
        CheckConstraint(
            f"length(description) BETWEEN 1 AND {MAX_DESCRIPTION_CHARS}",
            name="description_bounded",
        ),
        CheckConstraint("unit = 'source_page'", name="unit_is_source_page"),
        CheckConstraint(_one_of("kind", ExperimentKind), name="kind_valid"),
        CheckConstraint(_one_of("status", ExperimentStatus), name="status_valid"),
        CheckConstraint(
            "kind <> 'policy_ab' OR candidate_id IS NOT NULL", name="policy_ab_has_candidate"
        ),
        CheckConstraint(
            "treatment_fraction > 0.0 AND treatment_fraction <= 1.0",
            name="treatment_fraction_bounded",
        ),
        CheckConstraint(f"created_by ~ '{PROMOTER_ACTOR_PATTERN}'", name="created_by_human"),
        CheckConstraint(
            "status <> 'draft' OR (started_at IS NULL AND ended_at IS NULL)",
            name="draft_has_no_timestamps",
        ),
        CheckConstraint(
            "status <> 'active' OR (started_at IS NOT NULL AND ended_at IS NULL)",
            name="active_is_started",
        ),
        CheckConstraint("status <> 'ended' OR ended_at IS NOT NULL", name="ended_has_ended_at"),
        CheckConstraint(
            "started_at IS NULL OR ended_at IS NULL OR ended_at >= started_at",
            name="interval_ordered",
        ),
        Index("ix_experiments_status_key", "status", "key"),
    )


class ExperimentAssignment(Base):
    __tablename__ = "experiment_assignments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    """The SOURCE page for a ``policy_ab`` and the TARGET page for a ``holdout``, per the
    experiment's ``unit``."""
    arm: Mapped[str] = mapped_column(Text, nullable=False)
    """Deterministic AND materialised: computed from the first 8 bytes of
    ``sha256(key + ':' + str(page_id))``, then WRITTEN, so the arm stays auditable even if the
    hash function ever changes."""
    assigned_by: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "page_id", name="uq_experiment_assignments_experiment_id_page_id"
        ),
        CheckConstraint(_one_of("arm", ExperimentArm), name="arm_valid"),
        CheckConstraint(f"assigned_by ~ '{ACTOR_PATTERN}'", name="assigned_by_valid"),
        Index("ix_experiment_assignments_page_id", "page_id"),
    )
