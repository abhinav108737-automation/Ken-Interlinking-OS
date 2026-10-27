"""Shared provider machinery: the batch base class, URL resolution counting, and the GUARDED
Google egress client (ADR-0015 §4, §12.3 and §12.9.4 -- all binding).

Layering: ``ken_core`` never imports ``ken_services``, so nothing here knows the importer's
error vocabulary. A provider refuses with :class:`AnalyticsFetchError`, whose ``code`` is one
of :class:`FetchErrorCode` -- a deliberate, documented copy of the subset of
``ken_services.analytics.protocol.ImportErrorCode`` a provider can produce. The importer maps
it back. The message is the code only: never a row, a URL, a file path or a token.

**Google egress (refused BEFORE any I/O):**

* ``KEN_ANALYTICS_GOOGLE_ENABLED`` must be true;
* the base URL must be ``https`` on the exact allowlisted host (``Settings`` already refuses
  anything else when egress is enabled; this is the second, independent check);
* the OAuth access token is read from ``KEN_ANALYTICS_GOOGLE_TOKEN_FILE`` at the start of
  EVERY import and the file must be a regular file with mode ``0600``;
* ``follow_redirects=False`` and ``trust_env=False`` (no proxy, no netrc, no redirect to
  another host);
* 429 and 5xx get a bounded 3-try backoff; 401/403 is the fixed code ``google_auth_failed``;
* the token never appears in a log, a counter, an error, a snapshot or a request URL.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from ken_contracts.analytics import (
    MAX_SAMPLE_PATH_CHARS,
    MAX_UNRESOLVED_SAMPLE,
    AnalyticsBatch,
    Dataset,
    DateRange,
    FetchStats,
    LinkMetrics,
    PageMetrics,
    PageUrlResolver,
)
from ken_contracts.common import LinkId, PageId
from ken_contracts.model import ProviderHealth
from ken_core.config import GOOGLE_ANALYTICS_HOSTS, Settings
from ken_core.logging import get_logger
from ken_core.providers.analytics.service_account import (
    ServiceAccountError,
    ServiceAccountTokenSource,
)

__all__ = [
    "GOOGLE_RETRY_STATUSES",
    "GOOGLE_TRIES",
    "AnalyticsFetchError",
    "BaseAnalyticsProvider",
    "FetchErrorCode",
    "GoogleEgress",
    "StatsBuilder",
    "UrlDiagnostics",
    "resolve_page",
]

logger = get_logger(__name__)

GOOGLE_TRIES: Final = 3
"""Total attempts of one Google request (the bounded backoff of §12.3)."""
GOOGLE_RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
_BACKOFF_BASE_S: Final = 0.5
_TOKEN_MODE_MASK: Final = 0o077
"""Any bit set here means the token file is readable by group or others."""


class FetchErrorCode(StrEnum):
    """Why a provider refused or failed. A documented copy of the importer's subset (see the
    module docstring); ``ken_core`` may not import the service vocabulary."""

    INVALID_REQUEST = "invalid_request"
    GOOGLE_DISABLED = "google_disabled"
    GOOGLE_AUTH_FAILED = "google_auth_failed"
    GOOGLE_UNAVAILABLE = "google_unavailable"
    TOKEN_FILE_INVALID = "token_file_invalid"  # noqa: S105 -- an error code, not a secret
    FILE_TOO_LARGE = "file_too_large"
    FILE_INVALID = "file_invalid"
    TOO_MANY_ROWS = "too_many_rows"
    PROVIDER_FAILED = "provider_failed"


class AnalyticsFetchError(Exception):
    """A provider refused or failed. ``str(exc)`` is the code, nothing else."""

    def __init__(self, code: FetchErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


# --------------------------------------------------------------------------- #
# URL -> page resolution, with the counters the ledger needs
# --------------------------------------------------------------------------- #
@runtime_checkable
class UrlDiagnostics(Protocol):
    """What a resolver can tell a provider BEYOND the frozen
    :class:`~ken_contracts.analytics.PageUrlResolver`, so an unmatched URL can be counted as
    ``ambiguous_url`` rather than ``unresolved_url`` and a bounded sample PATH can be kept.

    ``ken_services.analytics.urls.InventoryUrlResolver`` implements it. A resolver that does
    not is treated as "never ambiguous, no sample".
    """

    def ambiguous(self, url: str) -> bool: ...

    def sample_path(self, url: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class PageResolution:
    page_id: PageId | None
    ambiguous: bool = False
    sample_path: str | None = None


def resolve_page(resolver: PageUrlResolver, url: str) -> PageResolution:
    """Resolve ``url`` and classify a miss (ADR-0015 §4 "URL -> page")."""
    page_id = resolver.resolve(url)
    if page_id is not None:
        return PageResolution(page_id=page_id)
    if isinstance(resolver, UrlDiagnostics):
        if resolver.ambiguous(url):
            return PageResolution(page_id=None, ambiguous=True)
        return PageResolution(page_id=None, sample_path=resolver.sample_path(url))
    return PageResolution(page_id=None)


@dataclass(slots=True)
class StatsBuilder:
    """Mutable accumulator for :class:`~ken_contracts.analytics.FetchStats`. Counters only:
    a row's contents are never kept, and the unresolved sample holds bounded PATHS."""

    rows_read: int = 0
    unresolved_url: int = 0
    unresolved_link: int = 0
    ambiguous_url: int = 0
    other_row: int = 0
    rejected_invalid: int = 0
    sampled: bool = False
    _sample: list[str] = field(default_factory=list)

    def note_unresolved_url(self, sample_path: str | None) -> None:
        self.unresolved_url += 1
        self.add_sample(sample_path)

    def add_sample(self, sample_path: str | None) -> None:
        if (
            sample_path is None
            or len(self._sample) >= MAX_UNRESOLVED_SAMPLE
            or len(sample_path) > MAX_SAMPLE_PATH_CHARS
            or sample_path in self._sample
        ):
            return
        self._sample.append(sample_path)

    def resolve(self, resolver: PageUrlResolver, url: str) -> PageId | None:
        """Resolve one URL, counting the miss. ``None`` means the row must be dropped."""
        outcome = resolve_page(resolver, url)
        if outcome.page_id is not None:
            return outcome.page_id
        if outcome.ambiguous:
            self.ambiguous_url += 1
        else:
            self.note_unresolved_url(outcome.sample_path)
        return None

    def build(self) -> FetchStats:
        return FetchStats(
            rows_read=self.rows_read,
            unresolved_url=self.unresolved_url,
            unresolved_link=self.unresolved_link,
            ambiguous_url=self.ambiguous_url,
            other_row=self.other_row,
            rejected_invalid=self.rejected_invalid,
            sampled=self.sampled,
            unresolved_sample=tuple(sorted(self._sample)),
        )


# --------------------------------------------------------------------------- #
# The batch base class
# --------------------------------------------------------------------------- #
class BaseAnalyticsProvider(ABC):
    """Implements the frozen protocol around one abstract :meth:`fetch_batch`.

    ``fetch_link_metrics`` / ``fetch_page_metrics`` are the thin filters the contract
    prescribes: they fetch the matching dataset and keep the requested ids (an empty id
    sequence means "everything the batch holds").
    """

    name: str = "analytics"

    @abstractmethod
    async def fetch_batch(
        self, *, date_range: DateRange, datasets: frozenset[Dataset]
    ) -> AnalyticsBatch: ...

    async def fetch_link_metrics(
        self, *, link_ids: Sequence[LinkId], date_range: DateRange
    ) -> list[LinkMetrics]:
        batch = await self.fetch_batch(date_range=date_range, datasets=frozenset({Dataset.LINKS}))
        wanted = {str(value) for value in link_ids}
        return [row for row in batch.link_rows if not wanted or row.link_id in wanted]

    async def fetch_page_metrics(
        self, *, page_ids: Sequence[PageId], date_range: DateRange
    ) -> list[PageMetrics]:
        batch = await self.fetch_batch(date_range=date_range, datasets=frozenset({Dataset.PAGES}))
        wanted = {str(value) for value in page_ids}
        return [row for row in batch.page_rows if not wanted or row.page_id in wanted]

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, reachable=True)

    async def aclose(self) -> None:
        """Idempotent; providers holding no resource need nothing."""
        return None


# --------------------------------------------------------------------------- #
# Guarded Google egress
# --------------------------------------------------------------------------- #
class GoogleEgress:
    """The ONLY way a provider talks to Google. Every guard of §12.3 / §12.9.4 is here."""

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str,
        host_setting: str,
        scope: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._scope = scope
        self._token_source: ServiceAccountTokenSource | None = None
        self._base = base_url.rstrip("/")
        self._host_setting = host_setting
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # ------------------------------------------------------------- gating
    @property
    def enabled(self) -> bool:
        return self._settings.analytics_google_enabled

    def _loopback_fixture(self) -> bool:
        """A loopback base URL is a test fixture. ``Settings`` already refuses it together
        with ``analytics_google_enabled``, and the token file is NEVER read for it."""
        from ken_core.config import is_loopback_host

        return is_loopback_host(urlsplit(self._base).hostname)

    def gate(self) -> None:
        """Refuse before any I/O. Loopback fixtures are allowed only while egress is off."""
        if self._closed:
            raise AnalyticsFetchError(FetchErrorCode.PROVIDER_FAILED)
        parts = urlsplit(self._base)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)
        if self._loopback_fixture():
            if self.enabled:
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_DISABLED)
            if self._settings.environment not in {"local", "ci"}:
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_DISABLED)
            return
        if not self.enabled:
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_DISABLED)
        expected = GOOGLE_ANALYTICS_HOSTS[self._host_setting]
        if parts.scheme != "https" or (parts.hostname or "").lower() != expected:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)
        if parts.port not in {None, 443}:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)

    async def access_token(self) -> str | None:
        """The bearer token for THIS import: from the service-account key when
        ``KEN_ANALYTICS_SERVICE_ACCOUNT_FILE`` is set, else from the token file. ``None`` only for
        a loopback test fixture. Refusals are the fixed ``AnalyticsFetchError`` codes."""
        if self._loopback_fixture():
            return None
        key_file = self._settings.analytics_service_account_file
        if not key_file:
            return self.read_token()
        if self._scope is None:
            raise AnalyticsFetchError(FetchErrorCode.INVALID_REQUEST)
        if self._token_source is None:
            self._token_source = ServiceAccountTokenSource(
                key_file,
                timeout_s=self._settings.analytics_google_timeout_s,
                transport=self._transport,
            )
        try:
            return await self._token_source.token(self._scope)
        except ServiceAccountError as exc:
            if exc.code == "key_invalid":
                raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID) from None
            if exc.code == "auth_failed":
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_AUTH_FAILED) from None
            raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE) from None

    def read_token(self) -> str | None:
        """The access token for THIS import, or ``None`` for a loopback fixture.

        Refuses unless the path is a regular file whose mode is exactly ``0600``. The value
        is returned, never logged, and never stored on this object.
        """
        if self._loopback_fixture():
            return None
        path_value = self._settings.analytics_google_token_file
        if not path_value:
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        path = Path(path_value)
        try:
            info = path.stat()
            if not path.is_file() or (info.st_mode & _TOKEN_MODE_MASK):
                raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
            token = path.read_text(encoding="utf-8").strip()
        except AnalyticsFetchError:
            raise
        except OSError as exc:
            logger.error("analytics_token_file_unreadable", error_class=type(exc).__name__)
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID) from None
        if not token or any(char.isspace() for char in token) or not token.isascii():
            raise AnalyticsFetchError(FetchErrorCode.TOKEN_FILE_INVALID)
        return token

    # -------------------------------------------------------------- client
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=self._settings.analytics_google_timeout_s,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    async def post(self, path: str, *, body: dict[str, Any], token: str | None) -> Any:
        """One guarded POST with the bounded backoff. Returns the decoded JSON object."""
        self.gate()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        last: int | None = None
        for attempt in range(1, GOOGLE_TRIES + 1):
            try:
                response = await self._http().post(path, json=body, headers=headers)
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as exc:
                # The class name only: an httpx message can carry the URL (and its query).
                logger.warning(
                    "analytics_google_request_failed",
                    host_setting=self._host_setting,
                    attempt=attempt,
                    error_class=type(exc).__name__,
                )
                if attempt == GOOGLE_TRIES:
                    raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE) from None
                await self._sleep(_BACKOFF_BASE_S * attempt)
                continue
            status = response.status_code
            if status in {401, 403}:
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_AUTH_FAILED)
            if status in GOOGLE_RETRY_STATUSES:
                last = status
                logger.warning(
                    "analytics_google_retrying",
                    host_setting=self._host_setting,
                    attempt=attempt,
                    status=status,
                )
                if attempt == GOOGLE_TRIES:
                    break
                await self._sleep(_BACKOFF_BASE_S * attempt)
                continue
            if status != 200:
                logger.warning(
                    "analytics_google_unexpected_status",
                    host_setting=self._host_setting,
                    status=status,
                )
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE)
            try:
                payload = response.json()
            except ValueError:
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE) from None
            if not isinstance(payload, dict):
                raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE)
            return payload
        logger.error("analytics_google_exhausted", host_setting=self._host_setting, status=last)
        raise AnalyticsFetchError(FetchErrorCode.GOOGLE_UNAVAILABLE)

    async def aclose(self) -> None:
        self._closed = True
        if self._token_source is not None:
            await self._token_source.aclose()
            self._token_source = None
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
