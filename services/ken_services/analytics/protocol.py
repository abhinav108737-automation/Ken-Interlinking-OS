"""Phase 7 analytics service interfaces (docs/adr/ADR-0015-analytics.md, section 12 binding).

FROZEN (orchestrator). The importer / aggregation / attribution / measurement (W1) and the
first-party collector (W2) implement these; neither changes them. A few tiny pure helpers
are implemented HERE (:func:`normalized_path`, :func:`dedupe_key`, :func:`validate_range`,
``CollectorConfig.from_settings``) so that both sides compute them byte-identically.

Rules (binding):

* **Live window** (amendment 3). A plan's window is ``[deployed_at, rolled_back_at)`` from its
  ``link_plan_transitions`` rows ``to_status = 'deployed'`` and ``to_status = 'rolled_back'``
  -- NEVER from the ``deployment_items`` stage (a CMS no-op records ``already_applied`` while
  the plan still becomes ``deployed``). A plan never moved to ``deployed`` was never on a page
  (superseded, rejected, never applied): its events are ``link_not_live``.
* **Live plans** (amendment 1e). The :class:`LiveLinkIndex` holds plans whose window is open
  and whose status is in ``deployment.service.LINK_ON_PAGE`` -- which includes ``failed``, on
  purpose (the link may be on the page). Measuring is only ever from ``verified``; a plan whose
  verification stays inconclusive stays ``deployed`` and never measures.
* **Paths** (amendment 1d). Both sides compare :func:`normalized_path` only: the path of the
  normalised URL, query string and fragment dropped. ``imp``/``clk`` match the plan's
  ``source_url``.
* **attr mode**: ``(plan_id, path)``. Live plan and equal path -> accepted; live plan, other
  path -> ``path_mismatch``; otherwise ``link_not_live``.
* **href mode** (amendment 4): among live plans whose source path equals ``path``, those whose
  ``deployment_items.href`` (the href actually written; stage ``applied``/``already_applied``)
  is ``href_rules.hrefs_equivalent`` to the event's ``h`` -- both first resolved against the
  plan's ``source_url`` -- AND whose ``anchor_text`` equals ``a`` exactly. One -> accepted;
  none -> ``unresolved_link``; several -> ``ambiguous_link``.
* **Ingest** (collector): ``received_at`` = now truncated to the MINUTE (UTC); ``event_date``
  its UTC date; ``synthetic`` from :class:`CollectorConfig` (never the client); insert with
  ``ON CONFLICT (dedupe_key) DO NOTHING``. ``pv`` is never stored. Every response is 204.
* **Aggregation** re-checks the window: an event counts only if
  ``date_trunc('minute', deployed_at) <= received_at < rolled_back_at``.
* **Replace imports** run in ONE transaction: ``set_config('ken.analytics_writer', 'on',
  true)``; refuse ``synthetic_real_mix`` / ``via_mismatch`` (unless ``replace_via``); DELETE
  the (source, dataset, range) rows; INSERT sorted by natural key, duplicate keys summed; insert
  the ledger row; run :class:`MeasureSweep` (links datasets, non-synthetic only); emit
  ``analytics.updated`` -- all before COMMIT.
* **Measure lock order** (amendment 12, §12.4): per source page, sorted by id:
  ``pg_try_advisory_xact_lock(deploy_lock_key(source))`` (busy -> skip the source), skip a
  source with a ``rolling_back`` deployment, ``lock_source_plans``, the plan row lock, then the
  transition with ``expected_from = VERIFIED`` by :data:`~ken_contracts.analytics.ANALYTICS_ACTOR`
  plus the ``link_measurements`` row, in the caller's transaction.
* First-party counts are SPOOFABLE (Origin and ``pv`` are attacker-chosen): Phase 9 must never
  graduate a decision class on first-party data alone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    MAX_RANGE_DAYS,
    AnalyticsBatchProvider,
    AnalyticsPolicy,
    AnalyticsSource,
    CollectorPolicy,
    Dataset,
    DateRange,
    ImportVia,
    LiveWindow,
    MatchMode,
)
from ken_contracts.common import FrozenModel, reject_unstorable_text
from ken_core.config import Settings, is_loopback_host, parse_origin
from ken_services.inventory.urls import try_normalize_url

__all__ = [
    "COUNTER_NAMES",
    "WIRE_EVENT_TYPES",
    "AnalyticsImportError",
    "AttrEvent",
    "CollectorConfig",
    "CollectorPayload",
    "CollectorRejection",
    "HrefEvent",
    "ImportErrorCode",
    "ImportOutcome",
    "ImportRequest",
    "LinkResolution",
    "LiveLinkIndex",
    "LiveWindows",
    "MeasureOutcome",
    "MeasureSweep",
    "RunAggregation",
    "RunImport",
    "dedupe_key",
    "normalized_path",
    "validate_range",
]

_PATH_BASE: Final = "http://path.invalid/"

COUNTER_NAMES: Final = frozenset(
    {
        "rows_read",
        "rows_written",
        "merged_rows",
        "unresolved_url",
        "unresolved_link",
        "ambiguous_url",
        "outside_live_window",
        "outside_range",
        "non_daily_row",
        "other_row",
        "rejected_invalid",
        "plans_measured",
        "plans_skipped_busy",
    }
)
"""``job_runs.counters`` of every ``analytics_import`` run (import and aggregation alike)."""


# --------------------------------------------------------------------------- #
# Shared pure helpers (implemented here: both workstreams must agree byte for byte)
# --------------------------------------------------------------------------- #
def normalized_path(value: str) -> str | None:
    """The comparable path of a URL or a bare path: ``normalize_url`` semantics (percent
    encoding, dot segments), query string and fragment dropped. ``None`` if unparseable."""
    normalised = try_normalize_url(value, base=_PATH_BASE)
    if normalised is None:
        return None
    return urlsplit(normalised).path or "/"


def dedupe_key(pv: str, event_type: Literal["imp", "clk"], plan_id: UUID) -> str:
    """``link_events.dedupe_key``: sha256 of ``pv|type|plan_id`` (one event per link per
    page view and type). The page-view id itself is never stored."""
    return hashlib.sha256(f"{pv}|{event_type}|{plan_id}".encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- #
# Collector: wire schema (Pydantic, extra=forbid) and configuration
# --------------------------------------------------------------------------- #
WIRE_EVENT_TYPES: Final[Mapping[str, str]] = {
    "imp": "internal_link_impression",
    "clk": "internal_link_click",
}


def _storable(value: str) -> str:
    return reject_unstorable_text(value)


def _path_only(value: str) -> str:
    reject_unstorable_text(value)
    if (
        not value.startswith("/")
        or any(c in value for c in "?#")
        or any(c.isspace() for c in value)
    ):
        raise ValueError("path is a URL path only (no query, fragment or whitespace)")
    return value


class AttrEvent(FrozenModel):
    """attr mode: the link's ``data-ken-link-id``."""

    t: Literal["imp", "clk"]
    l: UUID  # noqa: E741 -- the wire name


class HrefEvent(FrozenModel):
    """href mode (a CMS that strips ``data-*``): the raw ``href`` attribute and anchor text."""

    t: Literal["imp", "clk"]
    h: Annotated[str, Field(min_length=1, max_length=2048), AfterValidator(_storable)]
    a: Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_storable)]


class CollectorPayload(FrozenModel):
    """``POST /e`` body (``text/plain`` JSON, at most 4 KiB streamed)."""

    v: Literal[1]
    pv: str = Field(pattern=r"^[0-9a-f]{32}$", description="Random per page load; never stored.")
    path: Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_path_only)]
    events: list[AttrEvent | HrefEvent] = Field(min_length=1, max_length=50)


class CollectorRejection(StrEnum):
    """Why the collector dropped a request or an event (in-memory counters, logged every
    ``rejection_log_interval_seconds``; the response is still 204)."""

    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    BODY_TOO_LARGE = "body_too_large"
    BAD_CONTENT_TYPE = "bad_content_type"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    BOT = "bot"
    PREFETCH = "prefetch"
    GPC = "gpc"
    RATE_LIMITED = "rate_limited"
    INVALID_SCHEMA = "invalid_schema"
    LINK_NOT_LIVE = "link_not_live"
    PATH_MISMATCH = "path_mismatch"
    UNRESOLVED_LINK = "unresolved_link"
    AMBIGUOUS_LINK = "ambiguous_link"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    host: str
    """Loopback IP literal unless ``allow_remote``; the port is always 0 (OS-assigned)."""
    allowed_origins: tuple[str, ...]
    """Exact ``scheme://host[:port]`` origins; ``null`` is never allowed."""
    allow_remote: bool
    synthetic: bool
    """True when EVERY allowed origin is loopback: stamped on each stored event."""
    limits: CollectorPolicy
    bot_ua_patterns: tuple[str, ...]
    policy_version: int

    @classmethod
    def from_settings(
        cls, settings: Settings, policy: AnalyticsPolicy, *, default_origin: str | None = None
    ) -> CollectorConfig:
        """``KEN_COLLECTOR_*`` plus the policy. ``default_origin`` (the simulated site's base
        URL) is used when ``KEN_COLLECTOR_ALLOWED_ORIGINS`` is empty."""
        origins = settings.collector_origins()
        if not origins and default_origin is not None:
            origins = (parse_origin(default_origin),)
        return cls(
            host=settings.collector_host,
            allowed_origins=origins,
            allow_remote=settings.collector_allow_remote,
            synthetic=all(is_loopback_host(urlsplit(o).hostname) for o in origins),
            limits=policy.collector,
            bot_ua_patterns=policy.bot_ua_patterns,
            policy_version=policy.version,
        )


# --------------------------------------------------------------------------- #
# Collector <-> database: the live-link index (W1/W2 boundary)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LinkResolution:
    """Outcome of resolving one wire event to a live plan."""

    plan_id: UUID | None
    match_mode: MatchMode
    rejection: CollectorRejection | None = None
    """``None`` iff ``plan_id`` is set; else ``link_not_live`` | ``path_mismatch`` |
    ``unresolved_link`` | ``ambiguous_link``."""

    def __post_init__(self) -> None:
        if (self.plan_id is None) == (self.rejection is None):
            raise ValueError("a resolution has a plan id or a rejection, never both")


class LiveLinkIndex(Protocol):
    """In-memory index of currently LIVE plans (see the module rules), refreshed every
    ``collector.live_index_refresh_seconds`` from a READ ONLY session. Lookups are pure and
    synchronous (they run on the request path)."""

    def resolve_attr(self, *, plan_id: UUID, path: str) -> LinkResolution: ...

    def resolve_href(self, *, path: str, href: str, anchor_text: str) -> LinkResolution: ...

    async def refresh(self) -> None:
        """Rebuild from the database. On failure keep the previous index (never raise)."""
        ...


class LiveWindows(Protocol):
    """``ken_services.analytics.attribution.live_windows(session, plan_ids)``: one
    :class:`~ken_contracts.analytics.LiveWindow` per requested plan id (unknown or never
    deployed plans get ``deployed_at = None``), from ``link_plan_transitions`` only."""

    async def __call__(
        self, session: AsyncSession, plan_ids: Collection[UUID]
    ) -> dict[UUID, LiveWindow]: ...


# --------------------------------------------------------------------------- #
# Import / aggregation / measurement entry points (each one ``run_job(ANALYTICS_IMPORT)``)
# --------------------------------------------------------------------------- #
class ImportErrorCode(StrEnum):
    """``job_runs.failure_reason`` of a failed analytics run (fixed vocabulary)."""

    INVALID_REQUEST = "invalid_request"
    """Source/dataset/via combination not allowed, or an inverted range."""
    RANGE_TOO_LONG = "range_too_long"
    END_PAST_LAG = "end_past_lag"
    SYNTHETIC_REAL_MIX = "synthetic_real_mix"
    VIA_MISMATCH = "via_mismatch"
    ENVIRONMENT_REFUSED = "environment_refused"
    """Synthetic data in staging/production."""
    GOOGLE_DISABLED = "google_disabled"
    GOOGLE_AUTH_FAILED = "google_auth_failed"
    GOOGLE_UNAVAILABLE = "google_unavailable"
    TOKEN_FILE_INVALID = "token_file_invalid"  # noqa: S105 -- an error code, not a secret
    """Missing, unreadable, or not mode 0600."""
    FILE_TOO_LARGE = "file_too_large"
    FILE_INVALID = "file_invalid"
    TOO_MANY_ROWS = "too_many_rows"
    PROVIDER_FAILED = "provider_failed"


class AnalyticsImportError(Exception):
    """A refused or failed analytics run. The message is the code only: never row contents,
    URLs, tokens or exception text."""

    def __init__(self, code: ImportErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImportRequest:
    source: AnalyticsSource
    dataset: Dataset
    via: ImportVia
    date_range: DateRange
    source_timezone: str
    """Day boundaries: GA4 ``KEN_GA4_TIMEZONE``, GSC America/Los_Angeles, first-party UTC."""
    synthetic: bool = False
    replace_via: bool = False
    """``--replace-via``: allow overwriting rows of another ``via`` (recorded on the ledger)."""
    file_name: str | None = None
    """Basename only (CSV)."""
    file_sha256: str | None = None
    triggered_by: str = "cli"


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    job_run_id: UUID
    import_id: UUID
    counters: Mapping[str, int] = field(default_factory=dict)
    """Keys from :data:`COUNTER_NAMES`."""
    plans_measured: int = 0


class RunImport(Protocol):
    """``ken_services.analytics.importer.run_import``. Raises :class:`AnalyticsImportError`
    (the job run is then ``failed`` with that code; nothing else is written)."""

    async def __call__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: AnalyticsPolicy,
        provider: AnalyticsBatchProvider,
        request: ImportRequest,
        now: datetime | None = None,
    ) -> ImportOutcome: ...


class RunAggregation(Protocol):
    """``ken_services.analytics.aggregate.run_aggregation``: first-party ``link_events`` ->
    ``link_metrics_daily`` (``source = first_party``, ``via = collector``, ``synthetic`` carried
    from the events) for ``date_range`` (default: the trailing ``aggregation.trailing_days``
    complete UTC days), then the measure sweep. A replace import like any other."""

    async def __call__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: AnalyticsPolicy,
        date_range: DateRange | None = None,
        triggered_by: str = "cli",
        now: datetime | None = None,
    ) -> ImportOutcome: ...


@dataclass(frozen=True, slots=True)
class MeasureOutcome:
    measured: tuple[UUID, ...] = ()
    skipped_busy_sources: int = 0


class MeasureSweep(Protocol):
    """``ken_services.analytics.measure.measure_sweep``: in the CALLER's transaction, move every
    ``verified`` plan whose ``D0 + measurement_window_days`` is covered by NON-synthetic
    link-level data (first_party or ga4) to ``measured``, writing its ``link_measurements`` row
    (lock order in the module rules). Synthetic data never measures."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        policy: AnalyticsPolicy,
        now: datetime,
    ) -> MeasureOutcome: ...


def validate_range(date_range: DateRange) -> DateRange:
    """Raise :class:`AnalyticsImportError` unless ``date_range`` is ordered and spans at most
    :data:`~ken_contracts.analytics.MAX_RANGE_DAYS` days (inclusive)."""
    start, end = date_range.start, date_range.end
    if end < start:
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    if (end - start).days >= MAX_RANGE_DAYS:
        raise AnalyticsImportError(ImportErrorCode.RANGE_TOO_LONG)
    return date_range
