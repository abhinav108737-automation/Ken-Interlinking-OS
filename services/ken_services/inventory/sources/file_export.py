"""``FileExportContentSource``: the documented JSONL export format (ADR-0009 section 1).

Format (``docs/CONTRACTS.md``, ``ken_contracts.inventory``):

* line 1 -- one :class:`~ken_contracts.inventory.ExportHeader` JSON object;
* every following non-blank line -- one :class:`~ken_contracts.inventory.ExportPageRecord`.

A bad record never fails the run: it becomes a :class:`SourceIssue`. Only a
problem with the file as a whole (missing, larger than ``max_bytes``, not
UTF-8, invalid header) raises :class:`ExportFormatError`, from
:meth:`FileExportContentSource.site_scope`, before any record is processed.

Issue messages are SAFE: they name the line number and the kind of problem,
never a field value (an export may carry anything, including credentials in a
URL). The file is read line by line; it is never slurped into memory whole.

The file is local and bounded by ``max_bytes``, so reads are short blocking
calls made from small synchronous helpers.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator, Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Final

from pydantic import ValidationError

from ken_contracts.inventory import (
    EXPORT_FORMAT_VERSION,
    ExportHeader,
    ExportPageRecord,
    IssueCode,
    IssueSeverity,
    SiteScope,
    SourceAssertions,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
)
from ken_services.inventory.urls import (
    InvalidUrlError,
    build_scope,
    in_scope,
    normalize_netloc,
    try_normalize_url,
)

__all__ = ["ExportFormatError", "FileExportContentSource"]

_CHUNK: Final = 64 * 1024
_MESSAGE_MAX: Final = 500
_BOM: Final = "﻿"


class ExportFormatError(ValueError):
    """The export file as a whole is unusable. The message is safe to persist."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _error_types(exc: ValidationError) -> str:
    """Sorted, de-duplicated pydantic error types: never the offending values."""
    return ", ".join(sorted({str(error["type"]) for error in exc.errors()}))


def _line_message(line_no: int, text: str) -> str:
    return f"line {line_no}: {text}"[:_MESSAGE_MAX]


class FileExportContentSource:
    """Read-only :class:`~ken_contracts.inventory.ContentSourceProvider` over a JSONL export."""

    name: str = "file_export"
    source_type: SourceType = SourceType.FILE_EXPORT

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._clock = clock
        self._header: ExportHeader | None = None
        self._scope: SiteScope | None = None
        self._discovered = 0
        self._duplicates = 0
        self._out_of_scope = 0
        self._handle: BinaryIO | None = None
        self._closed = False

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    def config_snapshot(self) -> dict[str, Any]:
        """Secret-free: the file NAME only, and URLs only in normalised form."""
        header = self._header
        if header is None:
            try:
                header = self._load_header()
            except ExportFormatError:
                header = None
        site_url: str | None = None
        allowed_hosts: list[str] = []
        if header is not None:
            # try_normalize_url refuses credentials, so none can reach the snapshot.
            site_url = try_normalize_url(header.site_url)
            for host in header.allowed_hosts:
                try:
                    allowed_hosts.append(normalize_netloc(host))
                except InvalidUrlError:
                    continue
        return {
            "path": self._path.name,
            "format_version": EXPORT_FORMAT_VERSION,
            "site_url": site_url,
            "allowed_hosts": sorted(set(allowed_hosts)),
            "max_bytes": self._max_bytes,
        }

    async def site_scope(self) -> SiteScope:
        if self._scope is None:
            header = self._load_header()
            self._check_utf8()
            try:
                self._scope = build_scope(header.site_url, header.allowed_hosts)
            except (InvalidUrlError, ValidationError, ValueError):
                raise ExportFormatError(
                    "export header site_url or allowed_hosts is not a valid http(s) site"
                ) from None
        return self._scope

    # ------------------------------------------------------------------ #
    # File checks (synchronous, bounded)
    # ------------------------------------------------------------------ #
    def _check_file(self) -> None:
        if not self._path.is_file():
            raise ExportFormatError("export file not found")
        if self._path.stat().st_size > self._max_bytes:
            raise ExportFormatError(f"export file is larger than {self._max_bytes} bytes")

    def _load_header(self) -> ExportHeader:
        if self._header is not None:
            return self._header
        self._check_file()
        with self._path.open("rb") as handle:
            raw = handle.readline(self._max_bytes + 1)
        try:
            text = raw.decode("utf-8").removeprefix(_BOM).strip()
        except UnicodeDecodeError:
            raise ExportFormatError("export file is not valid UTF-8") from None
        if not text:
            raise ExportFormatError("export file has no header line")
        try:
            data = json.loads(text)
        except ValueError:
            raise ExportFormatError("export header is not valid JSON") from None
        try:
            self._header = ExportHeader.model_validate(data)
        except ValidationError as exc:
            raise ExportFormatError(f"export header is invalid: {_error_types(exc)}") from None
        return self._header

    def _check_utf8(self) -> None:
        """Validate the whole file as UTF-8 in bounded chunks."""
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with self._path.open("rb") as handle:
                while chunk := handle.read(_CHUNK):
                    decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            raise ExportFormatError("export file is not valid UTF-8") from None

    def _lines(self) -> Generator[tuple[int, bytes]]:
        """``(line number, raw bytes)`` for every line after the header."""
        handle = self._path.open("rb")
        self._handle = handle
        try:
            handle.readline(self._max_bytes + 1)  # header, already validated
            line_no = 1
            while raw := handle.readline(self._max_bytes + 1):
                line_no += 1
                yield line_no, raw
        finally:
            handle.close()
            self._handle = None

    # ------------------------------------------------------------------ #
    # Records
    # ------------------------------------------------------------------ #
    def _issue(
        self,
        code: IssueCode,
        severity: IssueSeverity,
        message: str,
        *,
        url: str | None = None,
        related_url: str | None = None,
    ) -> SourceIssue:
        return SourceIssue(
            code=code,
            severity=severity,
            url=url,
            related_url=related_url,
            message=message[:_MESSAGE_MAX],
            occurred_at=self._clock(),
        )

    def _record(
        self, line_no: int, raw: bytes, scope: SiteScope, seen: dict[str, int]
    ) -> SourceDocument | SourceIssue | None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._discovered += 1
            return self._issue(
                IssueCode.INVALID_RECORD,
                IssueSeverity.ERROR,
                _line_message(line_no, "not valid UTF-8"),
            )
        if not text.strip():
            return None  # blank lines are ignored
        self._discovered += 1

        try:
            data = json.loads(text)
        except ValueError:
            return self._issue(
                IssueCode.INVALID_RECORD,
                IssueSeverity.ERROR,
                _line_message(line_no, "invalid JSON"),
            )
        try:
            record = ExportPageRecord.model_validate(data)
        except ValidationError as exc:
            return self._issue(
                IssueCode.INVALID_RECORD,
                IssueSeverity.ERROR,
                _line_message(line_no, _error_types(exc)),
            )

        url = try_normalize_url(record.url)
        if url is None:
            # The raw value is never echoed: it may carry credentials.
            return self._issue(
                IssueCode.INVALID_URL,
                IssueSeverity.ERROR,
                _line_message(line_no, "url is not a valid http(s) URL"),
            )
        if not in_scope(url, scope):
            self._out_of_scope += 1
            return self._issue(
                IssueCode.OUT_OF_SCOPE,
                IssueSeverity.WARNING,
                _line_message(line_no, "url is outside the export's site scope"),
                url=url,
            )
        first_line = seen.get(url)
        if first_line is not None:
            self._duplicates += 1
            return self._issue(
                IssueCode.DUPLICATE_URL,
                IssueSeverity.WARNING,
                _line_message(line_no, f"duplicate of the url on line {first_line}"),
                url=url,
                related_url=url,
            )
        seen[url] = line_no

        try:
            return SourceDocument(
                source_type=SourceType.FILE_EXPORT,
                source_id=record.external_id or url,
                requested_url=url,
                final_url=url,
                http_status=None,
                fetched_at=self._clock(),
                html=record.body_html,
                assertions=SourceAssertions(
                    external_id=record.external_id,
                    page_type=record.page_type,
                    title=record.title,
                    canonical_url=record.canonical_url,
                    publication_state=record.publication_state,
                    language=record.language,
                    meta_description=record.meta_description,
                    published_at=record.published_at,
                    modified_at=record.modified_at,
                    metadata=dict(record.metadata),
                ),
            )
        except ValidationError as exc:
            return self._issue(
                IssueCode.INVALID_RECORD,
                IssueSeverity.ERROR,
                _line_message(line_no, _error_types(exc)),
                url=url,
            )

    async def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        scope = await self.site_scope()
        seen: dict[str, int] = {}
        lines = self._lines()
        try:
            for line_no, raw in lines:
                result = self._record(line_no, raw, scope, seen)
                if result is not None:
                    yield result
        finally:
            lines.close()

    async def summary(self) -> SourceSummary:
        return SourceSummary(
            discovered=self._discovered, skipped=self._duplicates + self._out_of_scope
        )

    async def aclose(self) -> None:
        """Idempotent. Closes the file if an iteration was abandoned mid-way."""
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            self._handle.close()
            self._handle = None
