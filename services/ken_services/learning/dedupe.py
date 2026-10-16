"""Proposal fingerprints and the idempotence of a reflection re-run (ADR-0016 section 3).

``fingerprint = sha256(canonical_json(fingerprint_payload(...)))``. The payload itself lives in
the FROZEN contract (:func:`~ken_contracts.learning.fingerprint_payload`), so the reflection
job, this dedupe query and the promotion check hash the same object -- the reason that function
is in ``ken_contracts`` and not here.

**Idempotence is the database's, not this module's.** ``ix_learning_proposals_open_fingerprint``
is UNIQUE over exactly ``OPEN_PROPOSAL_STATUSES``, so a second identical proposal cannot exist
while the first is still live, whatever a caller does. :func:`open_proposal_id` is the fast path
that lets a re-run report ``deduplicated`` instead of raising, and
:func:`is_duplicate_fingerprint` recognises the index's own refusal as the same outcome. Both
are necessary: the query alone would race, the index alone would surface as an error.

**Near-duplicate superseding was CUT (ADR-0016 15.3)**, so there is deliberately no
``change_signature`` here and nothing in this package withdraws a proposal. Auto-withdrawing a
proposal a human can already see is a silent state change; ``supersedes`` is set by a human and
by nobody else.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.learning import (
    OPEN_PROPOSAL_STATUSES,
    ProposalKind,
    ProposalStatus,
    TargetKind,
    fingerprint_payload,
)

__all__ = [
    "OPEN_FINGERPRINT_INDEX",
    "canonical_sha256",
    "is_duplicate_fingerprint",
    "open_proposal_id",
    "open_statuses",
    "policy_fingerprint",
    "skill_fingerprint",
]

OPEN_FINGERPRINT_INDEX: Final = "ix_learning_proposals_open_fingerprint"
"""The partial UNIQUE index that MAKES a re-run idempotent; named so a refusal is recognisable
without parsing a driver message."""

_OPEN_STATUSES: Final = tuple(
    sorted(status.value for status in ProposalStatus if status in OPEN_PROPOSAL_STATUSES)
)

_OPEN_SQL: Final = text(
    """
    SELECT p.id AS id
    FROM learning_proposals p
    WHERE p.fingerprint = :fingerprint
      AND p.status = ANY(:statuses)
    LIMIT 1
    """
)


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """``sha256`` of the canonical JSON form (sorted keys, no spaces, ASCII-escaped).

    The same encoding ``review/service.py`` and ``deployment/service.py`` use for a request
    fingerprint, so one convention covers every digest in the system.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def policy_fingerprint(
    *,
    target_kind: TargetKind,
    base_version: str,
    changes: Mapping[str, Any],
) -> str:
    """Fingerprint of a policy proposal: kind, target kind, base version and sorted changes.

    ``changes`` is ``path -> proposed value``. The BASE version is part of the hash, so the same
    change against a newer base document is a different proposal -- which is correct: it is a
    different diff, reviewed against different numbers.
    """
    return canonical_sha256(
        fingerprint_payload(
            kind=ProposalKind.POLICY,
            target_kind=target_kind,
            base_version=base_version,
            change_paths=sorted(changes),
            change_values=dict(sorted(changes.items())),
        )
    )


def skill_fingerprint(
    *,
    name: str,
    instructions: str,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> str:
    """Fingerprint of a skill proposal: the name and its normalised content.

    Instructions are normalised by whitespace only -- reflowing a skill is not a new skill, but
    changing a word is. Phase 8's reflection proposes policies only; this exists so a
    human-authored skill proposal hashes the same way when one is added.
    """
    normalised = " ".join(instructions.split())
    return canonical_sha256(
        fingerprint_payload(
            kind=ProposalKind.SKILL,
            target_kind=None,
            base_version="0",
            change_paths=("instructions", "input_schema", "output_schema"),
            change_values={
                "instructions": normalised,
                "input_schema": dict(sorted(input_schema.items())),
                "output_schema": dict(sorted(output_schema.items())),
            },
            skill_name=name,
        )
    )


async def open_proposal_id(session: AsyncSession, *, fingerprint: str) -> uuid.UUID | None:
    """The id of the OPEN proposal with this fingerprint, or ``None``.

    "Open" is exactly the index's predicate, so this answers the same question the database
    will answer on INSERT. A proposal that was rejected, withdrawn or rolled back does NOT
    block a new one: new evidence deserves a new hypothesis.
    """
    row = (
        await session.execute(
            _OPEN_SQL, {"fingerprint": fingerprint, "statuses": list(_OPEN_STATUSES)}
        )
    ).first()
    return None if row is None else uuid.UUID(str(row[0]))


def is_duplicate_fingerprint(error: IntegrityError) -> bool:
    """Whether ``error`` is the open-fingerprint index refusing a duplicate.

    Matched on the INDEX NAME, which is a schema fact, rather than on a message: a re-run that
    races with itself must report ``deduplicated`` and not fail a job.
    """
    return OPEN_FINGERPRINT_INDEX in str(getattr(error, "orig", error))


def open_statuses() -> Sequence[str]:
    """The index's predicate, for a caller that wants to show it."""
    return _OPEN_STATUSES
