"""In-memory :class:`~ken_contracts.cms.CMSProvider` (unit tests; ``KEN_CMS_PROVIDER=mock``).

Phase 0 shipped this mock to validate the PROTOCOL -- idempotency, optimistic locking, dry
run, protected pages, audit and rollback -- without touching ``body_html``. **Phase 6
(ADR-0014 s4/s8) upgrades it**: given a :class:`~ken_core.providers.cms.html_engine.
LinkHtmlEngine` (the deterministic mutation service, injected by the composition root), an
apply performs the REAL splice on ``body_html`` and a rollback is the inverse splice. Without
an engine it keeps the Phase 0 protocol-only behaviour (the version token changes, the body
does not), which the protocol tests still exercise.

There is no network client here and there must never be one: a real CMS gets
its own provider behind the same protocol.

Semantics
=========

Version tokens
    Opaque strings (``CMSPage.version``). This mock mints a random token per
    state change. Callers must never parse, order or increment them; the only
    legal operation is an equality check against ``expected_version``.

Validation (``validate_link_changes``)
    Pure: it reads current state and mutates nothing, not even the idempotency
    ledger, which it does not consult. Issue codes are the stable values of
    :class:`IssueCode`. ``normalized_changes`` echoes every change that has no
    change-scoped error, and is empty when the request itself is unusable
    (unknown or protected source page, stale version, missing actor).

Apply (``apply_link_changes``), in order
    1. Idempotency ledger. An ``operation_id`` that was already *applied*
       with an identical request (compared on every field except
       ``dry_run``) returns the stored result with ``idempotent_replay=True``
       and does nothing else, even if the page has moved since. The same
       ``operation_id`` with a different request raises
       :class:`CMSValidationError` with ``operation_id_reused``. Rejected
       operations are not recorded, so a rejected ``operation_id`` may be
       retried.
    2. Terminal request errors (unknown source page, protected source page,
       missing actor) raise :class:`CMSValidationError`. These come first
       because regenerating the plan cannot fix them.
    3. ``expected_version`` mismatch raises :class:`CMSVersionConflictError`
       (TRD.md section 13's 409): the caller must regenerate from current
       content, so change-level errors computed against stale content are not
       reported.
    4. Any remaining error raises :class:`CMSValidationError`, including (with an
       engine) the splice's own refusals on the CURRENT body (``block_not_found``,
       ``conflicting_link`` ...).
    5. ``dry_run=True`` runs every check above, the splice included, and returns
       ``status="no_op"`` with ``new_version == previous_version`` and an unrecorded
       ``dryrun-*`` deployment id. Nothing is persisted, no audit entry is written and the
       ``operation_id`` is not consumed. A dry run of an already-applied
       identical request reports ``no_op`` with ``idempotent_replay=True``
       and the original deployment id, because that is what a real apply
       would do.
    6. Success records the deployment. With an engine, ``applied_change_ids`` lists only
       the changes the splice actually APPLIED (a change already in that state -- our link
       already present, a removed link already absent -- is left out); when nothing
       changed the result is ``status="no_op"`` with the version unchanged (the operation
       is still recorded, so it replays). Otherwise ``status="applied"`` and a new version
       token. Without an engine every change is listed and the version always moves.

Rollback (ADR-0014 s8 -- SUPERSEDES the Phase 0 rule "page moved -> conflict")
    ``operation_id`` must be the one the deployment was applied with. When the page is
    still at the deployment's ``new_version``, the pre-deployment snapshot is restored.
    Otherwise (an unrelated later edit, or a later deployment of ours) the rollback is the
    INVERSE SPLICE of the changes this deployment APPLIED on the CURRENT html
    (:func:`~ken_core.providers.cms.html_engine.revert_deployment`: an INSERT is unwrapped, a
    REMOVE re-inserted), under the provider lock: safe after unrelated edits. It is refused
    only when one of OUR links itself changed: :class:`CMSValidationError` with
    ``rollback_conflict`` (and, without an engine, whenever the page moved, since nothing can
    prove the later edit unrelated). ``restored_version`` is a NEW version token, never the
    pre-deployment one: a rollback is a new state, and reissuing an old token would let a
    client holding a stale plan pass the optimistic lock. A deployment that applied nothing
    (``status="no_op"``) rolls back as ``no_op`` without touching the page
    (``restored_version`` is the current version). A second rollback of the same deployment
    returns the original rollback's result with ``status="no_op"``.

URL mapping
    :meth:`MockCMSProvider.resolve_page_id` maps a page URL (``url`` or ``canonical_url``)
    to its ``page_id``; the deployment service uses it for inventory pages that carry no CMS
    ``external_id`` (ADR-0014 s8 "CMS page resolution").

Concurrency
    One :class:`asyncio.Lock` serialises every apply and rollback. Each
    critical section yields to the event loop between check and commit, as a
    network-backed CMS would, so the lock is genuinely load-bearing rather
    than incidentally satisfied by asyncio's cooperative scheduling.

Audit
    Every non-dry-run apply and every rollback attempt, including rejected
    ones, appends an :class:`AuditEntry`, exposed read-only via
    :attr:`MockCMSProvider.audit_log`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, Self
from uuid import UUID, uuid4

from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    DeploymentResult,
    LinkChange,
    LinkChangeRequest,
    LinkOperation,
    RollbackResult,
    ValidationIssue,
    ValidationResult,
)
from ken_contracts.common import OperationId
from ken_contracts.model import ProviderHealth
from ken_core.providers.cms.html_engine import HtmlOutcome, LinkHtmlEngine, revert_deployment

PROVIDER_NAME: Final = "mock"


class IssueCode(StrEnum):
    """Stable validation issue codes. Clients branch on these, never on messages."""

    UNKNOWN_SOURCE_PAGE = "unknown_source_page"
    PROTECTED_PAGE = "protected_page"
    MISSING_ACTOR = "missing_actor"
    STALE_PAGE_VERSION = "stale_page_version"
    UNKNOWN_TARGET_PAGE = "unknown_target_page"
    SELF_LINK = "self_link"
    INSERT_HAS_PREVIOUS_FIELDS = "insert_has_previous_fields"
    MISSING_PREVIOUS_HREF = "missing_previous_href"
    MISSING_PREVIOUS_ANCHOR_TEXT = "missing_previous_anchor_text"
    DUPLICATE_CHANGE_ID = "duplicate_change_id"
    OPERATION_ID_REUSED = "operation_id_reused"
    DEPLOYMENT_OPERATION_MISMATCH = "deployment_operation_mismatch"
    ROLLBACK_CONFLICT = "rollback_conflict"
    """ADR-0014 s8: one of OUR links changed since the deployment; nothing was removed."""


# Request-level errors that regenerating the plan cannot fix. They take
# precedence over a version conflict (see module docstring, "Apply").
_TERMINAL_CODES: Final = frozenset(
    {IssueCode.UNKNOWN_SOURCE_PAGE, IssueCode.PROTECTED_PAGE, IssueCode.MISSING_ACTOR}
)

AuditAction = Literal["apply", "rollback"]
AuditOutcome = Literal[
    "applied",
    "idempotent_replay",
    "rejected_validation",
    "rejected_version_conflict",
    "rejected_not_found",
    "rolled_back",
    "rollback_no_op",
]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One audited apply or rollback attempt."""

    action: AuditAction
    outcome: AuditOutcome
    operation_id: UUID
    actor: str
    at: datetime
    page_id: str | None = None
    deployment_id: str | None = None
    issue_codes: tuple[str, ...] = ()


@dataclass(slots=True)
class _DeploymentRecord:
    result: DeploymentResult
    fingerprint: dict[str, Any]
    page_before: CMSPage
    changes: tuple[LinkChange, ...] = ()
    rollback: RollbackResult | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_version_token() -> str:
    # Opaque by contract; random so nothing can be inferred from it.
    return f"mock-{uuid4().hex}"


def _fingerprint(request: LinkChangeRequest) -> dict[str, Any]:
    """Request identity for idempotency. ``dry_run`` is excluded: it changes
    whether a request is executed, not what the request is."""
    return request.model_dump(mode="json", exclude={"dry_run"})


def _issue(code: IssueCode, message: str, change_id: UUID | None = None) -> ValidationIssue:
    return ValidationIssue(code=code.value, severity="error", message=message, change_id=change_id)


class MockCMSProvider:
    """In-memory CMS implementing :class:`~ken_contracts.cms.CMSProvider`.

    State lives only as long as the instance. See the module docstring for the
    full behavioural contract.
    """

    name: str = PROVIDER_NAME
    preserves_data_attributes: bool = True
    """ADR-0014 s8: whether ``data-*`` attributes survive this CMS (here: always)."""

    def __init__(
        self,
        pages: Iterable[CMSPage],
        *,
        clock: Callable[[], datetime] | None = None,
        html_engine: LinkHtmlEngine | None = None,
    ) -> None:
        self._pages: dict[str, CMSPage] = {}
        for page in pages:
            if page.page_id in self._pages:
                raise ValueError(f"Duplicate page_id in seed data: {page.page_id!r}")
            # Deep copy: the caller keeps no handle on our internal state.
            self._pages[page.page_id] = page.model_copy(deep=True)
        self._deployments: dict[str, _DeploymentRecord] = {}
        self._operations: dict[UUID, str] = {}
        self._audit: list[AuditEntry] = []
        self._lock = asyncio.Lock()
        self._clock = clock or _utc_now
        self._closed = False
        self._engine = html_engine

    @classmethod
    def from_fixture_dir(
        cls,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        html_engine: LinkHtmlEngine | None = None,
    ) -> Self:
        """Load every ``*.json`` file in ``path`` (non-recursive) as a :class:`CMSPage`.

        Raises:
            NotADirectoryError: ``path`` is not a directory.
            ValueError: no pages were found, or a page_id is duplicated.
            pydantic.ValidationError: a file is not a valid ``CMSPage``.
        """
        directory = Path(path)
        if not directory.is_dir():
            raise NotADirectoryError(f"CMS fixture directory not found: {directory}")
        files = sorted(directory.glob("*.json"))
        if not files:
            raise ValueError(f"No *.json page fixtures in {directory}")
        pages = [CMSPage.model_validate_json(f.read_text(encoding="utf-8")) for f in files]
        return cls(pages, clock=clock, html_engine=html_engine)

    # ------------------------------------------------------------- inspection
    @property
    def audit_log(self) -> tuple[AuditEntry, ...]:
        """Every audited apply/rollback attempt, oldest first. Read-only."""
        return tuple(self._audit)

    async def resolve_page_id(self, *, url: str) -> str | None:
        """The ``page_id`` whose ``url`` or ``canonical_url`` is ``url`` (exact), else None."""
        self._ensure_open()
        for page in self._pages.values():
            if url in (page.url, page.canonical_url):
                return page.page_id
        return None

    def edit_page(self, *, page_id: str, body_html: str) -> CMSPage:
        """TEST/SIMULATION helper: an editor's unrelated change (a new version token)."""
        page = self._pages[page_id]
        edited = page.model_copy(
            update={
                "body_html": body_html,
                "version": _new_version_token(),
                "updated_at": self._clock(),
            },
            deep=True,
        )
        self._pages[page_id] = edited
        return edited.model_copy(deep=True)

    # --------------------------------------------------------------- protocol
    async def get_page(self, *, page_id: str) -> CMSPage:
        self._ensure_open()
        page = self._pages.get(page_id)
        if page is None:
            raise CMSNotFoundError(f"No page with id {page_id!r}")
        return page.model_copy(deep=True)

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> ValidationResult:
        self._ensure_open()
        async with self._lock:
            issues, normalized = self._collect_issues(request)
            if not issues:
                outcome = self._mutate(request)
                if outcome is not None and not outcome.ok:
                    issues = list(outcome.issues)
                    failed = {i.change_id for i in issues}
                    normalized = [c for c in normalized if c.change_id not in failed]
        return ValidationResult(
            valid=not any(i.severity == "error" for i in issues),
            issues=issues,
            normalized_changes=normalized,
        )

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        self._ensure_open()
        async with self._lock:
            replay = self._check_ledger(request)
            if replay is not None:
                return replay

            issues, _ = self._collect_issues(request)
            # Yield between check and commit, as a network round-trip would.
            # Without self._lock this is a check-then-act race.
            await asyncio.sleep(0)
            self._raise_for_issues(request, issues)

            page = self._pages[request.source_page_id]
            now = self._clock()
            outcome = self._mutate(request)
            if outcome is not None and not outcome.ok:
                if not request.dry_run:
                    self._record_apply_rejection(
                        request, "rejected_validation", list(outcome.issues)
                    )
                raise CMSValidationError(
                    "Link change request rejected", issues=list(outcome.issues)
                )
            if request.dry_run:
                return DeploymentResult(
                    deployment_id=f"dryrun-{uuid4().hex}",
                    operation_id=request.operation_id,
                    source_page_id=request.source_page_id,
                    status="no_op",
                    previous_version=page.version,
                    new_version=page.version,
                    applied_at=now,
                )

            changed = outcome is None or outcome.changed
            if changed:
                update: dict[str, Any] = {"version": _new_version_token(), "updated_at": now}
                if outcome is not None:
                    update["body_html"] = outcome.html
                new_page = page.model_copy(update=update, deep=True)
            else:
                new_page = page
            applied_ids = (
                [c.change_id for c in request.changes]
                if outcome is None
                else [c.change_id for c in request.changes if c.change_id in set(outcome.applied)]
            )
            result = DeploymentResult(
                deployment_id=f"dep-{uuid4().hex}",
                operation_id=request.operation_id,
                source_page_id=request.source_page_id,
                status="applied" if changed else "no_op",
                applied_change_ids=applied_ids,
                previous_version=page.version,
                new_version=new_page.version,
                applied_at=now,
            )
            self._deployments[result.deployment_id] = _DeploymentRecord(
                result=result,
                fingerprint=_fingerprint(request),
                page_before=page.model_copy(deep=True),
                changes=tuple(c.model_copy(deep=True) for c in request.changes),
            )
            self._operations[request.operation_id] = result.deployment_id
            self._pages[request.source_page_id] = new_page
            self._record(
                "apply",
                "applied",
                request.operation_id,
                request.actor,
                page_id=request.source_page_id,
                deployment_id=result.deployment_id,
            )
            return result.model_copy(deep=True)

    async def rollback(
        self, *, deployment_id: str, operation_id: OperationId, actor: str
    ) -> RollbackResult:
        self._ensure_open()
        async with self._lock:
            record = self._deployments.get(deployment_id)
            if record is None:
                self._record(
                    "rollback",
                    "rejected_not_found",
                    operation_id,
                    actor,
                    deployment_id=deployment_id,
                )
                raise CMSNotFoundError(f"No deployment with id {deployment_id!r}")

            page_id = record.result.source_page_id
            if not actor.strip():
                self._reject_rollback(
                    record,
                    operation_id,
                    actor,
                    _issue(IssueCode.MISSING_ACTOR, "A rollback must name its authoriser."),
                )
            if record.result.operation_id != operation_id:
                self._reject_rollback(
                    record,
                    operation_id,
                    actor,
                    _issue(
                        IssueCode.DEPLOYMENT_OPERATION_MISMATCH,
                        "operation_id does not match the operation that created this deployment.",
                    ),
                )

            if record.rollback is not None:
                self._record(
                    "rollback",
                    "rollback_no_op",
                    operation_id,
                    actor,
                    page_id=page_id,
                    deployment_id=deployment_id,
                )
                return record.rollback.model_copy(update={"status": "no_op"})

            current = self._pages[page_id]
            await asyncio.sleep(0)  # see apply_link_changes
            now = self._clock()
            if not record.result.applied_change_ids:
                # The deployment changed nothing: there is nothing of ours to take out.
                record.rollback = RollbackResult(
                    deployment_id=deployment_id,
                    status="no_op",
                    restored_version=current.version,
                    rolled_back_at=now,
                    actor=actor,
                )
                self._record(
                    "rollback",
                    "rollback_no_op",
                    operation_id,
                    actor,
                    page_id=page_id,
                    deployment_id=deployment_id,
                )
                return record.rollback.model_copy()
            if current.version == record.result.new_version:
                # Nothing happened since: the snapshot IS the pre-deployment page.
                body = record.page_before.body_html
            else:
                reverted = self._revert(current, record)
                if not reverted.ok:
                    self._record(
                        "rollback",
                        "rejected_validation",
                        operation_id,
                        actor,
                        page_id=page_id,
                        deployment_id=deployment_id,
                        issue_codes=tuple(i.code for i in reverted.issues),
                    )
                    raise CMSValidationError("Rollback rejected", issues=list(reverted.issues))
                body = reverted.html
            restored = current.model_copy(
                update={"body_html": body, "version": _new_version_token(), "updated_at": now},
                deep=True,
            )
            self._pages[page_id] = restored
            record.rollback = RollbackResult(
                deployment_id=deployment_id,
                status="rolled_back",
                restored_version=restored.version,
                rolled_back_at=now,
                actor=actor,
            )
            self._record(
                "rollback",
                "rolled_back",
                operation_id,
                actor,
                page_id=page_id,
                deployment_id=deployment_id,
            )
            return record.rollback.model_copy()

    async def health(self) -> ProviderHealth:
        if self._closed:
            return ProviderHealth(provider=PROVIDER_NAME, reachable=False, error="provider closed")
        return ProviderHealth(
            provider=PROVIDER_NAME,
            reachable=True,
            latency_ms=0.0,
            detail={"pages": len(self._pages), "deployments": len(self._deployments)},
        )

    async def aclose(self) -> None:
        self._closed = True

    # ---------------------------------------------------------------- helpers
    def _mutate(self, request: LinkChangeRequest) -> HtmlOutcome | None:
        """The engine's splice of ``request`` on the CURRENT body (None without an engine)."""
        if self._engine is None:
            return None
        page = self._pages[request.source_page_id]
        return self._engine.apply(page.body_html, request.changes)

    def _revert(self, current: CMSPage, record: _DeploymentRecord) -> HtmlOutcome:
        if self._engine is None:
            issue = _issue(
                IssueCode.ROLLBACK_CONFLICT,
                "The page changed since this deployment and no HTML engine can prove the "
                "change unrelated.",
            )
            return HtmlOutcome(html=current.body_html, issues=(issue,))
        return revert_deployment(
            self._engine, current.body_html, record.changes, record.result.applied_change_ids
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise CMSUnavailableError("MockCMSProvider has been closed")

    def _check_ledger(self, request: LinkChangeRequest) -> DeploymentResult | None:
        """Resolve a previously applied ``operation_id``; None if it is new."""
        deployment_id = self._operations.get(request.operation_id)
        if deployment_id is None:
            return None
        record = self._deployments[deployment_id]
        if record.fingerprint != _fingerprint(request):
            issues = [
                _issue(
                    IssueCode.OPERATION_ID_REUSED,
                    "operation_id was already applied with a different request.",
                )
            ]
            if not request.dry_run:
                self._record_apply_rejection(request, "rejected_validation", issues)
            raise CMSValidationError("operation_id reused with a different request", issues=issues)

        if request.dry_run:
            current = self._pages[record.result.source_page_id].version
            return DeploymentResult(
                deployment_id=deployment_id,
                operation_id=request.operation_id,
                source_page_id=request.source_page_id,
                status="no_op",
                previous_version=current,
                new_version=current,
                applied_at=self._clock(),
                idempotent_replay=True,
            )
        self._record(
            "apply",
            "idempotent_replay",
            request.operation_id,
            request.actor,
            page_id=request.source_page_id,
            deployment_id=deployment_id,
        )
        return record.result.model_copy(update={"idempotent_replay": True}, deep=True)

    def _collect_issues(
        self, request: LinkChangeRequest
    ) -> tuple[list[ValidationIssue], list[LinkChange]]:
        issues: list[ValidationIssue] = []
        source_id = request.source_page_id
        page = self._pages.get(source_id)
        if page is None:
            issues.append(
                _issue(IssueCode.UNKNOWN_SOURCE_PAGE, f"No source page with id {source_id!r}.")
            )
        else:
            if page.protected:
                issues.append(_issue(IssueCode.PROTECTED_PAGE, f"Page {source_id!r} is protected."))
            if page.version != request.expected_version:
                issues.append(
                    _issue(
                        IssueCode.STALE_PAGE_VERSION,
                        "expected_version does not match the page's current version; "
                        "regenerate from current content.",
                    )
                )
        if not request.actor.strip():
            issues.append(_issue(IssueCode.MISSING_ACTOR, "A mutation must name its authoriser."))
        request_unusable = bool(issues)

        normalized: list[LinkChange] = []
        seen: set[UUID] = set()
        for change in request.changes:
            change_issues = self._change_issues(source_id, change, seen)
            seen.add(change.change_id)
            issues.extend(change_issues)
            if not change_issues:
                normalized.append(change.model_copy(deep=True))
        return issues, ([] if request_unusable else normalized)

    def _change_issues(
        self, source_id: str, change: LinkChange, seen: set[UUID]
    ) -> list[ValidationIssue]:
        cid = change.change_id
        issues: list[ValidationIssue] = []
        if cid in seen:
            issues.append(
                _issue(IssueCode.DUPLICATE_CHANGE_ID, "change_id repeated in this request.", cid)
            )
        if change.target_page_id == source_id:
            issues.append(_issue(IssueCode.SELF_LINK, "A page may not link to itself.", cid))
        elif change.target_page_id not in self._pages:
            issues.append(
                _issue(
                    IssueCode.UNKNOWN_TARGET_PAGE,
                    f"No target page with id {change.target_page_id!r}.",
                    cid,
                )
            )
        if change.operation is LinkOperation.INSERT:
            if change.previous_href is not None or change.previous_anchor_text is not None:
                issues.append(
                    _issue(
                        IssueCode.INSERT_HAS_PREVIOUS_FIELDS,
                        "INSERT must not set previous_href or previous_anchor_text.",
                        cid,
                    )
                )
        else:
            if change.previous_href is None:
                issues.append(
                    _issue(
                        IssueCode.MISSING_PREVIOUS_HREF,
                        f"{change.operation.value.upper()} requires previous_href.",
                        cid,
                    )
                )
            if change.previous_anchor_text is None:
                issues.append(
                    _issue(
                        IssueCode.MISSING_PREVIOUS_ANCHOR_TEXT,
                        f"{change.operation.value.upper()} requires previous_anchor_text "
                        "so the change is reversible from its payload alone.",
                        cid,
                    )
                )
        return issues

    def _raise_for_issues(self, request: LinkChangeRequest, issues: list[ValidationIssue]) -> None:
        """Apply the precedence rules from the module docstring."""
        errors = [i for i in issues if i.severity == "error"]
        if not errors:
            return
        codes = {i.code for i in errors}
        if codes & _TERMINAL_CODES or IssueCode.STALE_PAGE_VERSION not in codes:
            if not request.dry_run:
                self._record_apply_rejection(request, "rejected_validation", errors)
            raise CMSValidationError("Link change request rejected", issues=issues)

        current = self._pages[request.source_page_id].version
        if not request.dry_run:
            self._record_apply_rejection(request, "rejected_version_conflict", errors)
        raise CMSVersionConflictError(
            "Page version has changed; regenerate from current content.",
            expected=request.expected_version,
            current=current,
        )

    def _reject_rollback(
        self,
        record: _DeploymentRecord,
        operation_id: UUID,
        actor: str,
        issue: ValidationIssue,
    ) -> NoReturn:
        self._record(
            "rollback",
            "rejected_validation",
            operation_id,
            actor,
            page_id=record.result.source_page_id,
            deployment_id=record.result.deployment_id,
            issue_codes=(issue.code,),
        )
        raise CMSValidationError("Rollback rejected", issues=[issue])

    def _record_apply_rejection(
        self,
        request: LinkChangeRequest,
        outcome: AuditOutcome,
        issues: list[ValidationIssue],
    ) -> None:
        self._record(
            "apply",
            outcome,
            request.operation_id,
            request.actor,
            page_id=request.source_page_id,
            issue_codes=tuple(i.code for i in issues),
        )

    def _record(
        self,
        action: AuditAction,
        outcome: AuditOutcome,
        operation_id: UUID,
        actor: str,
        *,
        page_id: str | None = None,
        deployment_id: str | None = None,
        issue_codes: tuple[str, ...] = (),
    ) -> None:
        self._audit.append(
            AuditEntry(
                action=action,
                outcome=outcome,
                operation_id=operation_id,
                actor=actor,
                at=self._clock(),
                page_id=page_id,
                deployment_id=deployment_id,
                issue_codes=issue_codes,
            )
        )
