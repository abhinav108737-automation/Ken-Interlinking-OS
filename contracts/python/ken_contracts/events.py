"""Event vocabulary and envelope.

FROZEN CONTRACT. Implements TRD.md section 15.

The initial event bus is PostgreSQL-backed: a single append-only ``events``
table created by migration 0001. Because that table is created in Phase 0,
this envelope becomes DDL immediately -- and an append-only audit log is the
worst possible table to retrofit with a backfill. Hence the envelope carries
everything an audit trail needs from the first row:

* ``event_id``       -- stable identity, so a consumer can deduplicate
* ``event_version``  -- payload schema version, so a payload can evolve
* ``emitted_by``     -- which component wrote it, for the audit trail
* ``subject_type`` / ``subject_id`` -- what it is about, so "every event for
  page X" is an indexed query rather than a JSON probe
* ``correlation_id`` -- what caused it, tying events to a request or agent run

``EventName`` is a Pydantic enum validated at the application boundary rather
than a database enum, so the vocabulary can grow without a migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, ValidationError, model_validator

from ken_contracts.analytics import AnalyticsUpdatedPayload
from ken_contracts.auth import USERNAME_PATTERN
from ken_contracts.common import ContractModel, CorrelationId, FrozenModel
from ken_contracts.deployment import (
    LinkDeployedPayload,
    LinkFailedPayload,
    LinkRolledBackPayload,
    LinkVerifiedPayload,
)
from ken_contracts.inventory import CrawlCompletedPayload, PageDeletedPayload, PageEventPayload
from ken_contracts.jobs import JobCompletedPayload
from ken_contracts.learning import (
    PROPOSER_PATTERN,
    EvidenceWindow,
    SkillSource,
    TargetKind,
    is_learnable_path,
)


class EventName(StrEnum):
    """The TRD.md section 15 vocabulary, plus governed additions.

    Phase 0 emits only ``worker.heartbeat``. The remaining names are frozen
    here so later phases cannot invent divergent spellings for the same fact.

    Additions beyond TRD.md section 15, each an explicit decision:

    * ``skill.promoted`` -- added at the user's direction (post-Phase-0
      cleanup, 2026-10-13). TRD.md had ``policy.promoted`` but no skill
      equivalent, although AUTONOMY_AND_PERMISSIONS.md makes PROMOTE_SKILL an
      ASK-level action, which therefore needs an audit event.
    * ``worker.heartbeat`` -- operational, recorded in docs/adr/ADR-0005.md.
    * ``job.completed`` -- operational, Phase 2 (docs/adr/ADR-0010-content-intelligence.md):
      every finished generic job run (analysis, recommendation, verification,
      analytics import, evaluation, autonomy shadow), including failed ones.
      Ingestion keeps ``crawl.completed``.

    Governed lifecycles (no learning behaviour is implemented yet)::

        skill.proposed  -> offline/human evaluation -> skill.promoted
        policy.proposed -> offline/human evaluation -> policy.promoted

    Proposal is ALLOW for agents; promotion is ASK and requires a human. Both
    ``*.promoted`` events must carry a :class:`PromotionAuditPayload`, which
    :class:`EventEnvelope` enforces.
    """

    # Content lifecycle
    PAGE_CREATED = "page.created"
    PAGE_UPDATED = "page.updated"
    PAGE_DELETED = "page.deleted"
    CRAWL_COMPLETED = "crawl.completed"

    # Link lifecycle
    LINK_PROPOSED = "link.proposed"
    LINK_APPROVED = "link.approved"
    LINK_REJECTED = "link.rejected"
    LINK_DEPLOYED = "link.deployed"
    LINK_VERIFIED = "link.verified"
    LINK_FAILED = "link.failed"
    LINK_ROLLED_BACK = "link.rolled_back"

    # Measurement and learning
    ANALYTICS_UPDATED = "analytics.updated"
    SKILL_PROPOSED = "skill.proposed"
    SKILL_PROMOTED = "skill.promoted"
    POLICY_PROPOSED = "policy.proposed"
    POLICY_PROMOTED = "policy.promoted"

    # Operational
    WORKER_HEARTBEAT = "worker.heartbeat"
    JOB_COMPLETED = "job.completed"


PROMOTER_PATTERN: Final = "^user:" + USERNAME_PATTERN.removeprefix("^")
"""``^user:`` + :data:`~ken_contracts.auth.USERNAME_PATTERN`: the only shape a promotion
authoriser may have. ``ken_contracts.learning.PROMOTER_ACTOR_PATTERN`` is the literal copy
migration 0009 uses as a CHECK; a contract test asserts the two agree."""
EVALUATION_REF_PATTERN: Final = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
"""``evaluation_ref`` is ``str(policy_evaluations.id)``: a UUID string, nothing else."""


class PromotionAuditPayload(FrozenModel):
    """Required payload of every ``skill.promoted`` / ``policy.promoted`` event.

    Promotion is an ASK-level action (AUTONOMY_AND_PERMISSIONS.md) and must
    pass evaluation first (PRD.md section 9). These fields make each promotion
    reconstructable after the fact: what changed, from what, who authorised
    it, and on what evidence. A promotion event that cannot answer those
    questions is rejected at the contract boundary.
    """

    # Strip before the min_length checks: a whitespace-only "promoted_by" is
    # not an authoriser. Extra keys are rejected (extra="forbid"): adding
    # context to a promotion record is a deliberate contract change.
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    artifact_kind: Literal["skill", "policy"]
    name: str = Field(min_length=1, description="Skill or policy name.")
    version: str = Field(min_length=1, description="The version being promoted.")
    previous_version: str | None = Field(
        default=None,
        description="The version it replaces, if any -- what a rollback would restore.",
    )
    promoted_by: str = Field(
        pattern=PROMOTER_PATTERN,
        description=(
            "The HUMAN who authorised the promotion: ``user:<username>`` and nothing else. "
            "Agents may propose but never promote. Phase 8 (ADR-0016 15.7.4) made this a "
            "PATTERN: until then the docstring claimed a human while "
            "``promoted_by='agent:reflector@1'`` validated, so the module's central claim was "
            "decorative. The actor is always derived from the session (ken_contracts.auth)."
        ),
    )
    evaluation_ref: str = Field(
        pattern=EVALUATION_REF_PATTERN,
        description=(
            "``str(policy_evaluations.id)`` of the passing evaluation that justified the "
            "promotion -- a UUID, so the run is resolvable rather than merely cited "
            "(ADR-0016 section 7 / 15.7.4)."
        ),
    )


PROMOTION_EVENTS: Final = {
    EventName.SKILL_PROMOTED: "skill",
    EventName.POLICY_PROMOTED: "policy",
}
"""Promotion event -> the artifact kind its payload must describe."""


class EventEnvelope(ContractModel):
    """One row of the append-only event log. Column-for-column with migration 0001.

    Frozen: field assignment is refused outright and leaves the object
    UNCHANGED. (``validate_assignment`` alone was not enough -- pydantic sets the
    field before the after-validator rejects it, so a "rejected" assignment
    still mutated the envelope.) Freezing cannot see in-place mutation of the
    ``payload`` dict or a ``model_copy(update=...)``; that is why
    ``append_event`` re-validates at the persistence boundary as well.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4, description="Stable identity for dedupe.")
    event_name: EventName
    event_version: int = Field(
        default=1, ge=1, description="Schema version of ``payload`` for this event_name."
    )
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    emitted_by: str = Field(
        min_length=1, description="Component that wrote the event, e.g. 'ken-worker'."
    )
    subject_type: str | None = Field(
        default=None, description="Kind of entity the event concerns, e.g. 'page'."
    )
    subject_id: str | None = Field(default=None, description="Identifier of that entity, opaque.")
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: CorrelationId | None = None

    @model_validator(mode="after")
    def _promotions_are_governed(self) -> EventEnvelope:
        """A ``*.promoted`` event must carry a complete, consistent audit payload."""
        kind = PROMOTION_EVENTS.get(self.event_name)
        if kind is None:
            return self
        audit = PromotionAuditPayload.model_validate(self.payload)
        # Store the NORMALISED payload (whitespace stripped, optional keys
        # explicit), so what is persisted is exactly what was validated -- and
        # the stored name is the one compared with subject_id below.
        object.__setattr__(self, "payload", audit.model_dump())
        if audit.artifact_kind != kind:
            raise ValueError(
                f"{self.event_name} must describe a {kind}, not a {audit.artifact_kind}"
            )
        if self.subject_type != kind or self.subject_id != audit.name:
            raise ValueError(
                f"{self.event_name} must set subject_type={kind!r} and subject_id to the "
                f"{kind} name, so every event about it is one indexed query"
            )
        return self


# --------------------------------------------------------------------------- #
# Phase 8 (ADR-0016 section 8): the two new typed payloads.
#
# No new event name. ``skill.proposed`` / ``skill.promoted`` / ``policy.proposed`` /
# ``policy.promoted`` already exist and are exactly the four facts worth announcing;
# ``skill.promoted`` is a GOVERNED ADDITION recorded in docs/CONTRACTS.md section 12, not a
# TRD section 15 name. Stage changes live in the append-only ``learning_proposal_transitions``
# table (queryable, no vocabulary growth) and a finished evaluation is already announced by
# ``job.completed`` with ``kind=evaluation``.
# --------------------------------------------------------------------------- #
class PolicyProposedPayload(FrozenModel):
    """``policy.proposed`` (``event_version`` 1).

    ``change_paths`` is re-checked against the learning allowlist here, at the event boundary,
    so a proposal that touched a frozen field cannot even be ANNOUNCED. ``proposed_by`` is
    constrained to ``system:`` / ``agent:``: a proposal can never claim a human author.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    proposal_id: UUID
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_kind: TargetKind
    base_version: str = Field(min_length=1, max_length=64)
    change_paths: tuple[str, ...] = Field(min_length=1, max_length=50)
    proposed_by: str = Field(pattern=PROPOSER_PATTERN)
    job_run_id: UUID
    evidence_window: EvidenceWindow
    model: str | None = Field(default=None, description="Resolved REASONING model, or None.")
    fallback_used: bool = False

    @model_validator(mode="after")
    def _paths_are_learnable(self) -> PolicyProposedPayload:
        offending = sorted(path for path in self.change_paths if not is_learnable_path(path))
        if offending:
            raise ValueError(
                "a proposal may not change " + ", ".join(offending) + " (frozen or not "
                "allowlisted in ken_contracts.learning)"
            )
        if len(set(self.change_paths)) != len(self.change_paths):
            raise ValueError("change_paths must be unique")
        return self


class SkillProposedPayload(FrozenModel):
    """``skill.proposed`` (``event_version`` 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    proposal_id: UUID
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: int = Field(ge=1)
    source: SkillSource
    proposed_by: str = Field(pattern=PROPOSER_PATTERN)
    job_run_id: UUID
    model: str | None = None
    fallback_used: bool = False


# --------------------------------------------------------------------------- #
# Version-aware reads (the owed Phase 0 "cleanup QA M1" item; ADR-0016 section 8 / 15.7.14)
# --------------------------------------------------------------------------- #
EVENT_PAYLOAD_MODELS: Final[Mapping[EventName, Mapping[int, type[FrozenModel]]]] = {
    EventName.PAGE_CREATED: {1: PageEventPayload},
    EventName.PAGE_UPDATED: {1: PageEventPayload},
    EventName.PAGE_DELETED: {1: PageDeletedPayload},
    EventName.CRAWL_COMPLETED: {1: CrawlCompletedPayload},
    EventName.LINK_DEPLOYED: {1: LinkDeployedPayload},
    EventName.LINK_VERIFIED: {1: LinkVerifiedPayload},
    EventName.LINK_FAILED: {1: LinkFailedPayload},
    EventName.LINK_ROLLED_BACK: {1: LinkRolledBackPayload},
    EventName.ANALYTICS_UPDATED: {1: AnalyticsUpdatedPayload},
    EventName.SKILL_PROPOSED: {1: SkillProposedPayload},
    EventName.SKILL_PROMOTED: {1: PromotionAuditPayload},
    EventName.POLICY_PROPOSED: {1: PolicyProposedPayload},
    EventName.POLICY_PROMOTED: {1: PromotionAuditPayload},
    EventName.JOB_COMPLETED: {1: JobCompletedPayload},
}
"""``event_name -> event_version -> the model that validates that version's payload``.

An old row is read by the model registered for ITS ``event_version``. A stored row is NEVER
rewritten: the registry grows, the log does not change. This is what removes the standing risk
that one bad row makes the audit log unreadable forever."""

UNTYPED_EVENTS: Final = frozenset(
    {
        EventName.LINK_PROPOSED,
        EventName.LINK_APPROVED,
        EventName.LINK_REJECTED,
        EventName.WORKER_HEARTBEAT,
    }
)
"""Names with no payload schema BY DESIGN (their payloads are free-form context). A test
asserts :data:`EVENT_PAYLOAD_MODELS` and this set together cover every :class:`EventName`, so
adding a name forces a decision instead of silently defaulting to "untyped"."""


class PayloadStatus(StrEnum):
    """Verdict of :func:`validate_payload`. Also ``EventRecord.payload_status``."""

    OK = "ok"
    """Validated by the model registered for this (name, version) -- or untyped by design."""
    UNKNOWN_VERSION = "unknown_version"
    """No model is registered for this ``event_version``, or the stored event NAME is outside
    the vocabulary. The payload is returned verbatim; nothing is rewritten."""
    INVALID = "invalid"
    """A model exists and the stored payload does not satisfy it."""


class PayloadValidation(FrozenModel):
    """Why a stored payload is ``ok`` / ``unknown_version`` / ``invalid``. Carries no error
    TEXT: a validation message can quote stored content, and this travels to the audit UI."""

    status: PayloadStatus
    event_name: EventName | None = Field(
        default=None, description="None when the stored name is outside the vocabulary."
    )
    model: str | None = Field(default=None, description="Class name used, when status is ok.")
    error_count: int = Field(default=0, ge=0)


def validate_payload(*, event_name: str, event_version: int, payload: Any) -> PayloadValidation:
    """Validate ONE stored event row's payload. NEVER raises, for any input.

    ``list_events`` and the learning readers run every row through this, so a single
    malformed row degrades to a flagged row instead of a 500 that hides the whole log. The
    check covers an unknown event NAME too, not only an unknown payload version: before
    Phase 8 an unknown name broke listing at ``EventName(row.event_name)`` (ADR-0016 15.1.7).
    """
    try:
        name = EventName(event_name)
    except ValueError:
        return PayloadValidation(status=PayloadStatus.UNKNOWN_VERSION)
    try:
        versions = EVENT_PAYLOAD_MODELS.get(name)
        if versions is None:
            return PayloadValidation(status=PayloadStatus.OK, event_name=name)
        model = versions.get(int(event_version))
        if model is None:
            return PayloadValidation(status=PayloadStatus.UNKNOWN_VERSION, event_name=name)
        try:
            model.model_validate(payload)
        except ValidationError as exc:
            return PayloadValidation(
                status=PayloadStatus.INVALID, event_name=name, error_count=exc.error_count()
            )
        return PayloadValidation(status=PayloadStatus.OK, event_name=name, model=model.__name__)
    # Totality is the contract: a stored row must never be able to raise out of here.
    except Exception:
        return PayloadValidation(status=PayloadStatus.INVALID, event_name=name)
