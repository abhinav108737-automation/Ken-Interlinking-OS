"""``CsvAnalyticsProvider``: the TWO canonical CSV formats (ADR-0015 §12.2, §12.8 -- binding).

There are exactly two, both UTF-8 with a required header, ``YYYY-MM-DD`` dates only, and no
other column::

    ken_link_daily   date,source,link_id,impressions,clicks
    ken_page_daily   date,source,url,impressions,clicks,position,pageviews,sessions

The file's own header decides which dataset it carries; asking for the other one is refused
(``invalid_request``). ``source`` is the row's ORIGIN (``ga4`` / ``search_console`` /
``first_party``) -- never ``csv``, which is only a transport. The importer additionally
refuses rows whose origin is not the one the run declared.

Bounds and hygiene:

* at most ``KEN_ANALYTICS_IMPORT_MAX_FILE_BYTES`` (``file_too_large``) and at most
  ``imports.max_rows`` data rows (``too_many_rows``);
* a missing, unknown, reordered or extended header is ``file_invalid``;
* a malformed data row is COUNTED (``rejected_invalid``) and never echoed -- not in a log,
  not in an error, not in the ledger;
* the sha256 of the exact bytes read and the file's BASENAME are recorded on the ledger.
"""

from __future__ import annotations

import csv as csv_module
import hashlib
import io
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Final

from ken_contracts.analytics import (
    LINK_SOURCES,
    MAX_FILE_NAME_CHARS,
    PAGE_SOURCES,
    AnalyticsBatch,
    AnalyticsSource,
    Dataset,
    DateRange,
    LinkMetrics,
    MetricSource,
    PageMetrics,
    PageUrlResolver,
)
from ken_core.providers.analytics.base import (
    AnalyticsFetchError,
    BaseAnalyticsProvider,
    FetchErrorCode,
    StatsBuilder,
)

__all__ = [
    "LINK_HEADER",
    "PAGE_HEADER",
    "PROVIDER_NAME",
    "CsvAnalyticsProvider",
    "CsvFile",
    "read_csv_file",
]

PROVIDER_NAME: Final = "csv"
LINK_HEADER: Final = ("date", "source", "link_id", "impressions", "clicks")
PAGE_HEADER: Final = (
    "date",
    "source",
    "url",
    "impressions",
    "clicks",
    "position",
    "pageviews",
    "sessions",
)
_HEADERS: Final = {LINK_HEADER: Dataset.LINKS, PAGE_HEADER: Dataset.PAGES}
_MAX_FIELD_CHARS: Final = 4096
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


class CsvFile:
    """The bytes of one import file, with its basename and sha256."""

    __slots__ = ("file_name", "file_sha256", "text")

    def __init__(self, *, file_name: str, file_sha256: str, text: str) -> None:
        self.file_name = file_name
        self.file_sha256 = file_sha256
        self.text = text


def read_csv_file(path: str | Path, *, max_bytes: int) -> CsvFile:
    """Read and hash one file. Raises :class:`AnalyticsFetchError` -- never the path."""
    target = Path(path)
    try:
        info = target.stat()
        if not target.is_file():
            raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID)
        if info.st_size > max_bytes:
            raise AnalyticsFetchError(FetchErrorCode.FILE_TOO_LARGE)
        raw = target.read_bytes()
    except AnalyticsFetchError:
        raise
    except OSError:
        raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID) from None
    if len(raw) > max_bytes:
        raise AnalyticsFetchError(FetchErrorCode.FILE_TOO_LARGE)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID) from None
    if "\x00" in text:
        raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID)
    name = target.name
    if not name or len(name) > MAX_FILE_NAME_CHARS:
        raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID)
    return CsvFile(file_name=name, file_sha256=digest, text=text)


def _parse_date(value: str) -> date | None:
    """``YYYY-MM-DD`` and nothing else (§12.2: ISO dates only).

    The pattern is checked first because ``strptime`` would also accept ``2026-5-11`` and
    ``date.fromisoformat`` would also accept ``20260511``; one import format means one.
    """
    if _ISO_DATE.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_count(value: str) -> int | None:
    stripped = value.strip()
    if not stripped or not stripped.isdigit():
        return None
    try:
        count = int(stripped)
    except ValueError:  # pragma: no cover - isdigit() already guarantees it
        return None
    return count


def _parse_position(value: str) -> tuple[bool, float | None]:
    """``(ok, position)``: a blank cell is a valid ``None``, anything below 1.0 is malformed."""
    stripped = value.strip()
    if not stripped:
        return True, None
    try:
        position = float(stripped)
    except ValueError:
        return False, None
    if not math.isfinite(position) or position < 1.0:
        return False, None
    return True, position


class CsvAnalyticsProvider(BaseAnalyticsProvider):
    """One canonical CSV file as an :class:`~ken_contracts.analytics.AnalyticsBatch`."""

    name: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        path: str | Path,
        resolver: PageUrlResolver,
        max_file_bytes: int,
        max_rows: int,
    ) -> None:
        self._path = path
        self._resolver = resolver
        self._max_file_bytes = max_file_bytes
        self._max_rows = max_rows
        self._file: CsvFile | None = None

    # ------------------------------------------------------------ metadata
    @property
    def file_name(self) -> str | None:
        return None if self._file is None else self._file.file_name

    @property
    def file_sha256(self) -> str | None:
        return None if self._file is None else self._file.file_sha256

    def load(self) -> CsvFile:
        """Read and hash the file once (also what fills :attr:`file_sha256`)."""
        if self._file is None:
            self._file = read_csv_file(self._path, max_bytes=self._max_file_bytes)
        return self._file

    # --------------------------------------------------------------- fetch
    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch:
        del date_range  # the importer rejects out-of-range rows; a file is authoritative
        loaded = self.load()
        rows = csv_module.reader(io.StringIO(loaded.text, newline=""))
        try:
            header = next(rows)
        except StopIteration:
            raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID) from None
        dataset = _HEADERS.get(tuple(column.strip().lower() for column in header))
        if dataset is None:
            raise AnalyticsFetchError(FetchErrorCode.FILE_INVALID)
        if dataset not in datasets:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)
        stats = StatsBuilder()
        link_rows: list[LinkMetrics] = []
        page_rows: list[PageMetrics] = []
        for record in rows:
            if not record or all(not field.strip() for field in record):
                continue
            stats.rows_read += 1
            if stats.rows_read > self._max_rows:
                raise AnalyticsFetchError(FetchErrorCode.TOO_MANY_ROWS)
            if any(len(field) > _MAX_FIELD_CHARS for field in record):
                stats.rejected_invalid += 1
                continue
            if dataset is Dataset.LINKS:
                parsed_link = self._link_row(record, stats)
                if parsed_link is not None:
                    link_rows.append(parsed_link)
            else:
                parsed_page = self._page_row(record, stats)
                if parsed_page is not None:
                    page_rows.append(parsed_page)
        return AnalyticsBatch(
            link_rows=tuple(link_rows), page_rows=tuple(page_rows), stats=stats.build()
        )

    # ----------------------------------------------------------- row types
    def _link_row(self, record: list[str], stats: StatsBuilder) -> LinkMetrics | None:
        if len(record) != len(LINK_HEADER):
            stats.rejected_invalid += 1
            return None
        day = _parse_date(record[0].strip())
        source = _source_of(record[1], LINK_SOURCES)
        impressions = _parse_count(record[3])
        clicks = _parse_count(record[4])
        link_id = record[2].strip()
        if day is None or source is None or impressions is None or clicks is None or not link_id:
            stats.rejected_invalid += 1
            return None
        return LinkMetrics(
            link_id=link_id,
            source=MetricSource(source.value),
            range=DateRange(start=day, end=day),
            impressions=impressions,
            clicks=clicks,
        )

    def _page_row(self, record: list[str], stats: StatsBuilder) -> PageMetrics | None:
        if len(record) != len(PAGE_HEADER):
            stats.rejected_invalid += 1
            return None
        day = _parse_date(record[0].strip())
        source = _source_of(record[1], PAGE_SOURCES)
        impressions = _parse_count(record[3])
        clicks = _parse_count(record[4])
        position_ok, position = _parse_position(record[5])
        pageviews = _parse_count(record[6])
        sessions = _parse_count(record[7])
        url = record[2].strip()
        if (
            day is None
            or source is None
            or impressions is None
            or clicks is None
            or not position_ok
            or pageviews is None
            or sessions is None
            or not url
        ):
            stats.rejected_invalid += 1
            return None
        page_id = stats.resolve(self._resolver, url)
        if page_id is None:
            return None
        return PageMetrics(
            page_id=page_id,
            source=MetricSource(source.value),
            range=DateRange(start=day, end=day),
            impressions=impressions,
            clicks=clicks,
            sessions=sessions,
            pageviews=pageviews,
            position=position,
        )


def _source_of(value: str, allowed: frozenset[AnalyticsSource]) -> AnalyticsSource | None:
    try:
        source = AnalyticsSource(value.strip().lower())
    except ValueError:
        return None
    return source if source in allowed else None
