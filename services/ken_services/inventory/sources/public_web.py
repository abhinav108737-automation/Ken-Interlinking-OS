"""Polite, domain-scoped public web crawler (ADR-0009 sections 8 and 9).

``PublicWebContentSource`` implements ``ContentSourceProvider``. It is only ever
pointed at local fixture servers in tests; a real crawl needs an explicitly
configured base URL.

Politeness is not configurable away:

* GET only, never a form submission, never credentials; cookies are neither
  stored nor sent (a cookie jar whose policy accepts no domain, plus a request
  hook that strips ``Cookie`` / ``Authorization``); no proxy or netrc
  (``trust_env=False``).
* robots.txt is always honoured: fetched once per origin and cached. 2xx is
  parsed (first 500 KiB), 4xx allows everything, and 5xx, a timeout, a network
  error or a redirect leaving the site disallows the whole origin and yields
  one ``ROBOTS_UNAVAILABLE`` issue.
* One rate limiter spaces request STARTS (robots.txt included) by at least
  ``max(delay_s, min(Crawl-delay, 60))``; a semaphore bounds concurrency.
* Redirects are followed by hand, hop by hop; every hop is normalised,
  scope-, loop-, duplicate- and robots-checked.

Determinism: the crawl is breadth-first by depth. Every URL of a level is
planned (robots, page budget) sequentially in discovery order, then fetched
concurrently in redirect "rounds": each round fetches one hop for every
active chain, and the round's redirects are resolved sequentially in discovery
order before the next round starts. Records are then yielded in discovery
order and the next level is built from links in document order. Network timing
therefore never changes which URL is fetched, which is a duplicate, or the
order of records.

Issue ``url`` conventions: redirect-specific issues (out of scope, loop, too
many, duplicate, invalid target, robots-blocked hop) use ``url`` = the
requested URL and ``related_url`` = the redirect target. A terminal failure
(404, timeout, non-HTML ...) uses ``url`` = the URL that actually answered and,
when redirects led there, ``related_url`` = the originally requested URL.
"""

from __future__ import annotations

import asyncio
import codecs
import email.utils
import fnmatch
import html as html_lib
import http.cookiejar
import posixpath
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from ken_contracts.inventory import (
    CrawlConfig,
    IssueCode,
    IssueSeverity,
    SiteScope,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
)
from ken_core.net import NetworkGuard, UnsafeDestinationError
from ken_services.inventory import urls
from ken_services.inventory.html import discover
from ken_services.inventory.robots import (
    MAX_ROBOTS_BYTES,
    ROBOTS_PATH,
    RobotsPolicy,
    allow_all,
    disallow_all,
    parse_robots,
)
from ken_services.inventory.sources.browser_render import BrowserRenderer, RenderResult

ACCEPT: Final = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
HTML_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
MAX_CRAWL_DELAY_S: Final = 60.0
MAX_ROBOTS_REDIRECTS: Final = 5
_MAX_CONTENT_LANGUAGE: Final = 100
_FORBIDDEN_HEADERS: Final = ("cookie", "authorization", "proxy-authorization")
SITEMAP_MEDIA_TYPES: Final = frozenset({"application/xml", "text/xml", "application/xhtml+xml"})
# Regex, not an XML parser: nothing here can expand entities or fetch an external DTD.
_LOC_RE: Final = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)
_SITEMAP_INDEX_RE: Final = re.compile(r"<\s*sitemapindex\b", re.I)


def parse_sitemap(text: str) -> tuple[bool, list[str]]:
    """``(is_index, <loc> values in document order)`` of a sitemap document."""
    locs = [html_lib.unescape(m.group(1)).strip() for m in _LOC_RE.finditer(text)]
    return bool(_SITEMAP_INDEX_RE.search(text)), [loc for loc in locs if loc]


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Internal fetch results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Redirect:
    status: int
    location: str | None


@dataclass(frozen=True, slots=True)
class _Page:
    status: int
    content_type: str
    content_language: str | None
    html: str


@dataclass(frozen=True, slots=True)
class _Failure:
    code: IssueCode
    severity: IssueSeverity
    message: str
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class _RobotsResponse:
    status: int
    location: str | None
    body: bytes


type _PageResult = _Redirect | _Page | _Failure


@dataclass(slots=True)
class _Chain:
    """One page request and the redirect hops it led to."""

    requested: str
    depth: int
    current: str
    hops: list[str] = field(default_factory=list)
    pre: list[SourceIssue] = field(default_factory=list)
    outcome: SourceDocument | SourceIssue | None = None
    page_limited: bool = False


class _RateLimiter:
    """Minimum gap between request STARTS, shared by every request of a crawl."""

    def __init__(
        self, sleep: Callable[[float], Awaitable[None]], monotonic: Callable[[], float]
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._last: float | None = None
        self._hold_until: float = 0.0

    def hold(self, seconds: float) -> None:
        """Delay every later request start by at least ``seconds`` from now (Retry-After)."""
        self._hold_until = max(self._hold_until, self._monotonic() + seconds)

    async def wait(self, gap: float) -> None:
        async with self._lock:
            now = self._monotonic()
            due = now
            if self._last is not None:
                due = max(due, self._last + gap)
            due = max(due, self._hold_until)
            if due > now:
                await self._sleep(due - now)
                now = max(self._monotonic(), due)
            self._last = now


async def _strip_credentials(request: httpx.Request) -> None:
    """Defence in depth: no request ever carries a cookie or credentials."""
    for name in _FORBIDDEN_HEADERS:
        if name in request.headers:
            del request.headers[name]


def _media_type(value: str | None) -> str | None:
    if not value:
        return None
    media = value.split(";", 1)[0].strip().lower()
    return media or None


def _http_status(status: int) -> int | None:
    return status if 100 <= status <= 599 else None


def _decode(body: bytes, charset: str | None) -> str:
    if charset:
        try:
            codecs.lookup(charset)
            return body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            pass
    return body.decode("utf-8", errors="replace")


def _raw_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    """The body as sent, never decoded. A response already fully buffered by the transport
    (in-memory test transports) has no raw stream left; its bytes are already in memory."""
    if response.is_stream_consumed:
        return response.aiter_bytes()
    return response.aiter_raw()


def _is_encoded(response: httpx.Response) -> bool:
    """Whether the response declares any Content-Encoding other than ``identity``."""
    value = response.headers.get("content-encoding", "")
    return any(t.strip().lower() not in {"", "identity"} for t in value.split(","))


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _normalize(url: str, base: str) -> str | None:
    """``urls.try_normalize_url``, defensively absorbing any ``ValueError``.

    ``urls.normalize_url`` now resolves ``urljoin`` inside its own ``try`` (an
    href such as ``http://[bad`` returns ``None``); this wrapper stays as a
    second guard, because a hostile page must never abort a crawl.
    """
    try:
        return urls.try_normalize_url(url, base=base)
    except ValueError:
        return None


def _origin(normalized_url: str) -> str:
    parts = urlsplit(normalized_url)
    return f"{parts.scheme}://{parts.netloc}"


def _path_and_query(normalized_url: str) -> str:
    parts = urlsplit(normalized_url)
    return f"{parts.path or '/'}?{parts.query}" if parts.query else (parts.path or "/")


def rule_reason(config: CrawlConfig, normalized_url: str) -> str | None:
    """Why the include/exclude rules refuse an already-normalised URL, or ``None``."""
    parts = urlsplit(normalized_url)
    if config.https_only and parts.scheme != "https":
        return "not https"
    path = parts.path or "/"
    lowered = path.lower()
    if config.include_path_prefixes and not any(
        path.startswith(prefix) for prefix in config.include_path_prefixes
    ):
        return "outside the included paths"
    if any(fnmatch.fnmatchcase(lowered, glob.lower()) for glob in config.exclude_path_globs):
        return "matches an exclude pattern"
    extension = posixpath.splitext(lowered)[1]
    if extension and extension in config.exclude_extensions:
        return "excluded file type"
    if config.exclude_query_params and parts.query:
        names = {pair.split("=", 1)[0].lower() for pair in parts.query.split("&") if pair}
        if names & set(config.exclude_query_params):
            return "excluded query parameter"
    return None


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds from now (delta-seconds or HTTP-date), or ``None``."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now).total_seconds())


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #
class PublicWebContentSource:
    """Breadth-first, robots-honouring crawler of one site. See the module docstring."""

    name: str = "public_web"
    source_type: SourceType = SourceType.PUBLIC_WEB

    def __init__(
        self,
        config: CrawlConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        guard: NetworkGuard | None = None,
        allow_private_networks: bool = False,
        screenshot_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._screenshot_dir = screenshot_dir
        self._renderer: BrowserRenderer | None = None
        # SSRF guard (ken_core.net). Always on for real network I/O. With an injected
        # in-memory transport no socket is ever opened, so there is nothing to guard.
        self._guard: NetworkGuard | None = guard or (
            None
            if transport is not None
            else NetworkGuard(allow_private_networks=allow_private_networks)
        )
        self._transport = transport
        self._clock = clock
        self._limiter = _RateLimiter(sleep, monotonic)
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._started = False
        self._site_scope: SiteScope | None = None

        self._robots: dict[str, RobotsPolicy] = {}
        self._discovered: set[str] = set()
        self._seen: set[str] = set()  # enqueued, requested, or decided not to request
        self._claimed: set[str] = set()  # requested (initial request or redirect hop)
        self._skipped: set[str] = set()
        self._non_html = 0
        self._variants: dict[str, int] = {}
        self._variant_limited: set[str] = set()
        self._budget = config.max_pages
        self._page_limited = 0
        self._consecutive_blocks = 0
        self._aborted: str | None = None
        self._rule_skipped = 0

    # -- ContentSourceProvider -------------------------------------------- #
    def config_snapshot(self) -> dict[str, Any]:
        return self._config.model_dump(mode="json")

    async def site_scope(self) -> SiteScope:
        return self._scope()

    async def summary(self) -> SourceSummary:
        skipped = len(self._skipped - self._claimed) + self._non_html
        return SourceSummary(discovered=len(self._discovered), skipped=skipped)

    async def aclose(self) -> None:
        self._closed = True
        if self._renderer is not None:
            renderer, self._renderer = self._renderer, None
            await renderer.close()
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    async def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        if self._started:
            raise RuntimeError("iter_records() can be consumed only once")
        self._started = True
        scope = self._scope()
        if self._config.sitemap_urls:
            level, sitemap_issues = await self._sitemap_seeds(scope)
            for sitemap_issue in sitemap_issues:
                yield sitemap_issue
        else:
            seed = urls.normalize_url(self._config.base_url)
            level = [seed]
        for seed in level:
            self._discovered.add(seed)
            self._seen.add(seed)
            self._variants[urls.path_key(seed)] = self._variants.get(urls.path_key(seed), 0) + 1
        depth = 0
        while level:
            chains = await self._plan_level(level, depth)
            await self._fetch_level(chains, scope)
            next_level: list[str] = []
            for chain in chains:
                for issue in chain.pre:
                    yield issue
                if chain.outcome is None:
                    continue  # page budget exhausted: summarised once at the end
                yield chain.outcome
                if isinstance(chain.outcome, SourceDocument):
                    for issue in self._discover_links(chain.outcome, scope, next_level):
                        yield issue
            level = next_level
            depth += 1
            if self._aborted is not None:
                break

        if self._aborted is not None:
            yield self._issue(
                IssueCode.HTTP_ERROR,
                IssueSeverity.ERROR,
                f"crawl aborted to protect the site: {self._aborted}",
            )
        if self._page_limited:
            yield self._issue(
                IssueCode.LIMIT_REACHED,
                IssueSeverity.INFO,
                f"page request limit ({self._config.max_pages}) reached; "
                f"{self._page_limited} discovered URLs were not requested",
            )

    # -- sitemap seeds ------------------------------------------------------ #
    async def _sitemap_seeds(self, scope: SiteScope) -> tuple[list[str], list[SourceIssue]]:
        """Seed URLs from the sitemaps NAMED in the config, plus the issues met reading them.

        Each sitemap is one rate-limited, guarded GET that must be in scope and allowed by
        robots.txt; a redirect is not followed and a sitemap index is not expanded. The page rules,
        robots check and page budget still apply to every seed afterwards.
        """
        seeds: list[str] = []
        seen: set[str] = set()
        problems: list[SourceIssue] = []
        for raw in self._config.sitemap_urls:
            sitemap = _normalize(raw, raw)
            if sitemap is None or not urls.in_scope(sitemap, scope):
                problems.append(
                    self._issue(
                        IssueCode.OUT_OF_SCOPE,
                        IssueSeverity.ERROR,
                        "sitemap URL is invalid or outside the approved hosts; not fetched",
                        url=raw,
                    )
                )
                continue
            holder = _Chain(requested=sitemap, depth=0, current=sitemap)
            policy = await self._robots_policy(sitemap, holder)
            problems.extend(holder.pre)
            if not policy.can_fetch(_path_and_query(sitemap)):
                problems.append(
                    self._issue(
                        IssueCode.ROBOTS_DISALLOWED,
                        IssueSeverity.ERROR,
                        "sitemap disallowed by robots.txt; not fetched",
                        url=sitemap,
                    )
                )
                continue
            result = await self._request(sitemap, self._gap_for(sitemap), self._handle_sitemap)
            if isinstance(result, _Failure):
                problems.append(
                    self._issue(
                        result.code,
                        result.severity,
                        f"sitemap not read: {result.message}",
                        url=sitemap,
                        http_status=result.http_status,
                    )
                )
                continue
            is_index, locs = parse_sitemap(_decode(result, "utf-8"))
            if is_index:
                problems.append(
                    self._issue(
                        IssueCode.LIMIT_REACHED,
                        IssueSeverity.WARNING,
                        "this is a sitemap INDEX; child sitemaps are not followed (name the exact "
                        "sitemap URL instead)",
                        url=sitemap,
                    )
                )
                continue
            kept = 0
            for loc in locs:
                url = _normalize(loc, sitemap)
                if url is None or url in seen or not urls.in_scope(url, scope):
                    continue
                seen.add(url)
                seeds.append(url)
                kept += 1
            problems.append(
                self._issue(
                    IssueCode.LIMIT_REACHED,
                    IssueSeverity.INFO,
                    f"sitemap read: {len(locs)} entries, {kept} in scope",
                    url=sitemap,
                )
            )
        return seeds, problems

    async def _handle_sitemap(self, response: httpx.Response) -> bytes | _Failure:
        status = response.status_code
        if not 200 <= status < 300:
            return _Failure(
                IssueCode.HTTP_ERROR,
                IssueSeverity.ERROR,
                f"unexpected HTTP status {status} (redirects are not followed)",
                _http_status(status),
            )
        media = _media_type(response.headers.get("content-type"))
        if media not in SITEMAP_MEDIA_TYPES:
            return _Failure(
                IssueCode.NON_HTML, IssueSeverity.ERROR, f"not XML ({media or 'no content type'})"
            )
        if _is_encoded(response):
            return _Failure(
                IssueCode.TOO_LARGE,
                IssueSeverity.ERROR,
                "unsolicited Content-Encoding refused (identity was requested)",
            )
        limit = self._config.sitemap_max_bytes
        declared = _content_length(response)
        if declared is not None and declared > limit:
            return _Failure(IssueCode.TOO_LARGE, IssueSeverity.ERROR, f"larger than {limit} bytes")
        body = bytearray()
        async for chunk in _raw_chunks(response):
            body.extend(chunk)
            if len(body) > limit:
                return _Failure(
                    IssueCode.TOO_LARGE, IssueSeverity.ERROR, f"larger than {limit} bytes"
                )
        return bytes(body)

    # -- planning and fetching -------------------------------------------- #
    def _scope(self) -> SiteScope:
        if self._site_scope is None:
            self._site_scope = urls.build_scope(self._config.base_url, self._config.allowed_hosts)
        return self._site_scope

    async def _plan_level(self, level: list[str], depth: int) -> list[_Chain]:
        """Robots and page-budget decisions, sequentially in discovery order."""
        chains: list[_Chain] = []
        for url in level:
            chain = _Chain(requested=url, depth=depth, current=url)
            chains.append(chain)
            if self._budget <= 0:
                chain.page_limited = True
                self._page_limited += 1
                self._skipped.add(url)
                continue
            reason = self._rule_reason(url)
            if reason is not None:
                chain.outcome = self._issue(
                    IssueCode.OUT_OF_SCOPE,
                    IssueSeverity.INFO,
                    f"skipped by the crawl rules ({reason})",
                    url=url,
                )
                self._skipped.add(url)
                self._rule_skipped += 1
                continue
            policy = await self._robots_policy(url, chain)
            if not policy.can_fetch(_path_and_query(url)):
                chain.outcome = self._issue(
                    IssueCode.ROBOTS_DISALLOWED,
                    IssueSeverity.INFO,
                    "disallowed by robots.txt",
                    url=url,
                )
                self._skipped.add(url)
                continue
            self._budget -= 1
            self._claimed.add(url)
        return chains

    async def _fetch_level(self, chains: list[_Chain], scope: SiteScope) -> None:
        """Fetch every planned chain, one redirect hop per round."""
        active = [c for c in chains if c.outcome is None and not c.page_limited]
        while active:
            results = await asyncio.gather(*(self._fetch_page(c.current) for c in active))
            following: list[_Chain] = []
            for chain, result in zip(active, results, strict=True):
                if isinstance(result, _Redirect):
                    if await self._follow(chain, result, scope):
                        following.append(chain)
                elif isinstance(result, _Page):
                    chain.outcome = self._document(chain, result)
                else:
                    if result.code is IssueCode.NON_HTML:
                        self._non_html += 1
                    chain.outcome = self._issue(
                        result.code,
                        result.severity,
                        result.message,
                        url=chain.current,
                        related_url=chain.requested if chain.hops else None,
                        http_status=result.http_status,
                    )
            active = following

    async def _follow(self, chain: _Chain, redirect: _Redirect, scope: SiteScope) -> bool:
        """Decide one redirect hop. True if the chain continues to the target."""
        status = _http_status(redirect.status)
        if not redirect.location or not redirect.location.strip():
            chain.outcome = self._issue(
                IssueCode.HTTP_ERROR,
                IssueSeverity.ERROR,
                "redirect without a Location header",
                url=chain.current,
                http_status=status,
            )
            return False
        target = _normalize(redirect.location, chain.current)
        if target is None:
            chain.outcome = self._issue(
                IssueCode.INVALID_URL,
                IssueSeverity.ERROR,
                "redirect target is not a valid http(s) URL",
                url=chain.requested,
                http_status=status,
            )
            return False

        def fail(code: IssueCode, severity: IssueSeverity, message: str) -> bool:
            chain.outcome = self._issue(
                code,
                severity,
                message,
                url=chain.requested,
                related_url=target,
                http_status=status,
            )
            return False

        if not urls.in_scope(target, scope):
            return fail(
                IssueCode.REDIRECT_OUT_OF_SCOPE,
                IssueSeverity.WARNING,
                "redirect leaves the site scope; target not fetched",
            )
        if chain.current.startswith("https:") and target.startswith("http:"):
            return fail(
                IssueCode.REDIRECT_OUT_OF_SCOPE,
                IssueSeverity.WARNING,
                "redirect downgrades https to http; target not fetched",
            )
        if self._guard is not None:
            try:
                await self._guard.check_url(target)
            except UnsafeDestinationError as exc:
                return fail(
                    IssueCode.REDIRECT_OUT_OF_SCOPE,
                    IssueSeverity.WARNING,
                    f"redirect target refused by the network guard ({exc.code})",
                )
        self._discovered.add(target)
        if target == chain.current or target in chain.hops:
            return fail(IssueCode.REDIRECT_LOOP, IssueSeverity.ERROR, "redirect loop detected")
        if target in self._claimed:
            return fail(
                IssueCode.DUPLICATE_URL,
                IssueSeverity.INFO,
                "redirect target was already fetched in this crawl",
            )
        if len(chain.hops) >= self._config.max_redirects:
            return fail(
                IssueCode.TOO_MANY_REDIRECTS,
                IssueSeverity.ERROR,
                f"more than {self._config.max_redirects} redirects",
            )
        reason = self._rule_reason(target)
        if reason is not None:
            self._skipped.add(target)
            self._seen.add(target)
            self._rule_skipped += 1
            return fail(
                IssueCode.OUT_OF_SCOPE,
                IssueSeverity.INFO,
                f"redirect target skipped by the crawl rules ({reason})",
            )
        policy = await self._robots_policy(target, chain)
        if not policy.can_fetch(_path_and_query(target)):
            self._skipped.add(target)
            self._seen.add(target)
            return fail(
                IssueCode.ROBOTS_DISALLOWED,
                IssueSeverity.INFO,
                "redirect target disallowed by robots.txt",
            )
        chain.hops.append(chain.current)
        chain.current = target
        self._claimed.add(target)
        self._seen.add(target)
        return True

    def _rule_reason(self, normalized_url: str) -> str | None:
        """Why the include/exclude rules refuse this URL, or ``None`` if they allow it."""
        return rule_reason(self._config, normalized_url)

    def _abort(self, reason: str) -> None:
        if self._aborted is None:
            self._aborted = reason

    def _note_status(self, status: int, retry_after: str | None) -> None:
        """Back-off bookkeeping for one answered request (never retried, only slowed/stopped)."""
        config = self._config
        blocked = status in (403, 429) or status >= 500
        if not blocked:
            self._consecutive_blocks = 0
            return
        self._consecutive_blocks += 1
        if status in (429, 503) and config.max_retry_after_s > 0:
            seconds = _retry_after_seconds(retry_after, self._clock())
            if seconds is None:
                seconds = config.max_retry_after_s  # no usable header: wait the cap
            if seconds > config.max_retry_after_s:
                self._abort(
                    f"Retry-After {seconds:g}s exceeds the {config.max_retry_after_s:g}s cap"
                )
            else:
                self._limiter.hold(seconds)
        if (
            config.max_consecutive_blocks
            and self._consecutive_blocks >= config.max_consecutive_blocks
        ):
            self._abort(f"{self._consecutive_blocks} consecutive 403/429/5xx responses")

    def _gap_for(self, url: str) -> float:
        policy = self._robots.get(_origin(url))
        crawl_delay = policy.crawl_delay if policy is not None else None
        gap = self._config.delay_s
        if crawl_delay is not None:
            gap = max(gap, min(crawl_delay, MAX_CRAWL_DELAY_S))
        return gap

    async def _fetch_page(self, url: str) -> _PageResult:
        if self._config.render == "browser":
            return await self._fetch_page_browser(url)
        return await self._request(url, self._gap_for(url), self._handle_page)

    # -- browser rendering -------------------------------------------------- #
    async def _browser_gate(self, url: str, is_document: bool) -> str | None:
        """Refusal reason for ONE browser request, or None. Applied to the document and to every
        subresource, so the page cannot reach anything the HTTP crawler could not."""
        normalized = _normalize(url, url)
        if normalized is None:
            return "invalid url"
        if not normalized.startswith("https:") and self._config.https_only:
            return "not https"
        if not urls.in_scope(normalized, self._scope()):
            return "outside the approved hosts"
        if is_document:
            reason = self._rule_reason(normalized)
            if reason is not None:
                return reason
        if self._guard is not None:
            try:
                await self._guard.check_url(normalized)
            except UnsafeDestinationError as exc:
                return f"network guard ({exc.code})"
        policy = self._robots.get(_origin(normalized))
        if policy is None:
            return "robots.txt not loaded for this origin"
        if not policy.can_fetch(_path_and_query(normalized)):
            return "disallowed by robots.txt"
        return None

    def _get_renderer(self) -> BrowserRenderer:
        if self._closed:
            raise RuntimeError("source is closed")
        if self._transport is not None or self._guard is None:
            # An injected in-memory transport (tests) has no sockets, but a browser would open real
            # ones: browser mode is never combined with it, and never runs without the SSRF guard.
            raise RuntimeError("browser rendering requires the real network path and the guard")
        if self._renderer is None:
            self._renderer = BrowserRenderer(
                user_agent=self._config.user_agent,
                timeout_s=self._config.timeout_s,
                max_bytes=self._config.max_bytes,
                max_subrequests=self._config.browser_max_subrequests,
                gate=self._browser_gate,
                pace=self._browser_pace,
                on_status=self._note_status,
                screenshot_dir=self._screenshot_dir,
            )
        return self._renderer

    async def _browser_pace(self) -> None:
        if self._aborted is not None:
            raise RuntimeError("crawl aborted")
        await self._limiter.wait(self._config.delay_s)

    async def _fetch_page_browser(self, url: str) -> _PageResult:
        async with self._semaphore:
            if self._aborted is not None:
                return _Failure(
                    IssueCode.LIMIT_REACHED,
                    IssueSeverity.INFO,
                    "not requested: the crawl was aborted",
                )
            renderer = self._get_renderer()  # misconfiguration fails the run loudly
            try:
                # robots.txt of the origin must already be known (plan level fetched it)
                result = await renderer.render(url)
            except RuntimeError:
                return _Failure(
                    IssueCode.LIMIT_REACHED,
                    IssueSeverity.INFO,
                    "not requested: the crawl was aborted",
                )
            return self._from_render(result)

    def _from_render(self, result: RenderResult) -> _PageResult:
        if result.kind == "redirect":
            return _Redirect(result.status, result.location)
        if result.kind == "page":
            return _Page(
                status=result.status,
                content_type=result.content_type,
                content_language=result.content_language,
                html=result.html,
            )
        status = _http_status(result.status) if result.status else None
        match result.failure:
            case "timeout":
                return _Failure(IssueCode.TIMEOUT, IssueSeverity.ERROR, "browser render timed out")
            case "too_large":
                return _Failure(
                    IssueCode.TOO_LARGE,
                    IssueSeverity.WARNING,
                    f"response larger than {self._config.max_bytes} bytes",
                    status,
                )
            case "non_html":
                return _Failure(
                    IssueCode.NON_HTML,
                    IssueSeverity.INFO,
                    f"not HTML ({result.content_type or 'no content type'}); skipped",
                    status,
                )
            case "status":
                if result.status == 404:
                    return _Failure(
                        IssueCode.HTTP_NOT_FOUND, IssueSeverity.WARNING, "not found", 404
                    )
                if result.status == 410:
                    return _Failure(IssueCode.HTTP_GONE, IssueSeverity.WARNING, "gone", 410)
                return _Failure(
                    IssueCode.HTTP_ERROR,
                    IssueSeverity.ERROR,
                    f"unexpected HTTP status {result.status}",
                    status,
                )
            case "refused":
                return _Failure(
                    IssueCode.OUT_OF_SCOPE,
                    IssueSeverity.WARNING,
                    f"browser request refused ({result.location})",
                )
            case _:
                return _Failure(
                    IssueCode.NETWORK_ERROR,
                    IssueSeverity.ERROR,
                    "browser navigation failed or the page redirected itself",
                )

    async def _handle_page(self, response: httpx.Response) -> _PageResult:
        status = response.status_code
        if status in REDIRECT_STATUSES:
            return _Redirect(status, response.headers.get("location"))
        if status == 404:
            return _Failure(IssueCode.HTTP_NOT_FOUND, IssueSeverity.WARNING, "not found", 404)
        if status == 410:
            return _Failure(IssueCode.HTTP_GONE, IssueSeverity.WARNING, "gone", 410)
        if not 200 <= status < 300:
            return _Failure(
                IssueCode.HTTP_ERROR,
                IssueSeverity.ERROR,
                f"unexpected HTTP status {status}",
                _http_status(status),
            )
        media = _media_type(response.headers.get("content-type"))
        if media not in HTML_MEDIA_TYPES:
            return _Failure(
                IssueCode.NON_HTML,
                IssueSeverity.INFO,
                f"not HTML ({media or 'no content type'}); skipped",
                status,
            )
        too_large = _Failure(
            IssueCode.TOO_LARGE,
            IssueSeverity.WARNING,
            f"response larger than {self._config.max_bytes} bytes",
            status,
        )
        declared = _content_length(response)
        if declared is not None and declared > self._config.max_bytes:
            return too_large
        if _is_encoded(response):
            # We send Accept-Encoding: identity, so any other coding is unsolicited. Never
            # inflate it: a small body can expand a thousandfold before a size cap applies.
            return _Failure(
                IssueCode.TOO_LARGE,
                IssueSeverity.WARNING,
                "unsolicited Content-Encoding refused (identity was requested)",
                status,
            )
        body = bytearray()
        # aiter_raw: the bytes as sent, never decoded, so len(body) is the real memory held.
        async for chunk in _raw_chunks(response):
            body.extend(chunk)
            if len(body) > self._config.max_bytes:
                return too_large
        language = (response.headers.get("content-language") or "").strip() or None
        if language is not None and len(language) > _MAX_CONTENT_LANGUAGE:
            language = None
        return _Page(
            status=status,
            content_type=media,
            content_language=language,
            html=_decode(bytes(body), response.charset_encoding),
        )

    async def _request[T](
        self,
        url: str,
        gap: float,
        handle: Callable[[httpx.Response], Awaitable[T]],
    ) -> T | _Failure:
        """One rate-limited GET. Per-request failures become a ``_Failure``, never raise."""
        client = self._get_client()
        try:
            request = client.build_request("GET", url)
        except httpx.InvalidURL:
            return _Failure(IssueCode.INVALID_URL, IssueSeverity.ERROR, "URL cannot be requested")
        async with self._semaphore:
            if self._aborted is not None:  # never touch the site again after an abort
                return _Failure(
                    IssueCode.LIMIT_REACHED,
                    IssueSeverity.INFO,
                    "not requested: the crawl was aborted",
                )
            await self._limiter.wait(gap)
            if self._aborted is not None:
                return _Failure(
                    IssueCode.LIMIT_REACHED,
                    IssueSeverity.INFO,
                    "not requested: the crawl was aborted",
                )
            try:
                async with asyncio.timeout(self._config.timeout_s):
                    if self._guard is not None:
                        await self._guard.check_url(url)
                    response = await client.send(request, stream=True)
                    try:
                        if self._guard is not None:
                            self._guard.check_response(response)  # DNS-rebinding TOCTOU
                        self._note_status(response.status_code, response.headers.get("retry-after"))
                        return await handle(response)
                    finally:
                        await response.aclose()
            except UnsafeDestinationError as exc:
                return _Failure(
                    IssueCode.OUT_OF_SCOPE,
                    IssueSeverity.ERROR,
                    f"destination refused by the network guard ({exc.code})",
                )
            except (TimeoutError, httpx.TimeoutException) as exc:
                return _Failure(
                    IssueCode.TIMEOUT,
                    IssueSeverity.ERROR,
                    f"request timed out ({type(exc).__name__})",
                )
            except httpx.RequestError as exc:
                return _Failure(
                    IssueCode.NETWORK_ERROR,
                    IssueSeverity.ERROR,
                    f"network error ({type(exc).__name__})",
                )

    def _get_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("source is closed")
        if self._client is None:
            # A jar whose policy accepts no domain: Set-Cookie is never stored or sent.
            jar = http.cookiejar.CookieJar(
                policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            )
            self._client = httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(self._config.timeout_s),
                # identity: the max_bytes cap then bounds what is actually held in
                # memory. With gzip, each raw chunk is inflated whole before the cap
                # can apply, so a compression bomb could peak at ~150 MB (QA L2).
                headers={
                    "User-Agent": self._config.user_agent,
                    "Accept": ACCEPT,
                    "Accept-Encoding": "identity",
                },
                cookies=jar,
                event_hooks={"request": [_strip_credentials]},
            )
        return self._client

    # -- robots.txt -------------------------------------------------------- #
    async def _robots_policy(self, url: str, chain: _Chain) -> RobotsPolicy:
        """The cached policy for ``url``'s origin; a first-fetch issue is attached to ``chain``."""
        origin = _origin(url)
        policy = self._robots.get(origin)
        if policy is None:
            policy, issue = await self._fetch_robots(origin)
            self._robots[origin] = policy
            if issue is not None:
                chain.pre.append(issue)
        return policy

    async def _handle_robots(self, response: httpx.Response) -> _RobotsResponse:
        status = response.status_code
        if status in REDIRECT_STATUSES:
            return _RobotsResponse(status, response.headers.get("location"), b"")
        if not 200 <= status < 300:
            return _RobotsResponse(status, None, b"")
        if _is_encoded(response):
            # Fail closed: an encoded robots.txt is never inflated, and 502 makes the
            # origin disallowed rather than silently allow-all.
            return _RobotsResponse(502, None, b"")
        body = bytearray()
        async for chunk in _raw_chunks(response):
            body.extend(chunk)
            if len(body) >= MAX_ROBOTS_BYTES:
                break  # RFC 9309 2.5: parse at least 500 KiB, ignore the rest
        return _RobotsResponse(status, None, bytes(body[:MAX_ROBOTS_BYTES]))

    async def _fetch_robots(self, origin: str) -> tuple[RobotsPolicy, SourceIssue | None]:
        robots_url = origin + ROBOTS_PATH
        scope = self._scope()

        def unavailable(reason: str, status: int | None = None) -> tuple[RobotsPolicy, SourceIssue]:
            return disallow_all(), self._issue(
                IssueCode.ROBOTS_UNAVAILABLE,
                IssueSeverity.ERROR,
                f"robots.txt unavailable ({reason}); the origin is treated as disallowed",
                url=robots_url,
                http_status=_http_status(status) if status is not None else None,
            )

        url = robots_url
        for _hop in range(MAX_ROBOTS_REDIRECTS + 1):
            result = await self._request(url, self._config.delay_s, self._handle_robots)
            if isinstance(result, _Failure):
                return unavailable(result.code.value)
            status = result.status
            if 200 <= status < 300:
                rules = parse_robots(result.body.decode("utf-8", errors="replace"))
                return rules.policy_for(self._config.user_agent), None
            if status in REDIRECT_STATUSES:
                target = _normalize(result.location or "", url)
                if not result.location or target is None or not urls.in_scope(target, scope):
                    return unavailable("redirect outside the site", status)
                url = target
                continue
            # 4xx means "no robots.txt": allow all (RFC 9309) -- except 429, which
            # means "slow down", so it is treated like a 5xx: disallow (QA L4).
            if 400 <= status < 500 and status != 429 and not self._config.require_robots_file:
                return allow_all(), None
            return unavailable(f"HTTP {status}", status)
        return unavailable("too many redirects")

    async def robots_status(self, origin: str) -> tuple[RobotsPolicy, SourceIssue | None]:
        """The robots.txt policy for ``origin`` (one rate-limited fetch; no page is requested)."""
        return await self._fetch_robots(origin)

    # -- records ------------------------------------------------------------ #
    def _document(self, chain: _Chain, page: _Page) -> SourceDocument:
        return SourceDocument(
            source_type=SourceType.PUBLIC_WEB,
            source_id=chain.current,
            requested_url=chain.requested,
            final_url=chain.current,
            redirect_chain=tuple(chain.hops),
            http_status=page.status,
            content_type=page.content_type,
            content_language=page.content_language,
            fetched_at=self._clock(),
            depth=chain.depth,
            html=page.html,
        )

    def _issue(
        self,
        code: IssueCode,
        severity: IssueSeverity,
        message: str,
        *,
        url: str | None = None,
        related_url: str | None = None,
        http_status: int | None = None,
    ) -> SourceIssue:
        return SourceIssue(
            code=code,
            severity=severity,
            url=url,
            related_url=related_url,
            http_status=http_status,
            message=message,
            occurred_at=self._clock(),
        )

    def _discover_links(
        self, document: SourceDocument, scope: SiteScope, next_level: list[str]
    ) -> list[SourceIssue]:
        """Append followable in-scope links to ``next_level`` in document order."""
        facts = discover(document.html)
        if self._config.respect_meta_nofollow and facts.nofollow:
            return []
        base = document.final_url
        if facts.base_href:
            declared = _normalize(facts.base_href, document.final_url)
            if declared is not None and urls.in_scope(declared, scope):
                base = declared
        at_depth_limit = (document.depth or 0) >= self._config.max_depth
        cap = self._config.max_query_variants_per_path
        issues: list[SourceIssue] = []
        for href in facts.hrefs:
            target = _normalize(href, base)
            if target is None or not urls.in_scope(target, scope):
                continue
            self._discovered.add(target)
            if target in self._seen:
                continue
            self._seen.add(target)
            if at_depth_limit:
                self._skipped.add(target)
                continue
            if self._rule_reason(target) is not None:
                self._skipped.add(target)
                self._rule_skipped += 1
                continue
            key = urls.path_key(target)
            count = self._variants.get(key, 0)
            if count >= cap:
                self._skipped.add(target)
                if key not in self._variant_limited:
                    self._variant_limited.add(key)
                    issues.append(
                        self._issue(
                            IssueCode.LIMIT_REACHED,
                            IssueSeverity.INFO,
                            f"more than {cap} query variants of this path; extras skipped",
                            url=key,
                        )
                    )
                continue
            self._variants[key] = count + 1
            next_level.append(target)
        return issues
