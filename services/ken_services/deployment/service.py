"""The controlled deployment service (ADR-0014 s1 and s8): :class:`DatabaseDeploymentService`.

Implements :class:`ken_contracts.deployment.DeploymentService`. Deterministic: no LLM, no
agent and no job calls it; only an operator request does (AUTO_APPROVE stays disabled).

**Principal-based.** ``actor`` (``user:<username>``) and ``user_id`` are derived from the
session principal; ``Permission.DEPLOY`` is re-asserted (defence in depth). Plans move ONLY
through the Phase 3 transition service as :data:`DEPLOYMENT_ACTOR`, with the user in the note.

**Per-source deployment lock.** A session-level ``pg_try_advisory_lock`` on
``sha256("ken.deploy.source:" + id)`` on a DEDICATED connection is held across transaction 1,
the CMS call, transaction 2 and the verification; a concurrent request on the same source
gets the retryable 409 ``deployment_in_progress`` and its plans are left unchanged. Each
transaction also takes the ADR-0013 per-source plan-writer xact lock
(``validity.lock_source_plans``) before plan row locks.

**Deploy = two transactions around the CMS call (durable intent).**

1. tx1: plan rows locked; validation (:meth:`_validate`); plans ``approved -> validated``;
   the ``deployments`` row ``applying`` with the EXACT serialised ``LinkChangeRequest`` and
   the request fingerprint; ``requested`` items. A refusal writes nothing, except that STALE
   plans are expired (``approved -> expired``).
2. ``CMSProvider.apply_link_changes`` with the stored request.
3. tx2 (:meth:`_record_outcome`): applied -> deployment ``applied``, plans ``deployed`` and
   ``link.deployed``; CMS validation error -> ``failed``, plans ``failed`` and
   ``link.failed``; version conflict -> ``conflict``, plans ``expired``. A timeout or unknown
   outcome LEAVES THE ROW ``applying`` (503): reconciliation (:meth:`reconcile`, ``make
   deployments-reconcile``) or a client retry with the same ``operation_id`` resends the
   stored request byte for byte, and every provider replays an applied operation.

**Idempotency.** Fingerprint = sha256 of the canonical JSON of {sorted ``plan_ids``, note,
user}. Every outcome that wrote a ``deployments`` row -- ``applied`` (then verified),
``failed`` (the CMS refused) and ``conflict`` (version mismatch; plans expired) -- is returned
as its :class:`DeploymentDetail` (200), and a stored operation with the same fingerprint
REPLAYS it with the same status code and ``idempotent_replay``; a different fingerprint is a
409. Refusals before any write (validation issues) are 409 ``conflict``; an unknown CMS
outcome (row left ``applying``) is 503. A dry run never records or consumes an
``operation_id``.

**Verification** (:meth:`_verify_once`): the CMS page read back (and, when
``KEN_DEPLOY_VERIFY_BASE_URL`` is set, the published page and every target over HTTP)
through the injected ``LinkVerifier``. Deterministic CMS-side/HTML failures move
``deployed -> failed`` (``link.failed``) and the deployment to ``verification_failed``;
inconclusive checks (network, 5xx, timeout, publish lag) never fail a plan; a bounded
retry runs at deploy time. Re-verifying a ``verified`` plan records a run and may move the
DEPLOYMENT to ``verification_failed`` with no plan transition.

**Rollback** (two-phase ``rolling_back -> rolled_back``): the whole deployment through
``CMSProvider.rollback`` (inverse splice on the CMS's current HTML), or chosen plans through
a REMOVE ``LinkChangeRequest`` (per-plan rollback; stored on the row for reconciliation).
A ``rollback_conflict`` from the CMS returns the row to its prior status, plans unchanged;
``failed`` / ``conflict`` deployments are ``nothing_to_roll_back``; rolling back a rolled-back
deployment is a no-op. Events: ``LinkDeployedPayload`` / ``LinkVerifiedPayload`` /
``LinkFailedPayload`` / ``LinkRolledBackPayload``, ``correlation_id`` = the operation id, in
the transaction of the status change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSProvider,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    DeploymentResult,
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
    ValidationIssue,
)
from ken_contracts.deployment import (
    DEPLOYMENT_ACTOR,
    ChangePreview,
    CMSUnavailableDeploymentError,
    DeploymentDetail,
    DeploymentIssue,
    DeploymentIssueCode,
    DeploymentNotFoundError,
    DeploymentRefusedError,
    DeploymentStatus,
    DeploymentVersionConflictError,
    DeployRequest,
    DryRunRequest,
    DryRunResult,
    LinkDeployedPayload,
    LinkFailedPayload,
    LinkRolledBackPayload,
    LinkVerifiedPayload,
    RollbackReason,
    RollbackRequest,
    VerificationCheck,
    VerificationCheckName,
    VerificationOutcome,
)
from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.intelligence import TaxonomyPolicy, analysis_version
from ken_contracts.recommendations import (
    SUBJECT_LINK_PLAN,
    LinkPlan,
    LinkPlanStatus,
    PlanFreshness,
    PlanNotFoundError,
    RecommendationPolicy,
    actor_class,
)
from ken_core.config import Settings
from ken_core.db.models.deployment import Deployment, ItemStage
from ken_core.db.models.inventory import KnownUrl, Page, PageVersion
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import deployments as repo
from ken_core.db.repositories import recommendations as rec_repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_services.deployment.errors import (
    DeploymentPermissionError,
    DeploymentStateError,
    OperationReusedDeploymentError,
)
from ken_services.deployment.href_rules import href_problem
from ken_services.deployment.http_probe import FetchOutcome, HttpProbe
from ken_services.deployment.mutation import change_snippets
from ken_services.deployment.protocol import LinkInsertion, LinkVerifier, Mutator
from ken_services.deployment.reader import detail_of
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory import urls
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks, extract_body_links
from ken_services.jobs.runner import utc_now
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.placement import anchor_rule_ok
from ken_services.recommendations.policy import load_recommendation_policy
from ken_services.recommendations.transitions import transition, validate_actor
from ken_services.recommendations.validity import (
    lock_source_plans,
    page_eligibility,
    plan_invalidity,
)

__all__ = [
    "DEPLOY_LOCK_PREFIX",
    "EMITTED_BY",
    "DatabaseDeploymentService",
    "ReconcileSummary",
    "deploy_fingerprint",
    "deploy_lock_key",
    "rollback_fingerprint",
]

logger = get_logger(__name__)

EMITTED_BY: Final = "ken-deployment"
DEPLOY_LOCK_PREFIX: Final = "ken.deploy.source:"
_S = LinkPlanStatus
_D = DeploymentStatus
_C = DeploymentIssueCode
LINK_ON_PAGE: Final = frozenset(
    {_S.DEPLOYED, _S.VERIFIED, _S.FAILED, _S.MEASURED, _S.LEARNING_CAPTURED}
)
"""Plan statuses of a deployment's plans whose link may be on the page (rollback targets)."""
VERIFIABLE: Final = frozenset({_D.APPLIED, _D.VERIFIED, _D.VERIFICATION_FAILED})
ROLLBACKABLE: Final = VERIFIABLE
_OPERATION_UNIQUE: Final = "uq_deployments_operation_id"
_ROLLBACK_OPERATION_UNIQUE: Final = "uq_deployments_rollback_operation_id"
_MAX_CODES: Final = 20


# --------------------------------------------------------------------------- #
# Fingerprints and lock keys
# --------------------------------------------------------------------------- #
def _sha256_json(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def deploy_fingerprint(*, request: DeployRequest, user_id: uuid.UUID) -> str:
    """sha256 of {sorted plan_ids, note, user} (ADR-0014 s8)."""
    return _sha256_json(
        {
            "plan_ids": sorted(str(p) for p in request.plan_ids),
            "note": request.note,
            "user_id": str(user_id),
        }
    )


def rollback_fingerprint(
    *, deployment_id: uuid.UUID, request: RollbackRequest, user_id: uuid.UUID
) -> str:
    return _sha256_json(
        {
            "action": "rollback",
            "deployment_id": str(deployment_id),
            "reason": RollbackReason(request.reason).value,
            "note": request.note,
            "plan_ids": None
            if request.plan_ids is None
            else sorted(str(p) for p in request.plan_ids),
            "user_id": str(user_id),
        }
    )


def deploy_lock_key(source_page_id: uuid.UUID) -> int:
    """``pg_advisory_lock`` key: first 8 bytes of sha256("ken.deploy.source:" + id), signed."""
    digest = hashlib.sha256((DEPLOY_LOCK_PREFIX + str(source_page_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _issue(code: DeploymentIssueCode, plan_id: uuid.UUID | None = None) -> DeploymentIssue:
    return DeploymentIssue(code=code, plan_id=plan_id)


def _rollback_refusal(
    refusal: str, plan_ids: Sequence[uuid.UUID]
) -> DeploymentRefusedError | DeploymentVersionConflictError:
    """The caller-facing error of a refused rollback (also its replay)."""
    if refusal == DeploymentVersionConflictError.code:
        return DeploymentVersionConflictError([])
    if refusal == _C.NOTHING_TO_ROLL_BACK.value:
        return DeploymentRefusedError([_issue(_C.NOTHING_TO_ROLL_BACK)])
    return DeploymentRefusedError(
        [_issue(_C.ROLLBACK_CONFLICT, p) for p in plan_ids] or [_issue(_C.ROLLBACK_CONFLICT)]
    )


def _dedupe(issues: Sequence[DeploymentIssue]) -> list[DeploymentIssue]:
    seen: set[tuple[str, uuid.UUID | None]] = set()
    out: list[DeploymentIssue] = []
    for item in issues:
        key = (DeploymentIssueCode(item.code).value, item.plan_id)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


class _Outcome(StrEnum):
    APPLIED = "applied"
    FAILED = "failed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class _Checked:
    """What validation found (and built) for one request."""

    source_page_id: uuid.UUID | None = None
    source_page_version: int | None = None
    source_url: str | None = None
    plans: list[LinkPlan] = field(default_factory=list)
    cms_page: CMSPage | None = None
    cms_page_id: str | None = None
    changes: list[LinkChange] = field(default_factory=list)
    insertions: list[LinkInsertion] = field(default_factory=list)
    previews: list[ChangePreview] = field(default_factory=list)
    issues: list[DeploymentIssue] = field(default_factory=list)
    stale: dict[uuid.UUID, tuple[str, ...]] = field(default_factory=dict)


@dataclass(slots=True)
class ReconcileSummary:
    examined: int = 0
    applied: int = 0
    failed: int = 0
    conflict: int = 0
    rolled_back: int = 0
    reverted: int = 0
    still_unknown: int = 0
    skipped_locked: int = 0


@dataclass(frozen=True, slots=True)
class _Verified:
    run_id: int
    passed: bool
    inconclusive: bool


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class DatabaseDeploymentService:
    """:class:`ken_contracts.deployment.DeploymentService` over PostgreSQL (module docstring)."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        cms: CMSProvider,
        mutator: Mutator,
        verifier: LinkVerifier,
        probe: HttpProbe | None = None,
        policy: RecommendationPolicy | None = None,
        taxonomy: TaxonomyPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._cms = cms
        self._mutator = mutator
        self._verifier = verifier
        if probe is None and settings.deploy_verify_base_url:
            probe = HttpProbe(
                base_url=settings.deploy_verify_base_url,
                timeout_s=settings.deploy_http_timeout_s,
                user_agent=settings.crawl_user_agent,
            )
        self._probe = probe
        self._policy = policy
        self._taxonomy = taxonomy
        self._clock = clock
        self._sleep = sleep

    # ------------------------------------------------------------ policies
    def _recommendation_policy(self) -> RecommendationPolicy:
        if self._policy is None:
            self._policy = load_recommendation_policy(self._settings.recommendation_policy_path)
        return self._policy

    def _taxonomy_policy(self) -> TaxonomyPolicy:
        if self._taxonomy is None:
            self._taxonomy = load_taxonomy(self._settings.taxonomy_path)
        return self._taxonomy

    def _now(self) -> datetime:
        return self._clock()

    # ----------------------------------------------------------- principal
    @staticmethod
    def _actor(principal: Principal) -> str:
        if not has_permission(principal.role, Permission.DEPLOY):
            raise DeploymentPermissionError
        try:
            actor = validate_actor(principal.actor)
        except ValueError:
            raise DeploymentPermissionError from None
        if actor_class(actor) != "user":
            raise DeploymentPermissionError
        return actor

    # ---------------------------------------------------------------- lock
    def _engine(self) -> AsyncEngine:
        bind = self._sessionmaker.kw.get("bind")
        if not isinstance(bind, AsyncEngine):
            raise TypeError("the sessionmaker must be bound to an AsyncEngine")
        return bind

    @asynccontextmanager
    async def _source_lock(self, source_page_id: uuid.UUID) -> AsyncIterator[None]:
        """The per-source deployment lock (module docstring); busy -> 409 in progress."""
        key = deploy_lock_key(source_page_id)
        connection = await self._engine().connect()
        try:
            acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        if not acquired:
            await connection.close()
            raise DeploymentRefusedError([_issue(_C.DEPLOYMENT_IN_PROGRESS)])
        try:
            yield
        finally:
            try:
                await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await connection.commit()
            except Exception as exc:  # never pool a connection that may hold the lock
                logger.warning("deploy_lock_release_failed", error_class=type(exc).__name__)
                await connection.invalidate()
            finally:
                await connection.close()

    # -------------------------------------------------------------- dry run
    async def dry_run(self, *, request: DryRunRequest, principal: Principal) -> DryRunResult:
        actor = self._actor(principal)
        async with self._sessionmaker() as session:
            checked = await self._validate(
                session, request.plan_ids, actor=actor, operation_id=request.operation_id
            )
            await session.rollback()  # writes nothing
        return DryRunResult(
            source_page_id=checked.source_page_id,
            cms_page_id=checked.cms_page_id,
            cms_version=None if checked.cms_page is None else checked.cms_page.version,
            valid=not checked.issues,
            changes=checked.previews,
            issues=_dedupe(checked.issues),
        )

    # ------------------------------------------------------------ validation
    async def _validate(
        self,
        session: AsyncSession,
        plan_ids: Sequence[uuid.UUID],
        *,
        actor: str,
        operation_id: uuid.UUID,
        lock: bool = False,
        validate_with_cms: bool = True,
    ) -> _Checked:
        """Every deterministic deploy-time check (ADR-0014 s1.3, s3, s8). Issues, not raises
        (except 404 for an unknown plan and 503 for an unreachable CMS)."""
        checked = _Checked()
        rows: dict[uuid.UUID, LinkPlanRow] = {}
        for plan_id in sorted(plan_ids):  # a fixed lock order
            if lock:
                row = await rec_repo.lock_plan(session, plan_id=plan_id)
            else:
                row = await session.get(LinkPlanRow, plan_id)
            if row is None:
                raise PlanNotFoundError
            rows[plan_id] = row
        plans = [rec_repo.plan_from_row(rows[p]) for p in plan_ids]
        checked.plans = plans
        sources = {p.source_page_id for p in plans}
        if len(sources) != 1:
            checked.issues.append(_issue(_C.MIXED_SOURCES))
            return checked
        source_id = next(iter(sources))
        checked.source_page_id = source_id
        for plan in plans:
            if plan.status is not _S.APPROVED:
                checked.issues.append(_issue(_C.PLAN_NOT_APPROVED, plan.id))

        policy = self._recommendation_policy()
        taxonomy = self._taxonomy_policy()
        av = analysis_version(taxonomy.version)
        facts = await rec_repo.page_facts(
            session, analysis_version=av, page_ids=[source_id, *(p.target_page_id for p in plans)]
        )
        source = facts.get(source_id)
        if source is None:
            checked.issues += [_issue(_C.PLAN_INVALID, p.id) for p in plans]
            return checked
        checked.source_page_version = source.current_version
        checked.source_url = source.canonical_url
        body = await rec_repo.version_body(
            session, page_id=source_id, version=source.current_version
        )
        blocks = tuple(extract_blocks(body or ""))
        links = await rec_repo.version_links(
            session, page_id=source_id, version=source.current_version
        )
        linked = {link.target_page_id for link in links if link.target_page_id}
        eligibility = {
            pid: page_eligibility(f, taxonomy=taxonomy, analysis_version=av)
            for pid, f in facts.items()
        }
        hints: dict[uuid.UUID, str] = {}
        for plan in plans:
            check = check_plan_freshness(
                plan_source_version=plan.source_page_version,
                current_version=source.current_version,
                locator=plan.locator,
                anchor_text=plan.anchor_text,
                current_blocks=blocks,
                excluded_ancestors=policy.placement.excluded_ancestors,
            )
            if PlanFreshness(check.freshness) is PlanFreshness.STALE:
                checked.stale[plan.id] = check.reasons
                checked.issues.append(_issue(_C.PLAN_STALE, plan.id))
            hints[plan.id] = check.block_id or plan.locator.block_id
            invalid = plan_invalidity(
                source_eligibility=eligibility.get(source_id),
                target_eligibility=eligibility.get(plan.target_page_id),
                already_linked=plan.target_page_id in linked,
            )
            target_ok = eligibility.get(plan.target_page_id)
            if invalid is not None or target_ok is None or not target_ok.as_target:
                checked.issues.append(_issue(_C.PLAN_INVALID, plan.id))
            kind = "target_topic" if plan.anchor_source == "edited" else plan.anchor_source
            if not plan.anchor_text.strip() or not anchor_rule_ok(
                plan.anchor_text, kind, policy=policy.placement
            ):
                checked.issues.append(_issue(_C.PLAN_INVALID, plan.id))

        # The CMS page: its external_id (CMS-sourced pages), else the provider's URL mapping.
        cms_page_id = await self._cms_page_id(session, source_id, source.canonical_url)
        if cms_page_id is None:
            checked.issues.append(_issue(_C.CMS_PAGE_UNMAPPED))
            return checked
        checked.cms_page_id = cms_page_id
        try:
            page = await self._cms.get_page(page_id=cms_page_id)
        except CMSNotFoundError:
            checked.issues.append(_issue(_C.CMS_PAGE_NOT_FOUND))
            return checked
        except CMSUnavailableError:
            raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)]) from None
        checked.cms_page = page
        if page.protected:
            checked.issues.append(_issue(_C.CMS_PAGE_PROTECTED))

        # Targets: CMS ids, hrefs (the CURRENT canonical URL) and the href rules.
        allowed = self._allowed_hosts(source.canonical_url)
        target_cms: dict[uuid.UUID, str] = {}
        hrefs: dict[uuid.UUID, str] = {}
        for plan in plans:
            target = facts.get(plan.target_page_id)
            if target is None:
                checked.issues.append(_issue(_C.PLAN_INVALID, plan.id))
                continue
            href = target.canonical_url
            hrefs[plan.id] = href
            if href_problem(href, allowed_hosts=allowed) is not None:
                checked.issues.append(_issue(_C.HREF_INVALID, plan.id))
            target_id = await self._cms_page_id(session, target.id, target.canonical_url)
            if target_id is None:
                checked.issues.append(_issue(_C.CMS_PAGE_UNMAPPED, plan.id))
            else:
                target_cms[plan.id] = target_id

        # The CMS's CURRENT html: already linked, link density (with this batch).
        base = page.url or source.canonical_url
        scope = urls.build_scope(source.canonical_url, self._extra_hosts())
        cms_links = extract_body_links(page.body_html, base=base, scope=scope)
        linked_urls = {link.resolved_url for link in cms_links if link.resolved_url}
        already_linked: set[uuid.UUID] = set()
        for plan in plans:
            known = await self._target_urls(session, plan.target_page_id, hrefs.get(plan.id))
            if known & linked_urls:
                already_linked.add(plan.id)
                checked.issues.append(_issue(_C.ALREADY_LINKED, plan.id))
        internal = sum(1 for link in cms_links if link.link_type == "internal")
        words = sum(len(b.text.split()) for b in extract_blocks(page.body_html))
        limit = policy.selection.max_internal_links_per_100_words
        if words == 0 or (internal + len(plans)) * 100.0 / words > limit:
            checked.issues.append(_issue(_C.DENSITY_EXCEEDED))

        # Target reachability over HTTP (only when HTTP verification is configured).
        if self._probe is not None:
            reach: dict[uuid.UUID, bool] = {}
            for plan in plans:
                target_href = hrefs.get(plan.id)
                if target_href is None:
                    continue
                if plan.target_page_id not in reach:
                    fetched = await self._probe.fetch(self._probe.published_url(target_href))
                    reach[plan.target_page_id] = fetched.outcome is FetchOutcome.OK
                if not reach[plan.target_page_id]:
                    checked.issues.append(_issue(_C.BROKEN_TARGET, plan.id))

        # The mutation dry-runs cleanly on the CMS's CURRENT html (all-or-nothing).
        insertions = [
            LinkInsertion(
                plan_id=plan.id,
                href=hrefs.get(plan.id, plan.target_url),
                anchor_text=plan.anchor_text,
                context_hash=plan.locator.context_hash,
                block_id_hint=hints.get(plan.id, plan.locator.block_id),
                occurrence_index=plan.locator.occurrence_index,
                start=plan.locator.start,
                end=plan.locator.end,
            )
            for plan in plans
        ]
        checked.insertions = insertions
        excluded = policy.placement.excluded_ancestors
        # ALREADY_LINKED is decided first, from the CMS HTML: identity is (block, span, href),
        # so an editorial link with our href at the span would otherwise be ADOPTED as
        # already_applied, and a later rollback would unwrap it. Those plans never reach the
        # mutator.
        candidates = [i for i in insertions if i.plan_id not in already_linked]
        # The mutator is synchronous CPU work: off the event loop (identical result).
        mutated = await asyncio.to_thread(
            self._mutator.insert_links, page.body_html, candidates, excluded_ancestors=excluded
        )
        checked.issues += list(mutated.issues)
        checked.issues += [_issue(_C.ALREADY_APPLIED, pid) for pid in mutated.already_applied]
        if mutated.ok and not mutated.already_applied:
            checked.previews = await asyncio.to_thread(
                self._previews, page.body_html, plans, insertions, excluded
            )
        checked.changes = [
            LinkChange(
                change_id=plan.id,
                operation=LinkOperation.INSERT,
                target_page_id=target_cms.get(plan.id, str(plan.target_page_id)),
                href=hrefs.get(plan.id, plan.target_url),
                anchor_text=plan.anchor_text,
                locator=LinkLocator(
                    block_selector=hints.get(plan.id, plan.locator.block_id),
                    occurrence_index=plan.locator.occurrence_index,
                    context_hash=plan.locator.context_hash,
                ),
            )
            for plan in plans
        ]

        # The CMS's own validation (a dry run there too): only when everything else passed.
        if validate_with_cms and not checked.issues:
            probe_request = self._request(checked, operation_id=operation_id, actor=actor, dry=True)
            try:
                result = await self._cms.validate_link_changes(request=probe_request)
            except CMSUnavailableError:
                raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)]) from None
            except CMSNotFoundError:
                checked.issues.append(_issue(_C.CMS_PAGE_NOT_FOUND))
                return checked
            if not result.valid:
                checked.issues += self._cms_issues(result.issues, plans)
        return checked

    def _request(
        self, checked: _Checked, *, operation_id: uuid.UUID, actor: str, dry: bool
    ) -> LinkChangeRequest:
        if checked.cms_page is None or checked.cms_page_id is None:  # pragma: no cover
            raise DeploymentRefusedError([_issue(_C.CMS_PAGE_NOT_FOUND)])
        return LinkChangeRequest(
            operation_id=operation_id,
            source_page_id=checked.cms_page_id,
            expected_version=checked.cms_page.version,
            changes=checked.changes,
            dry_run=dry,
            actor=actor,
        )

    @staticmethod
    def _cms_issues(
        issues: Sequence[ValidationIssue], plans: Sequence[LinkPlan]
    ) -> list[DeploymentIssue]:
        """A CMS refusal as typed issues (``cms_validation_failed`` per change, or overall)."""
        ids = {p.id for p in plans}
        out = [
            _issue(_C.CMS_VALIDATION_FAILED, i.change_id if i.change_id in ids else None)
            for i in issues
            if i.severity == "error"
        ]
        return out or [_issue(_C.CMS_VALIDATION_FAILED)]

    def _extra_hosts(self) -> list[str]:
        raw = f"{self._settings.crawl_allowed_hosts},{self._settings.deploy_allowed_hosts}"
        return [h.strip() for h in raw.split(",") if h.strip()]

    def _allowed_hosts(self, source_url: str) -> set[str]:
        """The source page's host plus the configured extra hosts (hostnames, ADR-0014 s3)."""
        hosts = {h.split(":", 1)[0].lower() for h in self._extra_hosts()}
        host = urlsplit(source_url).hostname
        if host:
            hosts.add(host.lower())
        return hosts

    async def _cms_page_id(
        self, session: AsyncSession, page_id: uuid.UUID, canonical_url: str
    ) -> str | None:
        """ADR-0014 s8 resolution: external_id (CMS-sourced) -> provider URL mapping -> None."""
        row = (
            await session.execute(
                select(Page.external_id, PageVersion.source_type)
                .join(
                    PageVersion,
                    (PageVersion.page_id == Page.id)
                    & (PageVersion.version == Page.current_version),
                )
                .where(Page.id == page_id)
            )
        ).first()
        if row is not None and row.external_id and row.source_type == "cms":
            return str(row.external_id)
        resolver = getattr(self._cms, "resolve_page_id", None)
        if resolver is None:
            return None
        try:
            found = await resolver(url=canonical_url)
        except CMSUnavailableError:
            raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)]) from None
        return None if found is None else str(found)

    @staticmethod
    async def _target_urls(
        session: AsyncSession, target_page_id: uuid.UUID, href: str | None
    ) -> set[str]:
        """Every URL that has led to the target (redirects and aliases count)."""
        rows = await session.scalars(select(KnownUrl.url).where(KnownUrl.page_id == target_page_id))
        found = set(rows.all())
        if href:
            normalized = urls.try_normalize_url(href)
            if normalized:
                found.add(normalized)
        return found

    def _previews(
        self,
        html: str,
        plans: Sequence[LinkPlan],
        insertions: Sequence[LinkInsertion],
        excluded: Sequence[str],
    ) -> list[ChangePreview]:
        """Per plan: the block text with the anchor span, and an HTML snippet before/after
        (TEXT for display; the UI never renders it)."""
        out: list[ChangePreview] = []
        blocks = {sha256_hex(b.text): b for b in extract_blocks(html)}
        for plan, insertion in zip(plans, insertions, strict=True):
            single = self._mutator.insert_links(html, [insertion], excluded_ancestors=excluded)
            if not single.ok:
                continue
            before_snippet, after_snippet = change_snippets(html, single.html)
            block = blocks.get(plan.locator.context_hash)
            out.append(
                ChangePreview(
                    plan_id=plan.id,
                    anchor_text=plan.anchor_text,
                    href=insertion.href,
                    block_text=block.text if block is not None else "",
                    anchor_start=plan.locator.start,
                    anchor_end=plan.locator.end,
                    html_before=before_snippet,
                    html_after=after_snippet,
                )
            )
        return out

    # --------------------------------------------------------------- deploy
    async def deploy(self, *, request: DeployRequest, principal: Principal) -> DeploymentDetail:
        actor = self._actor(principal)
        fingerprint = deploy_fingerprint(request=request, user_id=principal.user_id)
        stored = await self._stored(request.operation_id)
        if stored is not None:
            return await self._replay(stored, fingerprint)
        await self._refuse_foreign_operation(request.operation_id)
        source_id = await self._source_of(request.plan_ids)
        async with self._source_lock(source_id):
            stored = await self._stored(request.operation_id)  # a concurrent duplicate won
            if stored is not None:
                return await self._replay_locked(stored, fingerprint)
            deployment_id = await self._intent(
                request, principal=principal, actor=actor, fingerprint=fingerprint
            )
            outcome = await self._send(deployment_id)
            return await self._finish(deployment_id, outcome, replay=False)

    async def _finish(
        self, deployment_id: uuid.UUID, outcome: _Outcome, *, replay: bool
    ) -> DeploymentDetail:
        """The caller-facing result of an outcome (the lock is held)."""
        if outcome is _Outcome.APPLIED:
            await self._verify_with_retry(deployment_id)
            return await self._detail(deployment_id, replay=replay)
        if outcome is _Outcome.UNKNOWN:
            raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)])
        # failed / conflict: a durable deployment row exists (status, items, error code), so
        # the caller gets its DETAIL (200), exactly as a replay of the operation does.
        return await self._detail(deployment_id, replay=replay)

    async def _stored(self, operation_id: uuid.UUID) -> Deployment | None:
        async with self._sessionmaker() as session:
            return await repo.get_by_operation(session, operation_id=operation_id)

    async def _row(self, deployment_id: uuid.UUID) -> Deployment:
        async with self._sessionmaker() as session:
            row = await repo.get_deployment(session, deployment_id=deployment_id)
        if row is None:
            raise DeploymentNotFoundError
        return row

    async def _detail(self, deployment_id: uuid.UUID, *, replay: bool = False) -> DeploymentDetail:
        async with self._sessionmaker() as session:
            row = await repo.get_deployment(session, deployment_id=deployment_id)
            if row is None:
                raise DeploymentNotFoundError
            return await detail_of(session, row, replay=replay)

    async def _refuse_foreign_operation(self, operation_id: uuid.UUID) -> None:
        """An id already used as a ROLLBACK operation is never a deploy id (409)."""
        async with self._sessionmaker() as session:
            used = await repo.get_by_rollback_operation(session, operation_id=operation_id)
            items = await repo.items_by_operation(session, operation_id=operation_id)
        if used is not None or items:
            raise OperationReusedDeploymentError([])

    async def _source_of(self, plan_ids: Sequence[uuid.UUID]) -> uuid.UUID:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(LinkPlanRow.id, LinkPlanRow.source_page_id).where(
                        LinkPlanRow.id.in_(list(plan_ids))
                    )
                )
            ).all()
        if len(rows) != len(set(plan_ids)):
            raise PlanNotFoundError
        sources: set[uuid.UUID] = {r.source_page_id for r in rows}
        if len(sources) != 1:
            raise DeploymentRefusedError([_issue(_C.MIXED_SOURCES)])
        return next(iter(sources))

    async def _replay(self, stored: Deployment, fingerprint: str) -> DeploymentDetail:
        if stored.request_fingerprint != fingerprint:
            raise OperationReusedDeploymentError([])
        if DeploymentStatus(stored.status) is _D.APPLYING:
            async with self._source_lock(stored.source_page_id):
                return await self._replay_locked(stored, fingerprint)
        return await self._detail(stored.id, replay=True)

    async def _replay_locked(self, stored: Deployment, fingerprint: str) -> DeploymentDetail:
        if stored.request_fingerprint != fingerprint:
            raise OperationReusedDeploymentError([])
        row = await self._row(stored.id)
        if DeploymentStatus(row.status) is _D.APPLYING:
            # The outcome is still unknown: resend the STORED request (a client retry is a
            # reconciliation). Every provider replays an operation it already applied.
            outcome = await self._send(row.id)
            return await self._finish(row.id, outcome, replay=True)
        return await self._detail(row.id, replay=True)

    async def _intent(
        self, request: DeployRequest, *, principal: Principal, actor: str, fingerprint: str
    ) -> uuid.UUID:
        """tx1 (module docstring). Raises the refusal after committing stale-plan expiry."""
        refusal: DeploymentRefusedError | None = None
        deployment_id = uuid.uuid4()
        try:
            async with self._sessionmaker() as session, session.begin():
                source_id = await session.scalar(
                    select(LinkPlanRow.source_page_id).where(LinkPlanRow.id == request.plan_ids[0])
                )
                if source_id is None:
                    raise PlanNotFoundError
                await lock_source_plans(session, source_page_id=source_id)
                checked = await self._validate(
                    session,
                    request.plan_ids,
                    actor=actor,
                    operation_id=request.operation_id,
                    lock=True,
                )
                if checked.issues:
                    refusal = DeploymentRefusedError(_dedupe(checked.issues))
                    await self._expire_stale(session, checked, actor=actor)
                else:
                    await self._record_intent(
                        session,
                        checked,
                        deployment_id=deployment_id,
                        request=request,
                        principal=principal,
                        actor=actor,
                        fingerprint=fingerprint,
                    )
        except IntegrityError as exc:
            if _OPERATION_UNIQUE in str(exc.orig):
                stored = await self._stored(request.operation_id)
                if stored is not None:
                    raise OperationReusedDeploymentError([]) from None
            raise
        if refusal is not None:
            raise refusal
        return deployment_id

    async def _expire_stale(self, session: AsyncSession, checked: _Checked, *, actor: str) -> None:
        for plan in checked.plans:
            reasons = checked.stale.get(plan.id)
            if reasons is None or plan.status is not _S.APPROVED:
                continue
            await transition(
                session,
                plan_id=plan.id,
                to_status=_S.EXPIRED,
                actor=DEPLOYMENT_ACTOR,
                note=f"stale at deploy ({', '.join(reasons)}); requested by {actor}"[:500],
                expected_from=_S.APPROVED,
                clock=self._clock,
            )

    async def _record_intent(
        self,
        session: AsyncSession,
        checked: _Checked,
        *,
        deployment_id: uuid.UUID,
        request: DeployRequest,
        principal: Principal,
        actor: str,
        fingerprint: str,
    ) -> None:
        now = self._now()
        for plan in checked.plans:
            await transition(
                session,
                plan_id=plan.id,
                to_status=_S.VALIDATED,
                actor=DEPLOYMENT_ACTOR,
                note=self._note("deploy", request.operation_id, actor),
                expected_from=_S.APPROVED,
                clock=self._clock,
            )
        cms_request = self._request(
            checked, operation_id=request.operation_id, actor=actor, dry=False
        )
        if (
            checked.source_page_id is None
            or checked.source_page_version is None
            or checked.cms_page_id is None
            or checked.cms_page is None
        ):  # pragma: no cover - validation passed, so all are set
            raise DeploymentRefusedError([_issue(_C.CMS_PAGE_NOT_FOUND)])
        await repo.insert_deployment(
            session,
            id=deployment_id,
            operation_id=request.operation_id,
            request_fingerprint=fingerprint,
            request_json=cms_request.model_dump_json(),
            cms_provider=self._cms.name,
            cms_page_id=checked.cms_page_id,
            source_page_id=checked.source_page_id,
            source_page_version=checked.source_page_version,
            status=_D.APPLYING.value,
            actor=actor,
            user_id=principal.user_id,
            note=request.note,
            preview=[p.model_dump(mode="json") for p in checked.previews],
            previous_version=checked.cms_page.version,
            attempts=1,
            created_at=now,
            updated_at=now,
        )
        await repo.insert_items(
            session,
            deployment_id=deployment_id,
            items=[
                repo.NewItem(
                    plan_id=plan.id,
                    stage=ItemStage.REQUESTED,
                    href=change.href,
                    anchor_text=plan.anchor_text,
                    target_page_id=plan.target_page_id,
                )
                for plan, change in zip(checked.plans, checked.changes, strict=True)
            ],
            recorded_at=now,
        )

    @staticmethod
    def _note(kind: str, operation_id: uuid.UUID, actor: str) -> str:
        return f"{kind} op={operation_id} by {actor}"[:500]

    # ------------------------------------------------------- CMS call + tx2
    async def _send(self, deployment_id: uuid.UUID) -> _Outcome:
        """Resend the STORED request (byte for byte) and record the outcome (tx2)."""
        row = await self._row(deployment_id)
        request = LinkChangeRequest.model_validate_json(row.request_json)
        try:
            result = await self._cms.apply_link_changes(request=request)
        except CMSVersionConflictError:
            await self._record_conflict(deployment_id)
            return _Outcome.CONFLICT
        except CMSValidationError as exc:
            await self._record_failed(deployment_id, exc.issues)
            return _Outcome.FAILED
        except CMSNotFoundError:
            await self._record_failed(
                deployment_id,
                [ValidationIssue(code=_C.CMS_PAGE_NOT_FOUND.value, severity="error", message="")],
            )
            return _Outcome.FAILED
        except CMSUnavailableError:
            await self._touch(deployment_id)
            return _Outcome.UNKNOWN
        except Exception as exc:  # an unknown outcome is never a failure: stay applying
            logger.error("deploy_cms_call_failed", error_class=type(exc).__name__)
            await self._touch(deployment_id)
            return _Outcome.UNKNOWN
        await self._record_applied(deployment_id, result)
        return _Outcome.APPLIED

    async def _touch(self, deployment_id: uuid.UUID) -> None:
        """An attempt with an unknown outcome: count it, keep the status."""
        try:
            async with self._sessionmaker() as session, session.begin():
                row = await repo.lock_deployment(session, deployment_id=deployment_id)
                if row is None or DeploymentStatus(row.status) not in {
                    _D.APPLYING,
                    _D.ROLLING_BACK,
                }:
                    return
                await repo.update_deployment(
                    session,
                    deployment_id=deployment_id,
                    attempts=row.attempts + 1,
                    updated_at=max(self._now(), row.updated_at),
                )
        except Exception as exc:  # best effort: the row is still applying either way
            logger.warning("deploy_touch_failed", error_class=type(exc).__name__)

    async def _plan_rows(
        self, session: AsyncSession, deployment_id: uuid.UUID
    ) -> list[LinkPlanRow]:
        ids = await repo.plan_ids_of(session, deployment_id=deployment_id)
        rows = []
        for plan_id in sorted(ids):
            row = await rec_repo.lock_plan(session, plan_id=plan_id)
            if row is not None:
                rows.append(row)
        order = {pid: i for i, pid in enumerate(ids)}
        return sorted(rows, key=lambda r: order[r.id])

    async def _lock_for_outcome(
        self, session: AsyncSession, deployment_id: uuid.UUID, expected: DeploymentStatus
    ) -> Deployment | None:
        row = await repo.lock_deployment(session, deployment_id=deployment_id)
        if row is None or DeploymentStatus(row.status) is not expected:
            return None  # already recorded (a concurrent reconciliation)
        await lock_source_plans(session, source_page_id=row.source_page_id)
        return row

    async def _record_applied(self, deployment_id: uuid.UUID, result: DeploymentResult) -> None:
        async with self._sessionmaker() as session, session.begin():
            row = await self._lock_for_outcome(session, deployment_id, _D.APPLYING)
            if row is None:
                return
            now = max(self._now(), row.updated_at)
            plans = await self._plan_rows(session, deployment_id)
            items = {
                i.plan_id: i for i in await repo.items_of(session, deployment_id=deployment_id)
            }
            new_items = []
            for plan_row in plans:
                plan = rec_repo.plan_from_row(plan_row)
                if plan.status is _S.VALIDATED:
                    await transition(
                        session,
                        plan_id=plan.id,
                        to_status=_S.DEPLOYED,
                        actor=DEPLOYMENT_ACTOR,
                        note=self._note("deployed", row.operation_id, row.actor),
                        expected_from=_S.VALIDATED,
                        clock=self._clock,
                    )
                    await self._emit(
                        session,
                        EventName.LINK_DEPLOYED,
                        plan.id,
                        LinkDeployedPayload(
                            plan_id=plan.id,
                            deployment_id=row.id,
                            operation_id=row.operation_id,
                            source_page_id=plan.source_page_id,
                            target_page_id=plan.target_page_id,
                            cms_provider=row.cms_provider,
                            cms_page_id=row.cms_page_id,
                            previous_version=result.previous_version,
                            new_version=result.new_version,
                            anchor_text=plan.anchor_text,
                            href=items[plan.id].href if plan.id in items else plan.target_url,
                            engine_version=plan.engine_version,
                            policy_version=plan.policy_version,
                            ranking_policy_version=plan.ranking_policy_version,
                            actor=row.actor,
                        ),
                        row.operation_id,
                        now,
                    )
                stage = (
                    ItemStage.ALREADY_APPLIED
                    if result.status == "no_op" or plan.id not in result.applied_change_ids
                    else ItemStage.APPLIED
                )
                new_items.append(self._item(items.get(plan.id), plan, stage))
            await repo.insert_items(
                session, deployment_id=deployment_id, items=new_items, recorded_at=now
            )
            await repo.update_deployment(
                session,
                deployment_id=deployment_id,
                status=_D.APPLIED.value,
                cms_deployment_id=result.deployment_id,
                previous_version=result.previous_version,
                new_version=result.new_version,
                applied_at=now,
                updated_at=now,
                error_code=None,
            )

    async def _record_failed(
        self, deployment_id: uuid.UUID, issues: Sequence[ValidationIssue]
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            row = await self._lock_for_outcome(session, deployment_id, _D.APPLYING)
            if row is None:
                return
            now = max(self._now(), row.updated_at)
            codes = sorted({i.code for i in issues if i.code})[:_MAX_CODES] or [
                _C.CMS_VALIDATION_FAILED.value
            ]
            safe_codes = [c for c in codes if c.replace("_", "").isalnum()][:_MAX_CODES]
            items = {
                i.plan_id: i for i in await repo.items_of(session, deployment_id=deployment_id)
            }
            new_items = []
            for plan_row in await self._plan_rows(session, deployment_id):
                plan = rec_repo.plan_from_row(plan_row)
                if plan.status is _S.VALIDATED:
                    await transition(
                        session,
                        plan_id=plan.id,
                        to_status=_S.FAILED,
                        actor=DEPLOYMENT_ACTOR,
                        note=self._note("cms refused", row.operation_id, row.actor),
                        expected_from=_S.VALIDATED,
                        clock=self._clock,
                    )
                    await self._emit(
                        session,
                        EventName.LINK_FAILED,
                        plan.id,
                        LinkFailedPayload(
                            plan_id=plan.id, deployment_id=row.id, stage="apply", codes=safe_codes
                        ),
                        row.operation_id,
                        now,
                    )
                new_items.append(self._item(items.get(plan.id), plan, ItemStage.FAILED, safe_codes))
            await repo.insert_items(
                session, deployment_id=deployment_id, items=new_items, recorded_at=now
            )
            await repo.update_deployment(
                session,
                deployment_id=deployment_id,
                status=_D.FAILED.value,
                error_code=_C.CMS_VALIDATION_FAILED.value,
                updated_at=now,
            )

    async def _record_conflict(self, deployment_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session, session.begin():
            row = await self._lock_for_outcome(session, deployment_id, _D.APPLYING)
            if row is None:
                return
            now = max(self._now(), row.updated_at)
            items = {
                i.plan_id: i for i in await repo.items_of(session, deployment_id=deployment_id)
            }
            new_items = []
            for plan_row in await self._plan_rows(session, deployment_id):
                plan = rec_repo.plan_from_row(plan_row)
                if plan.status is _S.VALIDATED:
                    await transition(
                        session,
                        plan_id=plan.id,
                        to_status=_S.EXPIRED,
                        actor=DEPLOYMENT_ACTOR,
                        note=self._note("cms version conflict", row.operation_id, row.actor),
                        expected_from=_S.VALIDATED,
                        clock=self._clock,
                    )
                new_items.append(self._item(items.get(plan.id), plan, ItemStage.CONFLICT))
            await repo.insert_items(
                session, deployment_id=deployment_id, items=new_items, recorded_at=now
            )
            await repo.update_deployment(
                session,
                deployment_id=deployment_id,
                status=_D.CONFLICT.value,
                error_code="cms_version_conflict",
                updated_at=now,
            )

    @staticmethod
    def _item(
        requested: Any,
        plan: LinkPlan,
        stage: str,
        codes: Sequence[str] = (),
        *,
        operation_id: uuid.UUID | None = None,
        fingerprint: str | None = None,
    ) -> repo.NewItem:
        return repo.NewItem(
            plan_id=plan.id,
            stage=stage,
            href=requested.href if requested is not None else plan.target_url,
            anchor_text=plan.anchor_text,
            target_page_id=plan.target_page_id,
            codes=list(codes),
            operation_id=operation_id,
            fingerprint=fingerprint,
        )

    async def _emit(
        self,
        session: AsyncSession,
        name: EventName,
        plan_id: uuid.UUID,
        payload: Any,
        correlation_id: uuid.UUID,
        now: datetime,
    ) -> None:
        await append_event(
            session,
            EventEnvelope(
                event_name=name,
                event_version=1,
                occurred_at=now,
                emitted_by=EMITTED_BY,
                subject_type=SUBJECT_LINK_PLAN,
                subject_id=str(plan_id),
                payload=payload.model_dump(mode="json"),
                correlation_id=correlation_id,
            ),
        )

    # --------------------------------------------------------- verification
    async def verify(self, *, deployment_id: uuid.UUID, principal: Principal) -> DeploymentDetail:
        self._actor(principal)
        row = await self._row(deployment_id)
        status = DeploymentStatus(row.status)
        if status in {_D.APPLYING, _D.ROLLING_BACK}:
            raise DeploymentRefusedError([_issue(_C.DEPLOYMENT_IN_PROGRESS)])
        if status not in VERIFIABLE:
            raise DeploymentStateError([])
        async with self._source_lock(row.source_page_id):
            await self._verify_once(deployment_id)
        return await self._detail(deployment_id)

    async def _verify_with_retry(self, deployment_id: uuid.UUID) -> None:
        attempts = self._settings.deploy_verify_attempts
        for attempt in range(attempts):
            verified = await self._verify_once(deployment_id)
            if verified is None or not verified.inconclusive:
                return
            if attempt + 1 < attempts:
                await self._sleep(self._settings.deploy_verify_retry_delay_s)

    async def _targets(
        self, session: AsyncSession, deployment_id: uuid.UUID
    ) -> list[tuple[LinkPlan, LinkInsertion]]:
        """(plan, its link) for every plan of the deployment whose link may be on the page."""
        items = {
            i.plan_id: i
            for i in await repo.items_of(session, deployment_id=deployment_id)
            if i.stage == ItemStage.REQUESTED
        }
        out = []
        for plan_id in await repo.plan_ids_of(session, deployment_id=deployment_id):
            row = await session.get(LinkPlanRow, plan_id, populate_existing=True)
            if row is None:
                continue
            plan = rec_repo.plan_from_row(row)
            if plan.status not in LINK_ON_PAGE:
                continue
            item = items[plan_id]
            out.append(
                (
                    plan,
                    LinkInsertion(
                        plan_id=plan.id,
                        href=item.href,
                        anchor_text=plan.anchor_text,
                        context_hash=plan.locator.context_hash,
                        block_id_hint=plan.locator.block_id,
                        occurrence_index=plan.locator.occurrence_index,
                        start=plan.locator.start,
                        end=plan.locator.end,
                    ),
                )
            )
        return out

    async def _checks(
        self, row: Deployment, targets: Sequence[tuple[LinkPlan, LinkInsertion]]
    ) -> list[VerificationCheck]:
        """CMS-side checks; with HTTP verification, the published page and targets too.
        Deterministic CMS/HTML failure -> failed; transient or publish lag -> inconclusive."""
        insertions = [ins for _plan, ins in targets]
        try:
            page = await self._cms.get_page(page_id=row.cms_page_id)
        except CMSUnavailableError:
            return [
                VerificationCheck(
                    plan_id=i.plan_id,
                    check=VerificationCheckName.LINK_PRESENT_ONCE,
                    outcome=VerificationOutcome.INCONCLUSIVE,
                    detail="cms_unavailable",
                )
                for i in insertions
            ]
        except CMSNotFoundError:
            return [
                VerificationCheck(
                    plan_id=i.plan_id,
                    check=VerificationCheckName.LINK_PRESENT_ONCE,
                    outcome=VerificationOutcome.FAILED,
                    detail="cms_page_not_found",
                )
                for i in insertions
            ]
        transient: set[uuid.UUID] = set()
        reach: dict[uuid.UUID, bool | None] = {}
        published = None
        if self._probe is not None:
            cache: dict[str, FetchOutcome] = {}
            for insertion in insertions:
                url = self._probe.published_url(insertion.href)
                if url not in cache:
                    cache[url] = (await self._probe.fetch(url)).outcome
                fetched_outcome = cache[url]
                if fetched_outcome is FetchOutcome.INCONCLUSIVE:
                    reach[insertion.plan_id] = None
                    transient.add(insertion.plan_id)
                else:
                    reach[insertion.plan_id] = fetched_outcome is FetchOutcome.OK
            source_url = await self._source_url(row.source_page_id)
            published = await self._probe.fetch(self._probe.published_url(source_url))
        # The verifier is synchronous CPU work: off the event loop (identical result).
        checks = await asyncio.to_thread(
            self._verifier.verify_links, page.body_html, insertions, target_reachable=reach
        )
        merged: list[VerificationCheck] = []
        public: dict[tuple[uuid.UUID, str], VerificationOutcome] = {}
        if published is not None and published.outcome is FetchOutcome.OK and published.body:
            for c in await asyncio.to_thread(
                self._verifier.verify_links, published.body, insertions, target_reachable=reach
            ):
                public[(c.plan_id, VerificationCheckName(c.check).value)] = VerificationOutcome(
                    c.outcome
                )
        for check in checks:
            name = VerificationCheckName(check.check)
            outcome = VerificationOutcome(check.outcome)
            detail = check.detail
            if name is VerificationCheckName.TARGET_REACHABLE and check.plan_id in transient:
                outcome, detail = VerificationOutcome.INCONCLUSIVE, "target_unreachable"
            elif outcome is VerificationOutcome.PASSED and published is not None:
                if published.outcome is not FetchOutcome.OK:
                    if name is VerificationCheckName.LINK_PRESENT_ONCE:
                        outcome, detail = (
                            VerificationOutcome.INCONCLUSIVE,
                            "published_page_unavailable",
                        )
                elif public.get((check.plan_id, name.value)) is VerificationOutcome.FAILED:
                    outcome, detail = VerificationOutcome.INCONCLUSIVE, "publish_lag"
            merged.append(
                VerificationCheck(plan_id=check.plan_id, check=name, outcome=outcome, detail=detail)
            )
        return merged

    async def _source_url(self, page_id: uuid.UUID) -> str:
        async with self._sessionmaker() as session:
            return await repo.source_url_of(session, page_id=page_id)

    async def _verify_once(self, deployment_id: uuid.UUID) -> _Verified | None:
        """One verification run, recorded with its plan and deployment transitions."""
        async with self._sessionmaker() as session:
            row = await repo.get_deployment(session, deployment_id=deployment_id)
            if row is None or DeploymentStatus(row.status) not in VERIFIABLE:
                return None
            targets = await self._targets(session, deployment_id)
        if not targets:
            return None
        checks = await self._checks(row, targets)
        by_plan: dict[uuid.UUID, list[VerificationCheck]] = {}
        for check in checks:
            by_plan.setdefault(check.plan_id, []).append(check)
        failed_plans = {
            pid
            for pid, cs in by_plan.items()
            if any(VerificationOutcome(c.outcome) is VerificationOutcome.FAILED for c in cs)
        }
        unsure_plans = {
            pid
            for pid, cs in by_plan.items()
            if pid not in failed_plans
            and any(VerificationOutcome(c.outcome) is VerificationOutcome.INCONCLUSIVE for c in cs)
        }
        passed = not failed_plans and not unsure_plans
        inconclusive = not failed_plans and bool(unsure_plans)
        return await self._record_verification(
            deployment_id,
            checks=checks,
            failed_plans=failed_plans,
            unsure_plans=unsure_plans,
            passed=passed,
            inconclusive=inconclusive,
        )

    async def _record_verification(
        self,
        deployment_id: uuid.UUID,
        *,
        checks: Sequence[VerificationCheck],
        failed_plans: set[uuid.UUID],
        unsure_plans: set[uuid.UUID],
        passed: bool,
        inconclusive: bool,
    ) -> _Verified | None:
        async with self._sessionmaker() as session, session.begin():
            row = await repo.lock_deployment(session, deployment_id=deployment_id)
            if row is None or DeploymentStatus(row.status) not in VERIFIABLE:
                return None
            await lock_source_plans(session, source_page_id=row.source_page_id)
            now = max(self._now(), row.updated_at)
            run_id = await repo.insert_verification(
                session,
                deployment_id=deployment_id,
                run_at=now,
                passed=passed,
                inconclusive=inconclusive,
                http_checked=self._probe is not None,
                checks=[c.model_dump(mode="json") for c in checks],
            )
            items = {
                i.plan_id: i for i in await repo.items_of(session, deployment_id=deployment_id)
            }
            new_items = []
            checked_ids = {c.plan_id for c in checks}
            for plan_row in await self._plan_rows(session, deployment_id):
                plan = rec_repo.plan_from_row(plan_row)
                if plan.id not in checked_ids or plan.status is not _S.DEPLOYED:
                    continue  # verified / failed plans: a run is recorded, no plan transition
                if plan.id in failed_plans:
                    codes = sorted(
                        {
                            VerificationCheckName(c.check).value
                            for c in checks
                            if c.plan_id == plan.id
                            and VerificationOutcome(c.outcome) is VerificationOutcome.FAILED
                        }
                    )
                    await self._move(session, plan.id, _S.FAILED, row, "verification failed")
                    await self._emit(
                        session,
                        EventName.LINK_FAILED,
                        plan.id,
                        LinkFailedPayload(
                            plan_id=plan.id, deployment_id=row.id, stage="verify", codes=codes
                        ),
                        row.operation_id,
                        now,
                    )
                    new_items.append(
                        self._item(items.get(plan.id), plan, ItemStage.VERIFICATION_FAILED, codes)
                    )
                elif plan.id not in unsure_plans:
                    await self._move(session, plan.id, _S.VERIFIED, row, "verified")
                    await self._emit(
                        session,
                        EventName.LINK_VERIFIED,
                        plan.id,
                        LinkVerifiedPayload(
                            plan_id=plan.id,
                            deployment_id=row.id,
                            verification_run_id=run_id,
                            http_checked=self._probe is not None,
                        ),
                        row.operation_id,
                        now,
                    )
                    new_items.append(self._item(items.get(plan.id), plan, ItemStage.VERIFIED))
            if new_items:
                await repo.insert_items(
                    session, deployment_id=deployment_id, items=new_items, recorded_at=now
                )
            status = DeploymentStatus(row.status)
            target = _D.VERIFICATION_FAILED if failed_plans else _D.VERIFIED if passed else status
            values: dict[str, Any] = {"updated_at": now}
            if target is not status:
                values["status"] = target.value
            if target is _D.VERIFIED:
                values["verified_at"] = now
            await repo.update_deployment(session, deployment_id=deployment_id, **values)
        return _Verified(run_id=run_id, passed=passed, inconclusive=inconclusive)

    async def _move(
        self,
        session: AsyncSession,
        plan_id: uuid.UUID,
        to_status: LinkPlanStatus,
        row: Deployment,
        what: str,
        *,
        actor: str | None = None,
        operation_id: uuid.UUID | None = None,
    ) -> None:
        await transition(
            session,
            plan_id=plan_id,
            to_status=to_status,
            actor=DEPLOYMENT_ACTOR,
            note=self._note(what, operation_id or row.operation_id, actor or row.actor),
            clock=self._clock,
        )

    # ------------------------------------------------------------- rollback
    async def rollback(
        self, *, deployment_id: uuid.UUID, request: RollbackRequest, principal: Principal
    ) -> DeploymentDetail:
        actor = self._actor(principal)
        fingerprint = rollback_fingerprint(
            deployment_id=deployment_id, request=request, user_id=principal.user_id
        )
        row = await self._row(deployment_id)
        replay = await self._rollback_replay(row, request.operation_id, fingerprint)
        if replay is not None:
            return replay
        async with self._source_lock(row.source_page_id):
            row = await self._row(deployment_id)
            again = await self._rollback_replay(row, request.operation_id, fingerprint, locked=True)
            if again is not None:
                return again
            status = DeploymentStatus(row.status)
            if status in {_D.FAILED, _D.CONFLICT}:
                raise DeploymentRefusedError([_issue(_C.NOTHING_TO_ROLL_BACK)])
            if status is _D.ROLLED_BACK:
                return await self._detail(deployment_id)  # idempotent: nothing changes
            if status not in ROLLBACKABLE:
                raise DeploymentRefusedError([_issue(_C.DEPLOYMENT_IN_PROGRESS)])
            targets = await self._rollback_targets(row, request.plan_ids)
            if not targets:
                return await self._detail(deployment_id)
            per_plan = request.plan_ids is not None
            remove_request = (
                await self._remove_request(row, targets, request.operation_id, actor)
                if per_plan
                else None
            )
            await self._rollback_intent(
                row,
                request=request,
                principal=principal,
                actor=actor,
                fingerprint=fingerprint,
                targets=targets if per_plan else None,
                remove_request=remove_request,
            )
            await self._send_rollback(deployment_id, raise_errors=True)
        return await self._detail(deployment_id)

    async def _rollback_replay(
        self, row: Deployment, operation_id: uuid.UUID, fingerprint: str, *, locked: bool = False
    ) -> DeploymentDetail | None:
        """A stored rollback operation replays; an id used for anything else is a 409."""
        async with self._sessionmaker() as session:
            items = await repo.items_by_operation(session, operation_id=operation_id)
            owner = await repo.get_by_rollback_operation(session, operation_id=operation_id)
            deploy = await repo.get_by_operation(session, operation_id=operation_id)
        if deploy is not None:
            raise OperationReusedDeploymentError([])
        if items:
            if any(i.fingerprint != fingerprint for i in items) or items[0].deployment_id != row.id:
                raise OperationReusedDeploymentError([])
            refused = [i for i in items if i.stage == ItemStage.ROLLBACK_CONFLICT]
            if refused:  # the operation was refused: replay the SAME refusal
                raise _rollback_refusal(
                    refused[0].codes[0] if refused[0].codes else _C.ROLLBACK_CONFLICT.value,
                    [i.plan_id for i in refused],
                )
            return await self._detail(row.id, replay=True)
        if owner is None:
            return None
        if owner.id != row.id or owner.rollback_fingerprint != fingerprint:
            raise OperationReusedDeploymentError([])
        if DeploymentStatus(owner.status) is _D.ROLLING_BACK:
            if not locked:
                async with self._source_lock(row.source_page_id):
                    await self._send_rollback(row.id, raise_errors=True)
            else:
                await self._send_rollback(row.id, raise_errors=True)
        return await self._detail(row.id, replay=True)

    async def _rollback_targets(
        self, row: Deployment, plan_ids: Sequence[uuid.UUID] | None
    ) -> list[LinkPlan]:
        async with self._sessionmaker() as session:
            ids = await repo.plan_ids_of(session, deployment_id=row.id)
            plans = []
            for plan_id in ids:
                plan_row = await session.get(LinkPlanRow, plan_id)
                if plan_row is not None:
                    plans.append(rec_repo.plan_from_row(plan_row))
        if plan_ids is not None:
            unknown = [p for p in plan_ids if p not in set(ids)]
            if unknown:
                raise DeploymentRefusedError([_issue(_C.PLAN_INVALID, p) for p in unknown])
            wanted = set(plan_ids)
            plans = [p for p in plans if p.id in wanted]
        return [p for p in plans if p.status in LINK_ON_PAGE]

    async def _remove_request(
        self, row: Deployment, targets: Sequence[LinkPlan], operation_id: uuid.UUID, actor: str
    ) -> LinkChangeRequest:
        """A per-plan rollback: REMOVE changes for OUR links, against the CURRENT version."""
        try:
            page = await self._cms.get_page(page_id=row.cms_page_id)
        except CMSUnavailableError:
            raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)]) from None
        except CMSNotFoundError:
            raise DeploymentRefusedError([_issue(_C.CMS_PAGE_NOT_FOUND)]) from None
        stored = {
            c.change_id: c for c in LinkChangeRequest.model_validate_json(row.request_json).changes
        }
        changes = [
            stored[p.id].model_copy(
                update={
                    "operation": LinkOperation.REMOVE,
                    "previous_href": stored[p.id].href,
                    "previous_anchor_text": stored[p.id].anchor_text,
                }
            )
            for p in targets
        ]
        return LinkChangeRequest(
            operation_id=operation_id,
            source_page_id=row.cms_page_id,
            expected_version=page.version,
            changes=changes,
            actor=actor,
        )

    async def _rollback_intent(
        self,
        row: Deployment,
        *,
        request: RollbackRequest,
        principal: Principal,
        actor: str,
        fingerprint: str,
        targets: Sequence[LinkPlan] | None,
        remove_request: LinkChangeRequest | None,
    ) -> None:
        try:
            async with self._sessionmaker() as session, session.begin():
                locked = await repo.lock_deployment(session, deployment_id=row.id)
                if locked is None or DeploymentStatus(locked.status) not in ROLLBACKABLE:
                    raise DeploymentRefusedError([_issue(_C.DEPLOYMENT_IN_PROGRESS)])
                await repo.update_deployment(
                    session,
                    deployment_id=row.id,
                    status=_D.ROLLING_BACK.value,
                    rollback_from_status=locked.status,
                    rollback_operation_id=request.operation_id,
                    rollback_fingerprint=fingerprint,
                    rollback_reason=RollbackReason(request.reason).value,
                    rollback_note=request.note,
                    rollback_actor=actor,
                    rollback_user_id=principal.user_id,
                    rollback_plan_ids=None if targets is None else [str(p.id) for p in targets],
                    rollback_request_json=None
                    if remove_request is None
                    else remove_request.model_dump_json(),
                    updated_at=max(self._now(), locked.updated_at),
                )
        except IntegrityError as exc:
            if _ROLLBACK_OPERATION_UNIQUE in str(exc.orig):
                raise OperationReusedDeploymentError([]) from None
            raise

    async def _send_rollback(self, deployment_id: uuid.UUID, *, raise_errors: bool) -> str:
        """The CMS call of a ``rolling_back`` row (stored intent) and its outcome (tx2)."""
        row = await self._row(deployment_id)
        if DeploymentStatus(row.status) is not _D.ROLLING_BACK:
            return "noop"
        stored_remove = row.rollback_request_json
        try:
            if stored_remove is not None:
                remove = LinkChangeRequest.model_validate_json(stored_remove)
                applied = await self._cms.apply_link_changes(request=remove)
                restored = applied.new_version
            else:
                result = await self._cms.rollback(
                    deployment_id=row.cms_deployment_id or "",
                    operation_id=row.operation_id,
                    actor=row.rollback_actor or row.actor,
                )
                restored = result.restored_version
        except (CMSValidationError, CMSVersionConflictError, CMSNotFoundError) as exc:
            plan_ids = [uuid.UUID(p) for p in row.rollback_plan_ids or []] or await self._live_ids(
                row
            )
            if isinstance(exc, CMSVersionConflictError):
                refusal = DeploymentVersionConflictError.code
            elif isinstance(exc, CMSNotFoundError):
                refusal = _C.NOTHING_TO_ROLL_BACK.value
            else:
                refusal = _C.ROLLBACK_CONFLICT.value
            await self._revert_rollback(deployment_id, refusal=refusal, plan_ids=plan_ids)
            if not raise_errors:
                return "reverted"
            raise _rollback_refusal(refusal, plan_ids) from None
        except CMSUnavailableError:
            await self._touch(deployment_id)
            if raise_errors:
                raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)]) from None
            return "unknown"
        except Exception as exc:  # unknown outcome: stays rolling_back
            logger.error("rollback_cms_call_failed", error_class=type(exc).__name__)
            await self._touch(deployment_id)
            if raise_errors:
                raise CMSUnavailableDeploymentError([_issue(_C.CMS_UNAVAILABLE)]) from None
            return "unknown"
        await self._record_rolled_back(deployment_id, restored_version=restored)
        return "rolled_back"

    async def _live_ids(self, row: Deployment) -> list[uuid.UUID]:
        return [p.id for p in await self._rollback_targets(row, None)]

    async def _record_rolled_back(self, deployment_id: uuid.UUID, *, restored_version: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            row = await self._lock_for_outcome(session, deployment_id, _D.ROLLING_BACK)
            if row is None:
                return
            now = max(self._now(), row.updated_at)
            chosen = (
                None
                if row.rollback_plan_ids is None
                else {uuid.UUID(p) for p in row.rollback_plan_ids}
            )
            items = {
                i.plan_id: i for i in await repo.items_of(session, deployment_id=deployment_id)
            }
            reason = RollbackReason(row.rollback_reason or RollbackReason.OTHER.value)
            actor = row.rollback_actor or row.actor
            new_items = []
            remaining = 0
            for plan_row in await self._plan_rows(session, deployment_id):
                plan = rec_repo.plan_from_row(plan_row)
                if plan.status not in LINK_ON_PAGE:
                    continue
                if chosen is not None and plan.id not in chosen:
                    remaining += 1
                    continue
                await self._move(
                    session,
                    plan.id,
                    _S.ROLLED_BACK,
                    row,
                    f"rolled back ({reason.value})",
                    actor=actor,
                    operation_id=row.rollback_operation_id,
                )
                await self._emit(
                    session,
                    EventName.LINK_ROLLED_BACK,
                    plan.id,
                    LinkRolledBackPayload(
                        plan_id=plan.id,
                        deployment_id=row.id,
                        reason=reason,
                        restored_version=restored_version,
                        actor=actor,
                    ),
                    row.rollback_operation_id or row.operation_id,
                    now,
                )
                new_items.append(
                    self._item(
                        items.get(plan.id),
                        plan,
                        ItemStage.ROLLED_BACK,
                        operation_id=row.rollback_operation_id,
                        fingerprint=row.rollback_fingerprint,
                    )
                )
            if new_items:
                await repo.insert_items(
                    session, deployment_id=deployment_id, items=new_items, recorded_at=now
                )
            if remaining:
                # A per-plan rollback that leaves links live: back to the prior status.
                values: dict[str, Any] = {
                    "status": row.rollback_from_status or _D.APPLIED.value,
                    "rollback_from_status": None,
                }
            else:
                values = {
                    "status": _D.ROLLED_BACK.value,
                    "rollback_from_status": None,
                    "rolled_back_at": now,
                }
            await repo.update_deployment(
                session,
                deployment_id=deployment_id,
                restored_version=restored_version,
                updated_at=now,
                **values,
            )

    async def _revert_rollback(
        self, deployment_id: uuid.UUID, *, refusal: str, plan_ids: Sequence[uuid.UUID]
    ) -> None:
        """A refused rollback: the row returns to its prior status; plans are unchanged.

        The refusal is recorded as ``rollback_conflict`` items carrying the operation id, its
        fingerprint and the refusal code, so a replay of that operation returns the SAME
        refusal (:meth:`_rollback_replay`) instead of running again; the deployment's
        ``rollback_operation_id`` slot is freed for a later, different rollback."""
        async with self._sessionmaker() as session, session.begin():
            row = await repo.lock_deployment(session, deployment_id=deployment_id)
            if row is None or DeploymentStatus(row.status) is not _D.ROLLING_BACK:
                return
            now = max(self._now(), row.updated_at)
            if row.rollback_operation_id is not None:
                items = {
                    i.plan_id: i for i in await repo.items_of(session, deployment_id=deployment_id)
                }
                refused = []
                for plan_id in plan_ids:
                    plan_row = await session.get(LinkPlanRow, plan_id)
                    if plan_row is None:  # pragma: no cover - plans are never deleted
                        continue
                    refused.append(
                        self._item(
                            items.get(plan_id),
                            rec_repo.plan_from_row(plan_row),
                            ItemStage.ROLLBACK_CONFLICT,
                            [refusal],
                            operation_id=row.rollback_operation_id,
                            fingerprint=row.rollback_fingerprint,
                        )
                    )
                if refused:
                    await repo.insert_items(
                        session, deployment_id=deployment_id, items=refused, recorded_at=now
                    )
            await repo.update_deployment(
                session,
                deployment_id=deployment_id,
                status=row.rollback_from_status or _D.APPLIED.value,
                rollback_from_status=None,
                rollback_operation_id=None,
                rollback_fingerprint=None,
                rollback_request_json=None,
                error_code=_C.ROLLBACK_CONFLICT.value,
                updated_at=now,
            )

    # ------------------------------------------------------- reconciliation
    async def reconcile(self, *, older_than_s: float | None = None) -> ReconcileSummary:
        """Resend the STORED request of every ``applying`` / ``rolling_back`` row not touched
        for ``older_than_s`` (default ``KEN_DEPLOY_RECONCILE_AFTER_S``): ``make
        deployments-reconcile``. A row whose source is locked (a deployment in flight) is
        skipped. An applied outcome is verified once."""
        horizon = self._settings.deploy_reconcile_after_s if older_than_s is None else older_than_s
        cutoff = self._now() - timedelta(seconds=horizon)
        summary = ReconcileSummary()
        async with self._sessionmaker() as session:
            rows = await repo.stuck_deployments(session, updated_before=cutoff)
        for row in rows:
            summary.examined += 1
            try:
                async with self._source_lock(row.source_page_id):
                    current = await self._row(row.id)
                    status = DeploymentStatus(current.status)
                    if status is _D.APPLYING:
                        outcome = await self._send(row.id)
                        if outcome is _Outcome.APPLIED:
                            summary.applied += 1
                            await self._verify_once(row.id)
                        elif outcome is _Outcome.FAILED:
                            summary.failed += 1
                        elif outcome is _Outcome.CONFLICT:
                            summary.conflict += 1
                        else:
                            summary.still_unknown += 1
                    elif status is _D.ROLLING_BACK:
                        result = await self._send_rollback(row.id, raise_errors=False)
                        if result == "rolled_back":
                            summary.rolled_back += 1
                        elif result == "reverted":
                            summary.reverted += 1
                        elif result == "unknown":
                            summary.still_unknown += 1
            except DeploymentRefusedError:
                summary.skipped_locked += 1
        logger.info("deployments_reconciled", **{k: getattr(summary, k) for k in summary.__slots__})
        return summary
