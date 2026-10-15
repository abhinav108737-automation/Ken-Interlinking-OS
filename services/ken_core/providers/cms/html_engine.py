"""The seam between CMS providers that store HTML and the deterministic mutation service.

Providers that mutate HTML themselves (``MockCMSProvider``, ``SimulatedCMSProvider``) receive
a :class:`LinkHtmlEngine` at construction. The implementation is
``ken_services.deployment.changes.ChangeEngine``, which wraps the injected pure ``Mutator``
(ADR-0014 s2) and maps a :class:`~ken_contracts.cms.LinkChange` (block ``context_hash``,
``occurrence_index``, anchor, href) onto it. ``ken_core`` never imports ``ken_services``: the
composition root (``factory.py``) resolves the engine by name, and tests inject doubles.

Every method is pure (strings in, strings out), never raises on malformed HTML, and is
ALL-OR-NOTHING: any issue means ``html`` is returned unchanged.

:func:`revert_deployment` is the providers' shared rollback splice (ADR-0014 s8): the inverse
of exactly the changes a deployment APPLIED, on the CURRENT html.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import UUID

from ken_contracts.cms import LinkChange, LinkOperation, ValidationIssue

__all__ = ["HtmlOutcome", "LinkHtmlEngine", "restoring_insert", "revert_deployment"]


@dataclass(frozen=True, slots=True)
class HtmlOutcome:
    html: str
    """The new HTML (== the input when nothing changed or any issue was found)."""
    applied: tuple[UUID, ...] = ()
    already: tuple[UUID, ...] = ()
    """Changes that were already in that state (an INSERT present, a removal absent)."""
    issues: tuple[ValidationIssue, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def changed(self) -> bool:
        return bool(self.applied)


@runtime_checkable
class LinkHtmlEngine(Protocol):
    def apply(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome:
        """Apply INSERT and REMOVE changes (REPLACE is refused: ``unsupported_operation``).
        Issue codes are ``DeploymentIssueCode`` values (``block_not_found``,
        ``conflicting_link``, ``rollback_conflict`` ...)."""
        ...

    def revert(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome:
        """The inverse of :meth:`apply` for INSERT changes: unwrap OUR links on the CURRENT
        html (safe after unrelated later edits). A link already absent counts as
        ``already``; a link whose href or anchor changed is ``rollback_conflict``."""
        ...


def restoring_insert(change: LinkChange) -> LinkChange:
    """The INSERT that undoes a REMOVE ``change``: it re-creates the link the REMOVE took out
    (``previous_href`` / ``previous_anchor_text``; same id, block hash and occurrence)."""
    return change.model_copy(
        update={
            "operation": LinkOperation.INSERT,
            "href": change.previous_href or change.href,
            "anchor_text": change.previous_anchor_text or change.anchor_text,
            "previous_href": None,
            "previous_anchor_text": None,
        },
        deep=True,
    )


def revert_deployment(
    engine: LinkHtmlEngine,
    html: str,
    changes: Sequence[LinkChange],
    applied: Collection[UUID],
) -> HtmlOutcome:
    """The inverse splice of a deployment on the CURRENT ``html`` (ADR-0014 s8).

    Only the changes the deployment actually APPLIED (``applied``: its
    ``DeploymentResult.applied_change_ids``) are reverted -- a change that was already in
    place belongs to an earlier deployment and is left alone. An applied INSERT is unwrapped
    (:meth:`LinkHtmlEngine.revert`); an applied REMOVE is re-inserted
    (:func:`restoring_insert`). All-or-nothing: any issue returns ``html`` unchanged with the
    engine's issues (``rollback_conflict`` when one of OUR links itself changed).
    """
    chosen = [c for c in changes if c.change_id in applied]
    inserts = [c for c in chosen if c.operation is LinkOperation.INSERT]
    removes = [c for c in chosen if c.operation is LinkOperation.REMOVE]
    current = html
    done: list[UUID] = []
    already: list[UUID] = []
    if inserts:
        unwrapped = engine.revert(current, inserts)
        if not unwrapped.ok:
            return HtmlOutcome(html=html, issues=unwrapped.issues)
        current = unwrapped.html
        done += unwrapped.applied
        already += unwrapped.already
    if removes:
        restored = engine.apply(current, [restoring_insert(c) for c in removes])
        if not restored.ok:
            return HtmlOutcome(html=html, issues=restored.issues)
        current = restored.html
        done += restored.applied
        already += restored.already
    return HtmlOutcome(html=current, applied=tuple(done), already=tuple(already))
