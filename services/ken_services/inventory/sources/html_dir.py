"""``HtmlDirectoryContentSource``: import a folder of HTML files you are already allowed to use.

Read-only and local: it never opens a socket, never follows a link and never executes anything.
The folder layout is mapped onto URLs under an operator-declared ``site_url``::

    index.html          -> <site_url>
    reports/ev.html     -> <site_url>reports/ev
    reports/index.html  -> <site_url>reports/

It plugs into the unchanged ingestion pipeline as a ``file_export`` source with
``name = "html_directory"``, so normalisation, versioning, change detection, link extraction and
every later stage behave exactly as for a JSONL export. Files that are symlinks pointing outside the
folder, oversize, not UTF-8, or that map to a duplicate URL become issues, never failures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ken_contracts.inventory import (
    IssueCode,
    IssueSeverity,
    SiteScope,
    SourceAssertions,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
)
from ken_services.inventory.provenance import check_label
from ken_services.inventory.urls import build_scope, in_scope, normalize_url, try_normalize_url

__all__ = ["HtmlDirectoryContentSource"]

EXTENSIONS: Final = frozenset({".html", ".htm"})
INDEX_NAMES: Final = frozenset({"index.html", "index.htm"})
DEFAULT_MAX_FILES: Final = 5000
DEFAULT_MAX_FILE_BYTES: Final = 5_000_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HtmlDirectoryContentSource:
    """Deterministic (sorted-path) import of ``*.html`` / ``*.htm`` below one folder."""

    name: str = "html_directory"
    source_type: SourceType = SourceType.FILE_EXPORT

    def __init__(
        self,
        root: Path,
        *,
        site_url: str,
        allowed_hosts: tuple[str, ...] = (),
        provenance: str | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._root = Path(root)
        self._site_url = normalize_url(site_url)
        if not self._site_url.endswith("/"):
            self._site_url += "/"
        self._allowed_hosts = allowed_hosts
        self._provenance = check_label(provenance)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._clock = clock
        self._discovered = 0
        self._skipped = 0
        self._closed = False

    def config_snapshot(self) -> dict[str, Any]:
        """Secret-free: the folder NAME only (never its full path)."""
        return {
            "path": self._root.name,
            "kind": "html_directory",
            "site_url": self._site_url,
            "allowed_hosts": sorted(self._allowed_hosts),
            "provenance": self._provenance,
            "max_files": self._max_files,
            "max_file_bytes": self._max_file_bytes,
        }

    async def site_scope(self) -> SiteScope:
        if not self._root.is_dir():
            raise ValueError("the HTML folder does not exist or is not a directory")
        return build_scope(self._site_url, self._allowed_hosts)

    def _url_for(self, relative: Path) -> str | None:
        parts = list(relative.parts)
        name = parts[-1]
        if name.lower() in INDEX_NAMES:
            path = "/".join(parts[:-1])
            raw = self._site_url + (path + "/" if path else "")
        else:
            stem = Path(name).stem
            raw = self._site_url + "/".join([*parts[:-1], stem])
        return try_normalize_url(raw)

    def _issue(self, code: IssueCode, severity: IssueSeverity, message: str) -> SourceIssue:
        return SourceIssue(
            code=code, severity=severity, message=message[:500], occurred_at=self._clock()
        )

    async def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        scope = await self.site_scope()
        root = self._root.resolve()
        files = sorted(
            p for p in root.rglob("*") if p.suffix.lower() in EXTENSIONS and not p.is_dir()
        )
        seen: dict[str, str] = {}
        for index, path in enumerate(files):
            relative = path.relative_to(root).as_posix()
            if index >= self._max_files:
                self._skipped += 1
                if index == self._max_files:
                    yield self._issue(
                        IssueCode.LIMIT_REACHED,
                        IssueSeverity.WARNING,
                        f"more than {self._max_files} files; the rest were not imported",
                    )
                continue
            self._discovered += 1
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)  # a symlink leaving the folder is refused
            except (OSError, ValueError):
                self._skipped += 1
                yield self._issue(
                    IssueCode.OUT_OF_SCOPE,
                    IssueSeverity.WARNING,
                    f"{relative}: not inside the folder (symlink?); skipped",
                )
                continue
            url = self._url_for(Path(relative))
            if url is None or not in_scope(url, scope):
                self._skipped += 1
                yield self._issue(
                    IssueCode.INVALID_URL, IssueSeverity.ERROR, f"{relative}: cannot map to a URL"
                )
                continue
            if url in seen:
                self._skipped += 1
                yield self._issue(
                    IssueCode.DUPLICATE_URL,
                    IssueSeverity.WARNING,
                    f"{relative}: same URL as {seen[url]}; skipped",
                )
                continue
            seen[url] = relative
            if resolved.stat().st_size > self._max_file_bytes:
                self._skipped += 1
                yield self._issue(
                    IssueCode.TOO_LARGE, IssueSeverity.WARNING, f"{relative}: file too large"
                )
                continue
            try:
                html = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._skipped += 1
                yield self._issue(
                    IssueCode.INVALID_RECORD,
                    IssueSeverity.ERROR,
                    f"{relative}: unreadable or not UTF-8",
                )
                continue
            metadata = {"source_file": relative}
            if self._provenance:
                metadata["provenance"] = self._provenance
            yield SourceDocument(
                source_type=SourceType.FILE_EXPORT,
                source_id=url,
                requested_url=url,
                final_url=url,
                http_status=None,
                fetched_at=self._clock(),
                html=html,
                assertions=SourceAssertions(metadata=metadata),
            )

    async def summary(self) -> SourceSummary:
        return SourceSummary(discovered=self._discovered, skipped=self._skipped)

    async def aclose(self) -> None:
        self._closed = True
