"""``run_import``: one replace-import, one transaction, one ledger row (ADR-0015 §2 "Replace
semantics" as amended by §12.6 and §12.9.1). Implements the frozen
:class:`~ken_services.analytics.protocol.RunImport`.

An import of ``(source, dataset, [start, end])`` is AUTHORITATIVE for every key on those
days. Everything below happens in ONE transaction, in this order:

1. ``set_config('ken.analytics_writer', 'on', true)`` -- transaction-local, so the DELETE
   right is never carried to the next transaction on a pooled connection;
2. refuse ``synthetic_real_mix`` (rows of the opposite ``synthetic`` flag in the range) and
   ``via_mismatch`` (rows of another ``via``), the latter only overridable by the explicit
   ``--replace-via``, which is RECORDED on the ledger (§12.6.8);
3. DELETE the (source, dataset, range) rows;
4. INSERT the ledger row -- physically before the metric rows, because they reference it by
   foreign key; it is written once, complete and immutable (§12.6.6), so ``rows_written`` is
   computed first;
5. INSERT the metric rows sorted by natural key, duplicate keys SUMMED (``merged_rows``);
6. run the measure sweep (link datasets, non-synthetic only), inside this transaction so
   ``plans_measured`` in the event is exact (§12.9.1);
7. emit ``analytics.updated``;
8. COMMIT.

Re-importing the same input therefore gives byte-identical rows.

**What a row must satisfy** (counted, never echoed):

* daily (``range.start == range.end``), else ``non_daily_row``;
* inside the requested range, else ``outside_range``;
* the row's own origin is the run's source, else ``rejected_invalid``;
* a link row names a known plan, else ``unresolved_link``; a page row a known page, else
  ``unresolved_url``;
* the day lies in the plan's live window ``[D(deployed_at), D(rolled_back_at)]`` (inclusive,
  source-local), else ``outside_live_window`` -- so a superseded, rejected or never-applied
  plan can never accumulate metrics.

**An import that would write nothing is refused** (``file_invalid``) when it read rows at
all: a replace import deletes before it inserts, so an input whose every row is dropped
would otherwise clear the range and store nothing. A genuinely EMPTY source
(``rows_read == 0``) still clears the range -- that is the deliberate way to do it.

**Refusals** raise :class:`~ken_services.analytics.protocol.AnalyticsImportError`; the job
run is then ``failed`` with that fixed code and NOTHING else is written (the refusal above
happens before the write transaction is even opened). End dates past the source lag are
refused, so a partial day is never stored.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    ANALYTICS_EMITTED_BY,
    LINK_SOURCES,
    PAGE_SOURCES,
    SUBJECT_ANALYTICS_IMPORT,
    AnalyticsBatch,
    AnalyticsBatchProvider,
    AnalyticsPolicy,
    AnalyticsSource,
    AnalyticsUpdatedPayload,
    Dataset,
    DateRange,
    ImportVia,
    LinkMetrics,
    LiveWindow,
    PageMetrics,
)
from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_core.config import Settings
from ken_core.db.repositories import analytics as repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_core.providers.analytics.base import AnalyticsFetchError, FetchErrorCode
from ken_services.analytics.attribution import live_windows
from ken_services.analytics.measure import measure_sweep
from ken_services.analytics.protocol import (
    COUNTER_NAMES,
    AnalyticsImportError,
    ImportErrorCode,
    ImportOutcome,
    ImportRequest,
    validate_range,
)
from ken_services.analytics.windows import day_of, source_timezone
from ken_services.jobs.runner import JobContext, JobFailedError, run_job, utc_now

__all__ = [
    "SNAPSHOT_MODES",
    "PreparedRows",
    "prepare_rows",
    "run_import",
    "source_timezone_of",
    "trailing_range",
    "utc_day",
]

logger = get_logger(__name__)

_FETCH_TO_IMPORT: Final[Mapping[FetchErrorCode, ImportErrorCode]] = {
    FetchErrorCode.INVALID_REQUEST: ImportErrorCode.INVALID_REQUEST,
    FetchErrorCode.GOOGLE_DISABLED: ImportErrorCode.GOOGLE_DISABLED,
    FetchErrorCode.GOOGLE_AUTH_FAILED: ImportErrorCode.GOOGLE_AUTH_FAILED,
    FetchErrorCode.GOOGLE_UNAVAILABLE: ImportErrorCode.GOOGLE_UNAVAILABLE,
    FetchErrorCode.TOKEN_FILE_INVALID: ImportErrorCode.TOKEN_FILE_INVALID,
    FetchErrorCode.FILE_TOO_LARGE: ImportErrorCode.FILE_TOO_LARGE,
    FetchErrorCode.FILE_INVALID: ImportErrorCode.FILE_INVALID,
    FetchErrorCode.TOO_MANY_ROWS: ImportErrorCode.TOO_MANY_ROWS,
    FetchErrorCode.PROVIDER_FAILED: ImportErrorCode.PROVIDER_FAILED,
}
"""``ken_core`` may not import the service vocabulary, so a provider's code is mapped here."""

SNAPSHOT_MODES: Final[Mapping[ImportVia, str]] = {
    ImportVia.COLLECTOR: "aggregate",
    ImportVia.SYNTHETIC: "synthetic",
}
"""``config_snapshot.mode`` for the runs that are not a plain import (ADR-0015 §5)."""


# --------------------------------------------------------------------------- #
# Row preparation (pure, given the live windows and the known ids)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PreparedRows:
    """What the importer will write, plus the counters explaining what it dropped."""

    link_rows: list[repo.LinkRow] = field(default_factory=list)
    page_rows: list[repo.PageRow] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTER_NAMES, 0))

    def count(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by

    @property
    def links_affected(self) -> int:
        return len({row.plan_id for row in self.link_rows})

    @property
    def pages_affected(self) -> int:
        return len({row.page_id for row in self.page_rows})


def _live_days(window: LiveWindow, *, timezone: str) -> tuple[date, date | None] | None:
    """``[D(deployed_at), D(rolled_back_at)]`` inclusive, or ``None`` if never live."""
    if window.deployed_at is None:
        return None
    start = day_of(window.deployed_at, timezone)
    end = None if window.rolled_back_at is None else day_of(window.rolled_back_at, timezone)
    return start, end


def _daily_day(
    item: LinkMetrics | PageMetrics, *, date_range: DateRange, prepared: PreparedRows
) -> date | None:
    if item.range.start != item.range.end:
        prepared.count("non_daily_row")
        return None
    day = item.range.start
    if day < date_range.start or day > date_range.end:
        prepared.count("outside_range")
        return None
    return day


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def prepare_rows(
    batch: AnalyticsBatch,
    *,
    request: ImportRequest,
    known_plans: set[uuid.UUID],
    known_pages: set[uuid.UUID],
    windows: Mapping[uuid.UUID, LiveWindow],
) -> PreparedRows:
    """Validate, drop and MERGE provider rows into what the database will hold.

    Duplicate natural keys inside one import are summed (``merged_rows``) rather than
    fighting over the primary key, and the result is ordered by natural key so a re-import
    writes exactly the same bytes.
    """
    prepared = PreparedRows()
    prepared.counters["rows_read"] = batch.stats.rows_read
    prepared.counters["unresolved_url"] = batch.stats.unresolved_url
    prepared.counters["unresolved_link"] = batch.stats.unresolved_link
    prepared.counters["ambiguous_url"] = batch.stats.ambiguous_url
    prepared.counters["other_row"] = batch.stats.other_row
    prepared.counters["rejected_invalid"] = batch.stats.rejected_invalid
    timezone = request.source_timezone
    if request.dataset is Dataset.LINKS:
        merged: dict[tuple[str, date, uuid.UUID], list[int]] = {}
        for row in batch.link_rows:
            day = _daily_day(row, date_range=request.date_range, prepared=prepared)
            if day is None:
                continue
            if row.source.value != request.source.value:
                prepared.count("rejected_invalid")
                continue
            plan_id = _uuid_or_none(row.link_id)
            if plan_id is None or plan_id not in known_plans:
                prepared.count("unresolved_link")
                continue
            live = _live_days(windows.get(plan_id, LiveWindow(plan_id=plan_id)), timezone=timezone)
            if live is None or day < live[0] or (live[1] is not None and day > live[1]):
                prepared.count("outside_live_window")
                continue
            key = (request.source.value, day, plan_id)
            if key in merged:
                prepared.count("merged_rows")
                merged[key][0] += row.impressions
                merged[key][1] += row.clicks
            else:
                merged[key] = [row.impressions, row.clicks]
        prepared.link_rows = [
            repo.LinkRow(
                source=request.source,
                metric_date=day,
                plan_id=plan_id,
                impressions=counts[0],
                clicks=counts[1],
            )
            for (_source, day, plan_id), counts in sorted(merged.items(), key=lambda i: i[0])
        ]
        return prepared

    merged_pages: dict[tuple[str, date, uuid.UUID], list[float]] = {}
    for page_row in batch.page_rows:
        day = _daily_day(page_row, date_range=request.date_range, prepared=prepared)
        if day is None:
            continue
        if page_row.source.value != request.source.value:
            prepared.count("rejected_invalid")
            continue
        page_id = _uuid_or_none(page_row.page_id)
        if page_id is None or page_id not in known_pages:
            prepared.count("unresolved_url")
            continue
        # position_sum = position x impressions; with no impressions the CHECK demands 0.
        position_sum = (
            0.0
            if page_row.position is None or page_row.impressions == 0
            else page_row.position * page_row.impressions
        )
        key = (request.source.value, day, page_id)
        values = [
            float(page_row.impressions),
            float(page_row.clicks),
            position_sum,
            float(page_row.pageviews),
            float(page_row.sessions),
        ]
        if key in merged_pages:
            prepared.count("merged_rows")
            existing = merged_pages[key]
            merged_pages[key] = [a + b for a, b in zip(existing, values, strict=True)]
        else:
            merged_pages[key] = values
    prepared.page_rows = [
        repo.PageRow(
            source=request.source,
            metric_date=day,
            page_id=page_id,
            impressions=int(values[0]),
            clicks=int(values[1]),
            position_sum=values[2] if int(values[0]) > 0 else 0.0,
            pageviews=int(values[3]),
            sessions=int(values[4]),
        )
        for (_source, day, page_id), values in sorted(merged_pages.items(), key=lambda i: i[0])
    ]
    return prepared


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate_request(
    request: ImportRequest, *, settings: Settings, policy: AnalyticsPolicy, now: datetime
) -> None:
    """Every refusal that needs no I/O (mirrors the 0008 ledger CHECKs plus the lag rule)."""
    validate_range(request.date_range)
    sources = LINK_SOURCES if request.dataset is Dataset.LINKS else PAGE_SOURCES
    if request.source not in sources:
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    if request.via is ImportVia.COLLECTOR and (
        request.source is not AnalyticsSource.FIRST_PARTY or request.dataset is not Dataset.LINKS
    ):
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    if request.via is ImportVia.API and request.source is AnalyticsSource.FIRST_PARTY:
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    if request.via is ImportVia.SYNTHETIC and not request.synthetic:
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    if request.synthetic and settings.environment not in {"local", "ci"}:
        raise AnalyticsImportError(ImportErrorCode.ENVIRONMENT_REFUSED)
    if (request.file_name is None) != (request.file_sha256 is None):
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    if request.via is not ImportVia.CSV and request.file_sha256 is not None:
        # The ledger CHECK is ``(via = 'csv') = (file_sha256 IS NOT NULL)``; refusing here
        # keeps it a typed refusal instead of an integrity error with a generic reason.
        raise AnalyticsImportError(ImportErrorCode.INVALID_REQUEST)
    newest = day_of(now, request.source_timezone) - timedelta(days=policy.lag_days(request.source))
    if request.date_range.end > newest:
        raise AnalyticsImportError(ImportErrorCode.END_PAST_LAG)


def _refuse_destructive_replace(
    prepared: PreparedRows, *, rows_written: int, request: ImportRequest
) -> None:
    """Refuse a replace import that read rows but would write none.

    A replace import DELETEs its (source, dataset, range) rows before it inserts, so an
    import that writes nothing is purely destructive. Two cases, deliberately different:

    * **every row was dropped** (``rows_read > 0`` and ``rows_written == 0``): the input was
      unusable -- a wrong origin column, unknown ids, days outside the range or outside every
      live window. Refusing here, BEFORE the write transaction is opened, is what keeps the
      rows already stored for those days (QA finding, Phase 7). The code is
      :attr:`~ken_services.analytics.protocol.ImportErrorCode.FILE_INVALID`: the source data
      as a whole was invalid, not the request.
    * **the source really was empty** (``rows_read == 0``): that is the documented, deliberate
      way to CLEAR a range, and it proceeds (logged, so it is never silent).
    """
    if rows_written > 0:
        return
    if prepared.counters["rows_read"] > 0:
        logger.warning(
            "analytics_import_refused_every_row_dropped",
            source=request.source.value,
            dataset=request.dataset.value,
            via=request.via.value,
            rows_read=prepared.counters["rows_read"],
            date_start=request.date_range.start.isoformat(),
            date_end=request.date_range.end.isoformat(),
        )
        raise AnalyticsImportError(ImportErrorCode.FILE_INVALID)
    logger.info(
        "analytics_import_clears_range",
        source=request.source.value,
        dataset=request.dataset.value,
        via=request.via.value,
        date_start=request.date_range.start.isoformat(),
        date_end=request.date_range.end.isoformat(),
    )


def _check_mix(origins: set[tuple[bool, str]], *, request: ImportRequest) -> None:
    """The replace mix check: ``synthetic`` AND ``via`` (§12.6.8)."""
    for synthetic, via in sorted(origins):
        if synthetic != request.synthetic:
            raise AnalyticsImportError(ImportErrorCode.SYNTHETIC_REAL_MIX)
        if via != request.via.value and not request.replace_via:
            raise AnalyticsImportError(ImportErrorCode.VIA_MISMATCH)


def _snapshot(
    request: ImportRequest, *, provider_name: str, policy: AnalyticsPolicy
) -> dict[str, Any]:
    """The job's ``config_snapshot``: SECRET-FREE (no token path, no database URL, no URL).

    ``mode`` is derived from ``via`` (ADR-0015 §5: ``import | aggregate | synthetic``), so the
    frozen ``RunImport`` signature needs no extra argument to label an aggregation run.
    """
    return {
        "mode": SNAPSHOT_MODES.get(request.via, "import"),
        "provider": provider_name,
        "source": request.source.value,
        "dataset": request.dataset.value,
        "via": request.via.value,
        "synthetic": request.synthetic,
        "replace_via": request.replace_via,
        "date_start": request.date_range.start.isoformat(),
        "date_end": request.date_range.end.isoformat(),
        "source_timezone": request.source_timezone,
        "analytics_policy_version": policy.version,
        "file_name": request.file_name,
    }


def _file_identity(request: ImportRequest, provider: AnalyticsBatchProvider) -> ImportRequest:
    """A CSV run records the file's BASENAME and sha256; the provider computed both."""
    if request.via is not ImportVia.CSV or request.file_sha256 is not None:
        return request
    name = getattr(provider, "file_name", None)
    digest = getattr(provider, "file_sha256", None)
    if not isinstance(name, str) or not isinstance(digest, str):
        raise AnalyticsImportError(ImportErrorCode.FILE_INVALID)
    return replace(request, file_name=name, file_sha256=digest)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run_import(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: AnalyticsPolicy,
    provider: AnalyticsBatchProvider,
    request: ImportRequest,
    now: datetime | None = None,
) -> ImportOutcome:
    """Run one import as a ``run_job(ANALYTICS_IMPORT)`` and return its outcome.

    Raises:
        AnalyticsImportError: the run was refused or failed; the job run holds the code.
        JobAlreadyRunningError: another analytics run holds the kind lock.
    """
    moment = now or utc_now()
    failure: dict[str, ImportErrorCode] = {}
    result: dict[str, Any] = {}

    async def body(job: JobContext) -> RunStatus:
        try:
            await _import_once(
                job,
                sessionmaker=sessionmaker,
                settings=settings,
                policy=policy,
                provider=provider,
                request=request,
                now=moment,
                result=result,
            )
        except AnalyticsImportError as exc:
            failure["code"] = exc.code
            raise JobFailedError(exc.code.value) from None
        return RunStatus.SUCCEEDED

    detail = await run_job(
        kind=JobKind.ANALYTICS_IMPORT,
        sessionmaker=sessionmaker,
        triggered_by=request.triggered_by,
        body=body,
        config_snapshot=_snapshot(request, provider_name=provider.name, policy=policy),
        counter_names=sorted(COUNTER_NAMES),
        clock=lambda: moment,
    )
    if detail.status is not RunStatus.SUCCEEDED:
        raise AnalyticsImportError(failure.get("code", ImportErrorCode.PROVIDER_FAILED))
    return ImportOutcome(
        job_run_id=detail.id,
        import_id=result["import_id"],
        counters=detail.counters,
        plans_measured=detail.counters.get("plans_measured", 0),
    )


async def _import_once(
    job: JobContext,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: AnalyticsPolicy,
    provider: AnalyticsBatchProvider,
    request: ImportRequest,
    now: datetime,
    result: dict[str, Any],
) -> None:
    _validate_request(request, settings=settings, policy=policy, now=now)
    try:
        batch = await provider.fetch_batch(
            date_range=request.date_range, datasets=frozenset({request.dataset})
        )
    except AnalyticsFetchError as exc:
        raise AnalyticsImportError(
            _FETCH_TO_IMPORT.get(exc.code, ImportErrorCode.PROVIDER_FAILED)
        ) from None
    except Exception as exc:
        # Never the message: a provider's exception text can carry a URL or a file path.
        logger.error("analytics_provider_failed", error_class=type(exc).__name__)
        raise AnalyticsImportError(ImportErrorCode.PROVIDER_FAILED) from None
    if batch.stats.rows_read > policy.imports.max_rows:
        raise AnalyticsImportError(ImportErrorCode.TOO_MANY_ROWS)
    stamped = _file_identity(request, provider)

    plan_ids = {
        parsed
        for parsed in (_uuid_or_none(row.link_id) for row in batch.link_rows)
        if parsed is not None
    }
    page_ids = {
        parsed
        for parsed in (_uuid_or_none(row.page_id) for row in batch.page_rows)
        if parsed is not None
    }
    async with sessionmaker() as reading:
        known_plans = await repo.plan_ids_present(reading, plan_ids)
        known_pages = await repo.page_ids_present(reading, page_ids)
        windows = await live_windows(reading, known_plans)
    prepared = prepare_rows(
        batch,
        request=stamped,
        known_plans=known_plans,
        known_pages=known_pages,
        windows=windows,
    )
    rows_written = (
        len(prepared.link_rows) if stamped.dataset is Dataset.LINKS else len(prepared.page_rows)
    )
    _refuse_destructive_replace(prepared, rows_written=rows_written, request=stamped)

    import_id = uuid.uuid4()
    async with job.transaction() as tx:
        session = tx.session
        await repo.enable_writer(session)
        if stamped.dataset is Dataset.LINKS:
            origins = await repo.existing_link_origins(
                session,
                source=stamped.source,
                start=stamped.date_range.start,
                end=stamped.date_range.end,
            )
        else:
            origins = await repo.existing_page_origins(
                session,
                source=stamped.source,
                start=stamped.date_range.start,
                end=stamped.date_range.end,
            )
        _check_mix(origins, request=stamped)
        if stamped.dataset is Dataset.LINKS:
            await repo.replace_delete_links(
                session,
                source=stamped.source,
                start=stamped.date_range.start,
                end=stamped.date_range.end,
            )
        else:
            await repo.replace_delete_pages(
                session,
                source=stamped.source,
                start=stamped.date_range.start,
                end=stamped.date_range.end,
            )
        await repo.insert_import(
            session,
            id=import_id,
            job_run_id=job.run_id,
            source=stamped.source.value,
            dataset=stamped.dataset.value,
            via=stamped.via.value,
            synthetic=stamped.synthetic,
            replace_via=stamped.replace_via,
            date_start=stamped.date_range.start,
            date_end=stamped.date_range.end,
            source_timezone=stamped.source_timezone,
            file_name=stamped.file_name,
            file_sha256=stamped.file_sha256,
            sampled=batch.stats.sampled,
            rows_written=rows_written,
            analytics_policy_version=policy.version,
            unresolved_sample=list(
                batch.stats.unresolved_sample[: policy.imports.max_unresolved_sample]
            ),
            triggered_by=stamped.triggered_by,
            created_at=now,
        )
        if stamped.dataset is Dataset.LINKS:
            await repo.insert_link_rows(
                session, prepared.link_rows, import_id=import_id, synthetic=stamped.synthetic
            )
        else:
            await repo.insert_page_rows(
                session, prepared.page_rows, import_id=import_id, synthetic=stamped.synthetic
            )
        prepared.counters["rows_written"] = rows_written
        if stamped.dataset is Dataset.LINKS and not stamped.synthetic:
            outcome = await measure_sweep(session, settings=settings, policy=policy, now=now)
            prepared.counters["plans_measured"] = len(outcome.measured)
            prepared.counters["plans_skipped_busy"] = outcome.skipped_busy_sources
        payload = AnalyticsUpdatedPayload(
            import_id=import_id,
            job_run_id=job.run_id,
            source=stamped.source,
            dataset=stamped.dataset,
            via=stamped.via,
            synthetic=stamped.synthetic,
            date_start=stamped.date_range.start,
            date_end=stamped.date_range.end,
            rows_written=rows_written,
            rows_unresolved=(
                prepared.counters["unresolved_url"]
                + prepared.counters["unresolved_link"]
                + prepared.counters["ambiguous_url"]
            ),
            rows_rejected=(
                prepared.counters["rejected_invalid"]
                + prepared.counters["non_daily_row"]
                + prepared.counters["outside_range"]
                + prepared.counters["outside_live_window"]
                + prepared.counters["other_row"]
            ),
            links_affected=prepared.links_affected,
            pages_affected=prepared.pages_affected,
            plans_measured=prepared.counters["plans_measured"],
        )
        await append_event(
            session,
            EventEnvelope(
                event_name=EventName.ANALYTICS_UPDATED,
                event_version=1,
                occurred_at=now,
                emitted_by=ANALYTICS_EMITTED_BY,
                subject_type=SUBJECT_ANALYTICS_IMPORT,
                subject_id=str(import_id),
                payload=payload.model_dump(mode="json"),
                correlation_id=job.run_id,
            ),
        )
        for name, value in prepared.counters.items():
            if value:
                tx.increment(name, value)
    result["import_id"] = import_id
    logger.info(
        "analytics_import_succeeded",
        import_id=str(import_id),
        source=stamped.source.value,
        dataset=stamped.dataset.value,
        via=stamped.via.value,
        rows_written=rows_written,
    )


def utc_day(moment: datetime) -> date:
    """The UTC calendar day of ``moment`` (first-party day boundary)."""
    return moment.astimezone(UTC).date()


def trailing_range(now: datetime, *, days: int) -> DateRange:
    """The last ``days`` COMPLETE UTC days, newest first excluded (today is partial)."""
    end = utc_day(now) - timedelta(days=1)
    return DateRange(start=end - timedelta(days=max(days, 1) - 1), end=end)


def source_timezone_of(source: AnalyticsSource, settings: Settings) -> str:
    """Re-exported for the CLI: the zone whose midnight separates ``source``'s days."""
    return source_timezone(source, settings)
