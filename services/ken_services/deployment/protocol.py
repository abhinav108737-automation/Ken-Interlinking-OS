"""Deterministic HTML mutation and verification interface (Phase 6, ADR-0014 section 2).

FROZEN (orchestrator). Pure functions over strings: no I/O, no clock, no randomness, no
LLM. Implemented in ``ken_services.deployment.mutation`` (``insert_links``, ``remove_links``)
and ``ken_services.deployment.verification`` (``verify_links``); the deployment service and
every CMS provider that mutates HTML (mock, simulated) call ONLY these.

Rules (binding):

* Work on the CMS page's FULL HTML string; locate blocks with the inventory block walk
  (``extract_blocks`` semantics over the same content root and boilerplate rules), by
  ``context_hash`` (sha256 of block text), ``block_id`` as a tie-break hint only.
* The anchor is ``phrase_occurrences(anchor_text, block.text)[occurrence_index]`` and must
  equal ``[start, end)``; it must pass ``span_is_safe`` with the given excluded ancestors and
  must not sit under a hidden ancestor (``hidden``, ``aria-hidden="true"``, inline
  ``display:none`` / ``visibility:hidden``).
* Map the span to EXACT offsets in the original string (stdlib ``html.parser`` positions);
  the span must lie in one text node with no character/entity reference, else refuse.
* SPLICE -- never re-serialise. The output equals the input except the inserted
  ``<a href="ESCAPED" data-ken-link-id="UUID">`` and ``</a>`` (or their removal).
* Idempotent: an existing ``data-ken-link-id`` with the same href+anchor -> ``already_applied``
  (no change); with different content -> ``conflicting_link``.
* Self-check after splicing: block texts unchanged; each inserted span now a run under ``a``.
* Total: return issues, never raise, for any input string.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ken_contracts.deployment import DeploymentIssue, VerificationCheck

__all__ = [
    "LinkInsertion",
    "LinkRemoval",
    "LinkVerifier",
    "MutationResult",
    "Mutator",
]


@dataclass(frozen=True, slots=True)
class LinkInsertion:
    plan_id: UUID
    """Also the ``data-ken-link-id`` and the CMS ``change_id``."""
    href: str
    anchor_text: str
    context_hash: str
    block_id_hint: str
    occurrence_index: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class LinkRemoval:
    plan_id: UUID
    expected_href: str
    expected_anchor_text: str


@dataclass(frozen=True, slots=True)
class MutationResult:
    html: str
    """The new HTML (== input when nothing applied)."""
    applied: tuple[UUID, ...] = ()
    already_applied: tuple[UUID, ...] = ()
    issues: tuple[DeploymentIssue, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.issues


class Mutator(Protocol):
    def insert_links(
        self,
        html: str,
        insertions: Sequence[LinkInsertion],
        *,
        excluded_ancestors: Collection[str],
    ) -> MutationResult:
        """All-or-nothing: if ANY insertion has an issue, ``html`` is returned unchanged and
        ``applied`` is empty. Insertions must not overlap (``overlapping_changes``)."""
        ...

    def remove_links(self, html: str, removals: Sequence[LinkRemoval]) -> MutationResult:
        """Unwrap the ``data-ken-link-id`` anchors (inverse splice). All-or-nothing; a link
        already absent is ``already_applied`` (idempotent rollback)."""
        ...


class LinkVerifier(Protocol):
    def verify_links(
        self,
        html: str,
        insertions: Sequence[LinkInsertion],
        *,
        target_reachable: dict[UUID, bool | None] | None = None,
    ) -> list[VerificationCheck]:
        """One check per (plan, VerificationCheckName). ``target_reachable[plan] is None`` or
        missing -> ``target_reachable`` is ``skipped``."""
        ...
