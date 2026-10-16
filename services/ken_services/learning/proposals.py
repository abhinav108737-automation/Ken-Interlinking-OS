"""Build and persist a ``learning_proposals`` row, plus the candidate document (W1).

This module is the FROZEN-FIELD BOUNDARY in service code. ADR-0016 15.2c found the allowlist
"convention, not a contract boundary", and 15.7.5 moved both path sets into the frozen contract;
what is left to enforce here is the rest of the boundary:

1. **A frozen path is refused**, with a typed :class:`CandidateRefusal`, before any model output
   or any option is involved. :class:`~ken_contracts.learning.PolicyChange` refuses it too, so
   the rule holds even if this check were deleted -- but a pydantic error is not an auditable
   refusal, and this is the layer that produces one.
2. **The YAML's NARROWED allowlist is honoured.** ``ken_contracts`` says what may ever be
   learned; ``policies/learning/learning.v1.yaml`` may take entries away. A path the contract
   allows but the operator removed is refused exactly as a frozen path is.
3. **The ADR-0012 section 10 cross-check is refused HERE**, with its own code, so a candidate
   that would make ``agents.v1.yaml`` fail to load cannot be built:
   ``agents.max_plans_per_run <= recommendation.selection.max_plans_per_source``.
4. **The candidate document is validated by its real policy model** and stored WHOLE
   (``policy_candidates.document``), which is what makes an ``offline_test`` replayable without
   a file on disk (ADR-0016 section 5).
5. **A proposal can never claim a human author.** ``proposed_by`` is
   :data:`~ken_contracts.learning.LEARNING_ACTOR`; a DB CHECK pins the shape, and the static
   boundary test forbids this package from even spelling a human actor prefix.

The candidate document is built from the base policy's VALIDATED dump, not from the raw YAML, so
every default is explicit in the stored document and a change can only ever REPLACE a key that
already exists. The only edits are the allowlisted parameters plus the ``version`` bump; nothing
else in the document moves, so the diff a human approves is exactly the diff that was proposed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.agents import AgentPolicy
from ken_contracts.events import EventEnvelope, EventName, PolicyProposedPayload
from ken_contracts.learning import (
    CROSS_CHECKED_PATHS,
    LEARNING_ACTOR,
    LEARNING_EMITTED_BY,
    SUBJECT_LEARNING_PROPOSAL,
    CrossPolicyContext,
    LearningError,
    LearningPolicy,
    PolicyChange,
    PolicyProposalBody,
    ProposalEvidence,
    ProposalStatus,
    TargetKind,
    is_frozen_path,
    is_learnable_path,
)
from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy
from ken_core.db.models.learning import LearningProposal, PolicyCandidate
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_services.learning.dedupe import canonical_sha256, open_proposal_id, policy_fingerprint

__all__ = [
    "AGENTS_PER_RUN_PATH",
    "RECOMMENDATION_PER_SOURCE_PATH",
    "CandidateRefusal",
    "CandidateRefusedError",
    "PreparedProposal",
    "apply_changes",
    "insert_policy_proposal",
    "policy_change",
    "prepare_policy_proposal",
    "read_path",
]

logger = get_logger(__name__)

AGENTS_PER_RUN_PATH: Final = "agents.max_plans_per_run"
RECOMMENDATION_PER_SOURCE_PATH: Final = "recommendation.selection.max_plans_per_source"

_POLICY_MODELS: Final[Mapping[TargetKind, type[BaseModel]]] = {
    TargetKind.RANKING: RankingPolicy,
    TargetKind.RECOMMENDATION: RecommendationPolicy,
    TargetKind.AGENTS: AgentPolicy,
}
"""The model a candidate document must satisfy. ``ranking`` therefore still enforces
non-negative weights that sum to 1, and ``agents`` still enforces its own field bounds -- a
candidate that would not LOAD cannot be stored."""


class CandidateRefusal(StrEnum):
    """Why a candidate was refused at this boundary. A fixed vocabulary, so a refusal is
    loggable and assertable without exception text."""

    FROZEN_FIELD = "frozen_field"
    """The path is in, or beneath, ``FROZEN_POLICY_PATHS``. Never learnable, in any phase."""
    NOT_ALLOWLISTED = "not_allowlisted"
    """Not in ``ALLOWED_CHANGE_PATHS``, or removed from it by the learning policy file."""
    WRONG_TARGET = "wrong_target"
    """The path does not belong to the policy document the proposal targets."""
    NO_CHANGE = "no_change"
    """The proposed value equals the base value: there is nothing to review."""
    UNKNOWN_PATH = "unknown_path"
    """The path names no key in the base document -- a change REPLACES, never introduces."""
    CROSS_CHECK_VIOLATED = "cross_check_violated"
    """The candidate would break ADR-0012 section 10 and make the agents policy unloadable."""
    INVALID_CANDIDATE = "invalid_candidate"
    """The resulting document fails its own policy model (e.g. weights no longer sum to 1)."""


class CandidateRefusedError(LearningError):
    """A refused candidate. Carries the code only -- never a database or pydantic message."""

    def __init__(self, refusal: CandidateRefusal, *, path: str | None = None) -> None:
        super().__init__(refusal.value if path is None else f"{refusal.value}: {path}")
        self.refusal = refusal
        self.path = path


# --------------------------------------------------------------------------- #
# Reading and writing a dotted path in a policy document
# --------------------------------------------------------------------------- #
def _keys(path: str, target_kind: TargetKind) -> tuple[str, ...]:
    prefix = target_kind.value
    if path != prefix and not path.startswith(f"{prefix}."):
        raise CandidateRefusedError(CandidateRefusal.WRONG_TARGET, path=path)
    return tuple(path.removeprefix(f"{prefix}.").split("."))


def read_path(document: Mapping[str, Any], path: str, *, target_kind: TargetKind) -> Any:
    """The value ``path`` names in ``document``.

    Raises:
        CandidateRefusedError: the path belongs to another policy, or names no existing key.
    """
    cursor: Any = document
    for key in _keys(path, target_kind):
        if not isinstance(cursor, Mapping) or key not in cursor:
            raise CandidateRefusedError(CandidateRefusal.UNKNOWN_PATH, path=path)
        cursor = cursor[key]
    return cursor


def apply_changes(
    document: Mapping[str, Any],
    *,
    target_kind: TargetKind,
    changes: Sequence[PolicyChange],
) -> dict[str, Any]:
    """A deep copy of ``document`` with ``changes`` applied and ``version`` bumped by one.

    Raises:
        CandidateRefusedError: a path names no existing key, or the base value does not match
            the change's ``from_value`` (the document moved under the proposal).
    """
    candidate: dict[str, Any] = _deep_copy(document)
    for change in changes:
        keys = _keys(change.path, target_kind)
        cursor: Any = candidate
        for key in keys[:-1]:
            if not isinstance(cursor, dict) or key not in cursor:
                raise CandidateRefusedError(CandidateRefusal.UNKNOWN_PATH, path=change.path)
            cursor = cursor[key]
        leaf = keys[-1]
        if not isinstance(cursor, dict) or leaf not in cursor:
            raise CandidateRefusedError(CandidateRefusal.UNKNOWN_PATH, path=change.path)
        if cursor[leaf] != change.from_value:
            raise CandidateRefusedError(CandidateRefusal.NO_CHANGE, path=change.path)
        cursor[leaf] = change.to_value
    version = candidate.get("version")
    if not isinstance(version, int):
        raise CandidateRefusedError(CandidateRefusal.INVALID_CANDIDATE)
    candidate["version"] = version + 1
    return candidate


def _deep_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_deep_copy(item) for item in value]
    return value


# --------------------------------------------------------------------------- #
# One change, refused at this boundary
# --------------------------------------------------------------------------- #
def policy_change(
    *,
    path: str,
    base_document: Mapping[str, Any],
    to_value: Any,
    target_kind: TargetKind,
    policy: LearningPolicy,
) -> PolicyChange:
    """One validated change, or a typed refusal. THE boundary ADR-0016 15.2c asked for.

    ``from_value`` is read from the base document, so the diff a human sees reconstructs from
    the stored row alone.

    Raises:
        CandidateRefusedError: frozen, not allowlisted (contract or narrowed policy), wrong
            target, unknown path, or not actually a change.
    """
    if is_frozen_path(path):
        raise CandidateRefusedError(CandidateRefusal.FROZEN_FIELD, path=path)
    if not is_learnable_path(path) or path not in set(policy.changes.allowed_paths):
        raise CandidateRefusedError(CandidateRefusal.NOT_ALLOWLISTED, path=path)
    from_value = read_path(base_document, path, target_kind=target_kind)
    if from_value == to_value:
        raise CandidateRefusedError(CandidateRefusal.NO_CHANGE, path=path)
    try:
        return PolicyChange(path=path, from_value=from_value, to_value=to_value)
    except ValidationError as exc:  # pragma: no cover - the checks above pre-empt every case
        raise CandidateRefusedError(CandidateRefusal.NOT_ALLOWLISTED, path=path) from exc


def _cross_check(
    changes: Sequence[PolicyChange], context: CrossPolicyContext | None
) -> CrossPolicyContext | None:
    """Refuse a candidate that would break the ADR-0012 section 10 load-time cross-check.

    The frozen :class:`~ken_contracts.learning.PolicyProposalBody` enforces the same rule, so
    this is belt and braces -- but it is the layer that turns the rule into a typed refusal
    rather than a validation error, and it refuses a MISSING context too.
    """
    touched = {change.path for change in changes} & CROSS_CHECKED_PATHS
    if not touched:
        return None
    if context is None:
        raise CandidateRefusedError(CandidateRefusal.CROSS_CHECK_VIOLATED, path=sorted(touched)[0])
    proposed = {change.path: change.to_value for change in changes}
    cap = proposed.get(RECOMMENDATION_PER_SOURCE_PATH, context.recommendation_max_plans_per_source)
    per_run = proposed.get(AGENTS_PER_RUN_PATH, context.agents_max_plans_per_run)
    if not isinstance(cap, int) or not isinstance(per_run, int) or per_run > cap:
        raise CandidateRefusedError(CandidateRefusal.CROSS_CHECK_VIOLATED, path=sorted(touched)[0])
    return context


# --------------------------------------------------------------------------- #
# A prepared proposal: body, candidate document, fingerprint
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PreparedProposal:
    """Everything needed to persist one proposal. Nothing here came from a model."""

    target_kind: TargetKind
    base_version: str
    body: PolicyProposalBody
    fingerprint: str
    candidate_version: str
    candidate_document: dict[str, Any]
    candidate_sha256: str

    @property
    def change_paths(self) -> tuple[str, ...]:
        return self.body.change_paths


def prepare_policy_proposal(
    *,
    target_kind: TargetKind,
    base_document: Mapping[str, Any],
    changes: Sequence[PolicyChange],
    cross_check: CrossPolicyContext | None = None,
) -> PreparedProposal:
    """Validate the candidate end to end and compute its fingerprint.

    Raises:
        CandidateRefusedError: any boundary refusal, including a candidate document that fails
            its own policy model.
    """
    if not changes:
        raise CandidateRefusedError(CandidateRefusal.NO_CHANGE)
    context = _cross_check(changes, cross_check)
    base_version = base_document.get("version")
    if not isinstance(base_version, int):
        raise CandidateRefusedError(CandidateRefusal.INVALID_CANDIDATE)
    candidate = apply_changes(base_document, target_kind=target_kind, changes=changes)
    model = _POLICY_MODELS[target_kind]
    try:
        validated = model.model_validate(candidate)
    except ValidationError as exc:
        logger.warning(
            "learning_candidate_invalid",
            target_kind=target_kind.value,
            errors=exc.error_count(),
            paths=[change.path for change in changes],
        )
        raise CandidateRefusedError(CandidateRefusal.INVALID_CANDIDATE) from exc
    document = validated.model_dump(mode="json")
    try:
        body = PolicyProposalBody(
            target_kind=target_kind,
            base_version=str(base_version),
            changes=tuple(changes),
            cross_check=context,
        )
    except ValidationError as exc:
        raise CandidateRefusedError(CandidateRefusal.INVALID_CANDIDATE) from exc
    fingerprint = policy_fingerprint(
        target_kind=target_kind,
        base_version=str(base_version),
        changes={change.path: change.to_value for change in changes},
    )
    return PreparedProposal(
        target_kind=target_kind,
        base_version=str(base_version),
        body=body,
        fingerprint=fingerprint,
        candidate_version=f"{base_version + 1}-{fingerprint[:12]}",
        candidate_document=document,
        candidate_sha256=canonical_sha256(document),
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
async def insert_policy_proposal(
    session: AsyncSession,
    *,
    prepared: PreparedProposal,
    evidence: ProposalEvidence,
    title: str,
    rationale: str,
    job_run_id: uuid.UUID,
    now: datetime,
) -> uuid.UUID | None:
    """Insert the proposal, its candidate document and ``policy.proposed``; ``None`` if a
    proposal with this fingerprint is already open.

    ``status`` is ``proposed`` and ``proposed_by`` is the learning system actor: zero production
    effect, and no path from here to ``promoted``. The real idempotence is the partial UNIQUE
    index -- this check only lets a re-run report it instead of raising.
    """
    existing = await open_proposal_id(session, fingerprint=prepared.fingerprint)
    if existing is not None:
        logger.info(
            "learning_proposal_deduplicated",
            proposal_id=str(existing),
            fingerprint=prepared.fingerprint,
        )
        return None

    proposal_id = uuid.uuid4()
    await session.execute(
        insert(LearningProposal).values(
            id=proposal_id,
            kind=prepared.body.kind.value,
            status=ProposalStatus.PROPOSED.value,
            target_kind=prepared.target_kind.value,
            base_version=prepared.base_version,
            title=title,
            rationale=rationale,
            body=prepared.body.model_dump(mode="json"),
            evidence=evidence.model_dump(mode="json"),
            fingerprint=prepared.fingerprint,
            proposed_by=LEARNING_ACTOR,
            job_run_id=job_run_id,
            model=evidence.model,
            fallback_used=evidence.fallback_used,
            created_at=now,
            updated_at=now,
        )
    )
    await session.execute(
        insert(PolicyCandidate).values(
            id=uuid.uuid4(),
            proposal_id=proposal_id,
            policy_kind=prepared.target_kind.value,
            base_version=prepared.base_version,
            candidate_version=prepared.candidate_version,
            document=prepared.candidate_document,
            document_sha256=prepared.candidate_sha256,
            created_at=now,
        )
    )
    payload = PolicyProposedPayload(
        proposal_id=proposal_id,
        fingerprint=prepared.fingerprint,
        target_kind=prepared.target_kind,
        base_version=prepared.base_version,
        change_paths=prepared.change_paths,
        proposed_by=LEARNING_ACTOR,
        job_run_id=job_run_id,
        evidence_window=evidence.window,
        model=evidence.model,
        fallback_used=evidence.fallback_used,
    )
    await append_event(
        session,
        EventEnvelope(
            event_name=EventName.POLICY_PROPOSED,
            occurred_at=now,
            emitted_by=LEARNING_EMITTED_BY,
            subject_type=SUBJECT_LEARNING_PROPOSAL,
            subject_id=str(proposal_id),
            payload=payload.model_dump(mode="json"),
            correlation_id=job_run_id,
        ),
    )
    logger.info(
        "learning_proposal_created",
        proposal_id=str(proposal_id),
        target_kind=prepared.target_kind.value,
        change_paths=list(prepared.change_paths),
        fingerprint=prepared.fingerprint,
        fallback_used=evidence.fallback_used,
    )
    return proposal_id
