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
import http.cookiejar
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

ACCEPT: Final = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
HTML_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
MAX_CRAWL_DELAY_S: Final = 60.0
MAX_ROBOTS_REDIRECTS: Final = 5
_MAX_CONTENT_LANGUAGE: Final = 100
_FORBIDDEN_HEADERS: Final = ("cookie", "authorization", "proxy-authorization")


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

    async def wait(self, gap: float) -> None:
        async with self._lock:
            now = self._monotonic()
            if self._last is not None:
                due = self._last + gap
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
    ) -> None:
        self._config = config
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
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    async def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        if self._started:
            raise RuntimeError("iter_records() can be consumed only once")
        self._started = True
        scope = self._scope()
        seed = urls.normalize_url(self._config.base_url)
        self._discovered.add(seed)
        self._seen.add(seed)
        self._variants[urls.path_key(seed)] = 1

        level = [seed]
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

        if self._page_limited:
            yield self._issue(
                IssueCode.LIMIT_REACHED,
                IssueSeverity.INFO,
                f"page request limit ({self._config.max_pages}) reached; "
                f"{self._page_limited} discovered URLs were not requested",
            )

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

    def _gap_for(self, url: str) -> float:
        policy = self._robots.get(_origin(url))
        crawl_delay = policy.crawl_delay if policy is not None else None
        gap = self._config.delay_s
        if crawl_delay is not None:
            gap = max(gap, min(crawl_delay, MAX_CRAWL_DELAY_S))
        return gap

    async def _fetch_page(self, url: str) -> _PageResult:
        return await self._request(url, self._gap_for(url), self._handle_page)

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
            await self._limiter.wait(gap)
            try:
                async with asyncio.timeout(self._config.timeout_s):
                    response = await client.send(request, stream=True)
                    try:
                        return await handle(response)
                    finally:
                        await response.aclose()
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
            if 400 <= status < 500 and status != 429:
                return allow_all(), None
            return unavailable(f"HTTP {status}", status)
        return unavailable("too many redirects")

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
