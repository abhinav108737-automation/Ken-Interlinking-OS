"""``SimulatedCMSProvider``: a local CMS that behaves like one (ADR-0014 s4 and s8).

**Isolation.** It owns its OWN engine and connections to its OWN database (``ken_cms_sim``
on the project PostgreSQL by default, ``KEN_CMS_SIM_DATABASE_URL``), OUTSIDE the
application's Alembic chain, and commits independently of any application session -- like a
real CMS. The schema (``cms_sim``) is created idempotently by :meth:`ensure_schema`;
``make cms-sim-seed`` creates the database when missing and seeds it from the inventory.
The URL is refused unless it names a host-local server other than the developer's own
PostgreSQL on port 5432, and a database whose name starts with ``ken_cms_sim`` -- judged on
the target the asyncpg driver will ACTUALLY connect to, so query-string overrides
(``?host=``, ``?port=``, ``?database=``) and multi-host lists cannot slip past.

**Behaviour** (the provider contract every CMS provider is tested against):

* pages carry FULL stored HTML (or the export fragment); the version token is opaque;
* ``apply_link_changes``, under the page row lock (``SELECT ... FOR UPDATE``): an
  ``operation_id`` already applied with the identical request (all fields but ``dry_run``)
  REPLAYS the stored result (``idempotent_replay``), even if the page has moved since; a
  different request with that id is ``operation_id_reused``; then unknown / protected page
  and a missing actor (terminal), a version mismatch (:class:`CMSVersionConflictError`), any
  other issue (:class:`CMSValidationError`); the real splice through the injected
  :class:`LinkHtmlEngine`; the operation ledger row and a snapshot of the page before.
  ``applied_change_ids`` lists only the changes the splice APPLIED; when nothing changed
  (every change already in that state) the result is ``no_op`` with the version unchanged;
* ``dry_run`` writes nothing and never consumes the ``operation_id``;
* ``rollback``: snapshot restore while the page is still at the deployment's
  ``new_version``, else the inverse splice of the changes it APPLIED on the CURRENT html
  (:func:`~ken_core.providers.cms.html_engine.revert_deployment`; safe after unrelated later
  edits); refused with ``rollback_conflict`` only when one of our links itself changed; a
  deployment that applied nothing rolls back as ``no_op`` without touching the page; a
  second rollback replays with ``status="no_op"``;
* ``resolve_page_id`` maps a URL to its page (inventory pages without a CMS id).

**Fault injection** (:class:`SimFaults`) for crash-consistency tests: ``fail_before_commit``
does the work, rolls it back and raises :class:`CMSUnavailableError` (the app committed its
intent, the CMS "timed out" and applied nothing); ``fail_after_commit`` COMMITS and then
raises (the CMS applied, the app never learns the outcome). Reconciliation resends the
stored request: the first case applies it then, the second replays the ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

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
from ken_core.config import NATIVE_POSTGRES_PORT, ConfigurationError, _as_list, _is_host_local
from ken_core.providers.cms.html_engine import HtmlOutcome, LinkHtmlEngine, revert_deployment
from ken_core.providers.cms.mock import IssueCode

__all__ = [
    "PROVIDER_NAME",
    "SCHEMA",
    "SimFaults",
    "SimulatedCMSProvider",
    "assert_safe_sim_url",
    "ensure_database",
]

PROVIDER_NAME: Final = "simulated"
SCHEMA: Final = "cms_sim"
_DB_NAME: Final = re.compile(r"ken_cms_sim[a-z0-9_]{0,40}")
_TERMINAL: Final = frozenset(
    {IssueCode.UNKNOWN_SOURCE_PAGE, IssueCode.PROTECTED_PAGE, IssueCode.MISSING_ACTOR}
)

_DDL: Final = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.pages (
        page_id text PRIMARY KEY,
        url text NOT NULL UNIQUE,
        canonical_url text,
        path text NOT NULL UNIQUE,
        title text NOT NULL,
        body_html text NOT NULL,
        version text NOT NULL,
        protected boolean NOT NULL DEFAULT false,
        published_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.page_snapshots (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        page_id text NOT NULL REFERENCES {SCHEMA}.pages (page_id),
        version text NOT NULL,
        body_html text NOT NULL,
        reason text NOT NULL,
        recorded_at timestamptz NOT NULL DEFAULT now()
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.operations (
        operation_id uuid PRIMARY KEY,
        deployment_id text NOT NULL UNIQUE,
        page_id text NOT NULL REFERENCES {SCHEMA}.pages (page_id),
        fingerprint text NOT NULL,
        request_json text NOT NULL,
        result_json text NOT NULL,
        page_before_html text NOT NULL,
        page_before_version text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.rollbacks (
        deployment_id text PRIMARY KEY REFERENCES {SCHEMA}.operations (deployment_id),
        result_json text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
)


@dataclass(slots=True)
class SimFaults:
    """One-shot (counted) fault injection; see the module docstring."""

    fail_before_commit: int = 0
    fail_after_commit: int = 0
    unavailable: bool = False

    def take_before(self) -> bool:
        if self.fail_before_commit > 0:
            self.fail_before_commit -= 1
            return True
        return False

    def take_after(self) -> bool:
        if self.fail_after_commit > 0:
            self.fail_after_commit -= 1
            return True
        return False


class _InjectedFaultError(Exception):
    """Raised inside the transaction to roll it back (fail_before_commit)."""


def assert_safe_sim_url(url: str) -> str:
    """The simulated CMS database URL, or :class:`ConfigurationError` (module docstring).

    Every target is judged as the asyncpg dialect resolves it (``create_connect_args``), the
    way ``Settings.targets_native_postgres`` does: a ``?host=`` / ``?port=`` / ``?database=``
    override or a multi-host list is checked host by host, and the effective database must be
    the URL's own. The message never echoes the URL (it may carry credentials)."""
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

    try:
        parsed = make_url(url)
    except Exception:
        raise ConfigurationError("KEN_CMS_SIM_DATABASE_URL is not a valid URL") from None
    if parsed.drivername != "postgresql+asyncpg":
        raise ConfigurationError("KEN_CMS_SIM_DATABASE_URL must use postgresql+asyncpg://")
    try:
        # SQLAlchemy ships these dialect internals without annotations.
        dialect = PGDialect_asyncpg()  # type: ignore[no-untyped-call]
        _, kwargs = dialect.create_connect_args(parsed)  # type: ignore[no-untyped-call]
    except Exception:
        raise ConfigurationError("KEN_CMS_SIM_DATABASE_URL is not a valid URL") from None
    hosts = _as_list(kwargs.get("host"))
    ports = _as_list(kwargs.get("port"))
    for index, host in enumerate(hosts or [""]):
        if not _is_host_local(str(host)):
            raise ConfigurationError("the simulated CMS database must be on this machine")
        raw_port = ports[index] if index < len(ports) else (ports[0] if ports else "")
        try:
            text_port = str(raw_port).strip()
            port = int(text_port) if text_port else NATIVE_POSTGRES_PORT  # libpq default
        except ValueError:
            raise ConfigurationError("KEN_CMS_SIM_DATABASE_URL has an invalid port") from None
        if port == NATIVE_POSTGRES_PORT:
            raise ConfigurationError(
                "the simulated CMS database must be on the project PostgreSQL "
                "(127.0.0.1:5433), never the developer's own server on port 5432"
            )
    database = kwargs.get("database")
    if (
        parsed.database is None
        or database != parsed.database
        or _DB_NAME.fullmatch(parsed.database) is None
    ):
        raise ConfigurationError("the simulated CMS database name must start with ken_cms_sim")
    return url


async def ensure_database(url: str) -> bool:
    """Create the (project-local, disposable) simulated-CMS database if it is missing.
    Returns True when it was created. Connects to the same server's ``postgres`` database."""
    assert_safe_sim_url(url)
    parsed = make_url(url)
    name = parsed.database or ""
    admin = create_async_engine(
        parsed.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            )
            if exists:
                return False
            # The name is validated by _DB_NAME (no quoting tricks possible).
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            return True
    finally:
        await admin.dispose()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _token() -> str:
    return f"sim-{uuid.uuid4().hex}"


def _fingerprint(request: LinkChangeRequest) -> str:
    """Request identity for idempotency (``dry_run`` excluded, as in the mock)."""
    return json.dumps(
        request.model_dump(mode="json", exclude={"dry_run"}), sort_keys=True, separators=(",", ":")
    )


def _path_of(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _issue(
    code: IssueCode | str, message: str, change_id: uuid.UUID | None = None
) -> ValidationIssue:
    value = code.value if isinstance(code, IssueCode) else code
    return ValidationIssue(code=value, severity="error", message=message, change_id=change_id)


def _page_from_row(row: Any) -> CMSPage:
    return CMSPage(
        page_id=row.page_id,
        url=row.url,
        title=row.title,
        body_html=row.body_html,
        version=row.version,
        canonical_url=row.canonical_url,
        protected=row.protected,
        published_at=row.published_at,
        updated_at=row.updated_at,
        metadata=dict(row.metadata or {}),
    )


def stable_page_id(url: str) -> str:
    """A deterministic simulated page id for a page without a CMS ``external_id``."""
    return "sim-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


class SimulatedCMSProvider:
    """:class:`~ken_contracts.cms.CMSProvider` over its own PostgreSQL database."""

    name: str = PROVIDER_NAME
    preserves_data_attributes: bool = True

    def __init__(
        self,
        *,
        database_url: str,
        html_engine: LinkHtmlEngine | None,
        clock: Callable[[], datetime] | None = None,
        faults: SimFaults | None = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        assert_safe_sim_url(database_url)
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_size=2,
            pool_pre_ping=True,
            connect_args={"timeout": connect_timeout_s},
        )
        self._html = html_engine
        self._clock = clock or _utc_now
        self.faults = faults or SimFaults()
        self._closed = False

    # ------------------------------------------------------------ lifecycle
    async def ensure_schema(self) -> None:
        async with self._engine.begin() as connection:
            for statement in _DDL:
                await connection.execute(text(statement))

    async def reset(self) -> None:
        """Delete every simulated page, operation and snapshot (seed ``--reset``, tests)."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    f"TRUNCATE {SCHEMA}.rollbacks, {SCHEMA}.operations, "
                    f"{SCHEMA}.page_snapshots, {SCHEMA}.pages"
                )
            )

    async def upsert_pages(self, pages: Iterable[CMSPage], *, replace: bool = False) -> int:
        """Seed pages. Existing pages are kept unless ``replace`` (then body, title and a NEW
        version token are written). Returns the number written."""
        written = 0
        async with self._engine.begin() as connection:
            for page in pages:
                values = {
                    "id": page.page_id,
                    "url": page.url,
                    "canonical": page.canonical_url,
                    "path": _path_of(page.url),
                    "title": page.title,
                    "body": page.body_html,
                    "version": page.version or _token(),
                    "protected": page.protected,
                    "published": page.published_at,
                    "updated": page.updated_at or self._clock(),
                    "metadata": json.dumps(page.metadata),
                }
                conflict = (
                    "DO UPDATE SET title = EXCLUDED.title, body_html = EXCLUDED.body_html, "
                    "version = EXCLUDED.version, protected = EXCLUDED.protected, "
                    "updated_at = EXCLUDED.updated_at"
                    if replace
                    else "DO NOTHING"
                )
                result = await connection.execute(
                    text(
                        f"INSERT INTO {SCHEMA}.pages (page_id, url, canonical_url, path, title, "
                        "body_html, version, protected, published_at, updated_at, metadata) "
                        "VALUES (:id, :url, :canonical, :path, :title, :body, :version, "
                        ":protected, :published, :updated, CAST(:metadata AS jsonb)) "
                        f"ON CONFLICT (page_id) {conflict}"
                    ),
                    values,
                )
                written += result.rowcount or 0
        return written

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._engine.dispose()

    # ------------------------------------------------------------- helpers
    def _check_open(self) -> None:
        if self._closed:
            raise CMSUnavailableError("SimulatedCMSProvider has been closed")
        if self.faults.unavailable:
            raise CMSUnavailableError("simulated CMS unavailable (fault injection)")

    async def _page(self, connection: AsyncConnection, page_id: str, *, lock: bool) -> Any:
        suffix = " FOR UPDATE" if lock else ""
        return (
            await connection.execute(
                text(f"SELECT * FROM {SCHEMA}.pages WHERE page_id = :id{suffix}"), {"id": page_id}
            )
        ).first()

    async def _page_ids(self, connection: AsyncConnection, ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        rows = await connection.execute(
            text(f"SELECT page_id FROM {SCHEMA}.pages WHERE page_id = ANY(:ids)"),
            {"ids": list(ids)},
        )
        return {r.page_id for r in rows}

    async def _issues(
        self, connection: AsyncConnection, request: LinkChangeRequest, page: Any
    ) -> tuple[list[ValidationIssue], bool]:
        """(issues, request_unusable): the mock's request + change rules, over this DB."""
        issues: list[ValidationIssue] = []
        if page is None:
            issues.append(_issue(IssueCode.UNKNOWN_SOURCE_PAGE, "No such source page."))
        else:
            if page.protected:
                issues.append(_issue(IssueCode.PROTECTED_PAGE, "The page is protected."))
            if page.version != request.expected_version:
                issues.append(
                    _issue(
                        IssueCode.STALE_PAGE_VERSION,
                        "expected_version does not match the page's current version.",
                    )
                )
        if not request.actor.strip():
            issues.append(_issue(IssueCode.MISSING_ACTOR, "A mutation must name its authoriser."))
        unusable = bool(issues)
        known = await self._page_ids(connection, [c.target_page_id for c in request.changes])
        seen: set[uuid.UUID] = set()
        for change in request.changes:
            cid = change.change_id
            if cid in seen:
                issues.append(_issue(IssueCode.DUPLICATE_CHANGE_ID, "change_id repeated.", cid))
            seen.add(cid)
            if change.target_page_id == request.source_page_id:
                issues.append(_issue(IssueCode.SELF_LINK, "A page may not link to itself.", cid))
            elif change.target_page_id not in known:
                issues.append(_issue(IssueCode.UNKNOWN_TARGET_PAGE, "No such target page.", cid))
            if change.operation is LinkOperation.INSERT:
                if change.previous_href is not None or change.previous_anchor_text is not None:
                    issues.append(
                        _issue(IssueCode.INSERT_HAS_PREVIOUS_FIELDS, "INSERT sets previous_*.", cid)
                    )
            else:
                if change.previous_href is None:
                    issues.append(_issue(IssueCode.MISSING_PREVIOUS_HREF, "No previous_href.", cid))
                if change.previous_anchor_text is None:
                    issues.append(
                        _issue(
                            IssueCode.MISSING_PREVIOUS_ANCHOR_TEXT, "No previous_anchor_text.", cid
                        )
                    )
        return issues, unusable

    def _mutate(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome | None:
        return None if self._html is None else self._html.apply(html, changes)

    async def _ledger(self, connection: AsyncConnection, operation_id: uuid.UUID) -> Any:
        return (
            await connection.execute(
                text(f"SELECT * FROM {SCHEMA}.operations WHERE operation_id = :o"),
                {"o": operation_id},
            )
        ).first()

    # ------------------------------------------------------------ protocol
    async def resolve_page_id(self, *, url: str) -> str | None:
        self._check_open()
        try:
            async with self._engine.connect() as connection:
                value = await connection.scalar(
                    text(
                        f"SELECT page_id FROM {SCHEMA}.pages WHERE url = :u OR canonical_url = :u "
                        "ORDER BY page_id LIMIT 1"
                    ),
                    {"u": url},
                )
        except (OSError, SQLAlchemyError) as exc:
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        return None if value is None else str(value)

    async def page_by_path(self, path: str) -> CMSPage | None:
        """The page served at ``path`` (``make cms-sim-serve``), or None."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(f"SELECT * FROM {SCHEMA}.pages WHERE path = :p"), {"p": path}
                )
            ).first()
        return None if row is None else _page_from_row(row)

    async def list_pages(self) -> list[CMSPage]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(text(f"SELECT * FROM {SCHEMA}.pages ORDER BY path"))
            ).all()
        return [_page_from_row(r) for r in rows]

    async def edit_page(self, *, page_id: str, body_html: str) -> CMSPage:
        """SIMULATION helper: an editor's change (snapshot + a new version token)."""
        async with self._engine.begin() as connection:
            row = await self._page(connection, page_id, lock=True)
            if row is None:
                raise CMSNotFoundError("no such page")
            await self._snapshot(connection, row, "edit")
            await connection.execute(
                text(
                    f"UPDATE {SCHEMA}.pages SET body_html = :b, version = :v, updated_at = :t "
                    "WHERE page_id = :id"
                ),
                {"b": body_html, "v": _token(), "t": self._clock(), "id": page_id},
            )
            updated = await self._page(connection, page_id, lock=False)
        return _page_from_row(updated)

    async def get_page(self, *, page_id: str) -> CMSPage:
        self._check_open()
        try:
            async with self._engine.connect() as connection:
                row = await self._page(connection, page_id, lock=False)
        except (OSError, SQLAlchemyError) as exc:
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        if row is None:
            raise CMSNotFoundError("no such page")
        return _page_from_row(row)

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> ValidationResult:
        self._check_open()
        try:
            async with self._engine.connect() as connection:
                page = await self._page(connection, request.source_page_id, lock=False)
                issues, unusable = await self._issues(connection, request, page)
                stored = await self._ledger(connection, request.operation_id)
        except (OSError, SQLAlchemyError) as exc:
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        if stored is not None and stored.fingerprint != _fingerprint(request):
            issues.append(
                _issue(IssueCode.OPERATION_ID_REUSED, "operation_id was used for another request.")
            )
        if not issues and page is not None:
            outcome = self._mutate(page.body_html, request.changes)
            if outcome is not None and not outcome.ok:
                issues.extend(outcome.issues)
        failed = {i.change_id for i in issues}
        normalized = [] if unusable else [c for c in request.changes if c.change_id not in failed]
        return ValidationResult(valid=not issues, issues=issues, normalized_changes=normalized)

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        self._check_open()
        if request.dry_run:
            return await self._dry_run(request)
        commit_then_fail = False
        try:
            async with self._engine.begin() as connection:
                page = await self._page(connection, request.source_page_id, lock=True)
                stored = await self._ledger(connection, request.operation_id)
                if stored is not None:
                    if stored.fingerprint != _fingerprint(request):
                        raise CMSValidationError(
                            "operation_id reused with a different request",
                            issues=[
                                _issue(
                                    IssueCode.OPERATION_ID_REUSED,
                                    "operation_id was already applied with a different request.",
                                )
                            ],
                        )
                    replay = DeploymentResult.model_validate_json(stored.result_json)
                    return replay.model_copy(update={"idempotent_replay": True})
                issues, _unusable = await self._issues(connection, request, page)
                self._raise_for(request, issues, page)
                outcome = self._mutate(page.body_html, request.changes)
                if outcome is not None and not outcome.ok:
                    raise CMSValidationError(
                        "Link change request rejected", issues=list(outcome.issues)
                    )
                now = self._clock()
                body = page.body_html if outcome is None else outcome.html
                changed = outcome is None or outcome.changed
                new_version = _token() if changed else page.version
                applied_ids = (
                    [c.change_id for c in request.changes]
                    if outcome is None
                    else [
                        c.change_id for c in request.changes if c.change_id in set(outcome.applied)
                    ]
                )
                result = DeploymentResult(
                    deployment_id=f"simdep-{uuid.uuid4().hex}",
                    operation_id=request.operation_id,
                    source_page_id=request.source_page_id,
                    status="applied" if changed else "no_op",
                    applied_change_ids=applied_ids,
                    previous_version=page.version,
                    new_version=new_version,
                    applied_at=now,
                )
                if changed:
                    await self._snapshot(connection, page, f"before {result.deployment_id}")
                    await connection.execute(
                        text(
                            f"UPDATE {SCHEMA}.pages SET body_html = :b, version = :v, "
                            "updated_at = :t WHERE page_id = :id"
                        ),
                        {"b": body, "v": new_version, "t": now, "id": page.page_id},
                    )
                await connection.execute(
                    text(
                        f"INSERT INTO {SCHEMA}.operations (operation_id, deployment_id, page_id, "
                        "fingerprint, request_json, result_json, page_before_html, "
                        "page_before_version) VALUES (:o, :d, :p, :f, :r, :res, :bh, :bv)"
                    ),
                    {
                        "o": request.operation_id,
                        "d": result.deployment_id,
                        "p": page.page_id,
                        "f": _fingerprint(request),
                        "r": request.model_dump_json(),
                        "res": result.model_dump_json(),
                        "bh": page.body_html,
                        "bv": page.version,
                    },
                )
                if self.faults.take_before():
                    raise _InjectedFaultError
                commit_then_fail = self.faults.take_after()
        except _InjectedFaultError:
            raise CMSUnavailableError("simulated CMS timeout before commit") from None
        except DBAPIError as exc:
            # A concurrent identical operation won the ledger insert: replay it.
            if "operations_pkey" in str(exc.orig):
                return await self.apply_link_changes(request=request)
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        except (OSError, SQLAlchemyError) as exc:
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        if commit_then_fail:
            raise CMSUnavailableError("simulated CMS timeout after commit")
        return result

    async def _dry_run(self, request: LinkChangeRequest) -> DeploymentResult:
        try:
            async with self._engine.connect() as connection:
                page = await self._page(connection, request.source_page_id, lock=False)
                stored = await self._ledger(connection, request.operation_id)
                if stored is not None:
                    if stored.fingerprint != _fingerprint(request):
                        raise CMSValidationError(
                            "operation_id reused with a different request",
                            issues=[_issue(IssueCode.OPERATION_ID_REUSED, "operation_id reused.")],
                        )
                    current = page.version if page is not None else stored.page_before_version
                    return DeploymentResult(
                        deployment_id=stored.deployment_id,
                        operation_id=request.operation_id,
                        source_page_id=request.source_page_id,
                        status="no_op",
                        previous_version=current,
                        new_version=current,
                        applied_at=self._clock(),
                        idempotent_replay=True,
                    )
                issues, _unusable = await self._issues(connection, request, page)
        except (OSError, SQLAlchemyError) as exc:
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        self._raise_for(request, issues, page)
        outcome = self._mutate(page.body_html, request.changes)
        if outcome is not None and not outcome.ok:
            raise CMSValidationError("Link change request rejected", issues=list(outcome.issues))
        return DeploymentResult(
            deployment_id=f"dryrun-{uuid.uuid4().hex}",
            operation_id=request.operation_id,
            source_page_id=request.source_page_id,
            status="no_op",
            previous_version=page.version,
            new_version=page.version,
            applied_at=self._clock(),
        )

    @staticmethod
    def _raise_for(request: LinkChangeRequest, issues: list[ValidationIssue], page: Any) -> None:
        if not issues:
            return
        codes = {i.code for i in issues}
        if codes & {c.value for c in _TERMINAL} or IssueCode.STALE_PAGE_VERSION.value not in codes:
            raise CMSValidationError("Link change request rejected", issues=issues)
        raise CMSVersionConflictError(
            "Page version has changed; regenerate from current content.",
            expected=request.expected_version,
            current=page.version,
        )

    async def _snapshot(self, connection: AsyncConnection, page: Any, reason: str) -> None:
        await connection.execute(
            text(
                f"INSERT INTO {SCHEMA}.page_snapshots (page_id, version, body_html, reason) "
                "VALUES (:p, :v, :b, :r)"
            ),
            {"p": page.page_id, "v": page.version, "b": page.body_html, "r": reason[:200]},
        )

    async def rollback(
        self, *, deployment_id: str, operation_id: OperationId, actor: str
    ) -> RollbackResult:
        self._check_open()
        commit_then_fail = False
        try:
            async with self._engine.begin() as connection:
                op = (
                    await connection.execute(
                        text(f"SELECT * FROM {SCHEMA}.operations WHERE deployment_id = :d"),
                        {"d": deployment_id},
                    )
                ).first()
                if op is None:
                    raise CMSNotFoundError("no such deployment")
                if not actor.strip():
                    raise CMSValidationError(
                        "Rollback rejected",
                        issues=[
                            _issue(IssueCode.MISSING_ACTOR, "A rollback must name its authoriser.")
                        ],
                    )
                if op.operation_id != operation_id:
                    raise CMSValidationError(
                        "Rollback rejected",
                        issues=[
                            _issue(
                                IssueCode.DEPLOYMENT_OPERATION_MISMATCH,
                                "operation_id does not match the deployment's operation.",
                            )
                        ],
                    )
                page = await self._page(connection, op.page_id, lock=True)
                done = (
                    await connection.execute(
                        text(
                            f"SELECT result_json FROM {SCHEMA}.rollbacks WHERE deployment_id = :d"
                        ),
                        {"d": deployment_id},
                    )
                ).first()
                if done is not None:
                    previous = RollbackResult.model_validate_json(done.result_json)
                    return previous.model_copy(update={"status": "no_op"})
                applied = DeploymentResult.model_validate_json(op.result_json)
                request = LinkChangeRequest.model_validate_json(op.request_json)
                now = self._clock()
                if not applied.applied_change_ids:
                    # The deployment changed nothing: nothing of ours to take out.
                    result = RollbackResult(
                        deployment_id=deployment_id,
                        status="no_op",
                        restored_version=page.version,
                        rolled_back_at=now,
                        actor=actor,
                    )
                else:
                    if page.version == applied.new_version:
                        body = op.page_before_html
                    elif self._html is None:
                        raise CMSValidationError(
                            "Rollback rejected",
                            issues=[_issue(IssueCode.ROLLBACK_CONFLICT, "The page changed since.")],
                        )
                    else:
                        reverted = revert_deployment(
                            self._html, page.body_html, request.changes, applied.applied_change_ids
                        )
                        if not reverted.ok:
                            raise CMSValidationError(
                                "Rollback rejected", issues=list(reverted.issues)
                            )
                        body = reverted.html
                    restored = _token()
                    await self._snapshot(connection, page, f"before rollback {deployment_id}")
                    await connection.execute(
                        text(
                            f"UPDATE {SCHEMA}.pages SET body_html = :b, version = :v, "
                            "updated_at = :t WHERE page_id = :id"
                        ),
                        {"b": body, "v": restored, "t": now, "id": page.page_id},
                    )
                    result = RollbackResult(
                        deployment_id=deployment_id,
                        status="rolled_back",
                        restored_version=restored,
                        rolled_back_at=now,
                        actor=actor,
                    )
                await connection.execute(
                    text(
                        f"INSERT INTO {SCHEMA}.rollbacks (deployment_id, result_json) "
                        "VALUES (:d, :r)"
                    ),
                    {"d": deployment_id, "r": result.model_dump_json()},
                )
                if self.faults.take_before():
                    raise _InjectedFaultError
                commit_then_fail = self.faults.take_after()
        except _InjectedFaultError:
            raise CMSUnavailableError("simulated CMS timeout before commit") from None
        except (OSError, SQLAlchemyError) as exc:
            raise CMSUnavailableError("simulated CMS unavailable") from exc
        if commit_then_fail:
            raise CMSUnavailableError("simulated CMS timeout after commit")
        return result

    async def health(self) -> ProviderHealth:
        if self._closed:
            return ProviderHealth(provider=PROVIDER_NAME, reachable=False, error="provider closed")
        try:
            async with self._engine.connect() as connection:
                pages = await connection.scalar(text(f"SELECT count(*) FROM {SCHEMA}.pages"))
        except Exception as exc:
            return ProviderHealth(provider=PROVIDER_NAME, reachable=False, error=type(exc).__name__)
        return ProviderHealth(
            provider=PROVIDER_NAME,
            reachable=True,
            detail={"pages": int(pages or 0), "data_attributes_preserved": True},
        )
