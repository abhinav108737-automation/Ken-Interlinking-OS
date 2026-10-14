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

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator

from ken_contracts.common import ContractModel, CorrelationId, FrozenModel


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
        min_length=1,
        description=(
            "The HUMAN who authorised the promotion. Agents may propose but never "
            "promote; from Phase 5 this must be an authenticated RBAC principal."
        ),
    )
    evaluation_ref: str = Field(
        min_length=1,
        description="Identifier of the passing offline/human evaluation that justified promotion.",
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
