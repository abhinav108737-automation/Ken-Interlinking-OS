"""Deterministic HTML mutation and verification interface (Phase 6, ADR-0014 section 2).

FROZEN (orchestrator). Pure functions over strings: no I/O, no clock, no randomness, no
LLM. Implemented in ``ken_services.deployment.mutation`` (``insert_links``, ``remove_links``)
and ``ken_services.deployment.verification`` (``verify_links``); the deployment service and
every CMS provider that mutates HTML (mock, simulated) call ONLY these.

Rules (binding):

* Accept either a FRAGMENT (``<main>...</main>``, as the Phase 1 export carries) or a FULL
  document as ``CMSPage.body_html``. Work on that string; locate blocks with the inventory
  block walk (``extract_blocks`` semantics over the same content root and boilerplate), by
  ``context_hash`` (sha256 of block text) ONLY: zero matches -> ``block_not_found``; several
  -> the ``block_id`` hint, else ``ambiguous_block``.
* The anchor is ``phrase_occurrences(anchor_text, block.text)[occurrence_index]`` and must
  equal ``[start, end)``; it must pass ``span_is_safe`` with the given excluded ancestors and
  must not sit under a hidden ancestor (``hidden``, ``aria-hidden="true"``, inline
  ``display:none`` / ``visibility:hidden``).
* Map the span to EXACT offsets in the original string (stdlib
  ``html.parser.HTMLParser(convert_charrefs=False)`` positions); the span must lie in one
  text node, and its two BOUNDARIES must map exactly (interior whitespace runs and entity
  references are preserved verbatim, never rewritten), else refuse.
* SPLICE -- never re-serialise. The output equals the input except the inserted
  ``<a href="ESCAPED" data-ken-link-id="UUID">`` and ``</a>`` (or their removal).
* Identify OUR link by (block ``context_hash`` of its text, anchor span, href) -- NOT by the
  ``data-ken-link-id`` attribute, which is only a hint (a CMS sanitiser may strip ``data-*``).
* Idempotent: our link already present -> ``already_applied`` (no change); an ``<a>`` at the
  span with a different href, or our id with different content -> ``conflicting_link``.
* Self-check after splicing: every block's text and hash unchanged; the set of existing
  ``<a href>`` elements unchanged except the new ones; each inserted span is now a run under
  ``a``; and ``remove_links(insert_links(x)) == x`` byte for byte.
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
    """Our link, located like an insertion (the block text is unchanged by our link)."""

    plan_id: UUID
    expected_href: str
    expected_anchor_text: str
    context_hash: str
    block_id_hint: str
    start: int
    end: int


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
        """Unwrap OUR anchors (inverse splice) on the CURRENT html -- safe after unrelated later
        edits. All-or-nothing; a link already absent is ``already_applied`` (idempotent
        rollback); a link whose href or anchor changed is ``rollback_conflict``."""
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
