"""HTTP side of deployment checks (ADR-0014 s1.7, s3, s8): :class:`HttpProbe`.

Used ONLY when ``KEN_DEPLOY_VERIFY_BASE_URL`` is configured. A page's published URL is that
base plus the page's path and query (the simulated site, ``make cms-sim-serve``, serves the
simulated pages exactly so; for a real site the base is the site itself).

The Phase 1 crawler's guards apply (``ken_services.inventory.sources.public_web``): GET only;
no cookies, credentials or proxies (``trust_env=False``; ``Cookie`` / ``Authorization`` are
stripped by a request hook); a body cap; and SCOPE -- a URL whose host is not the base
URL's is refused before any I/O. Redirects are NOT followed: a 3xx is a deterministic
failure (no off-host redirect can be taken, and a redirecting target is not a clean link).

Outcomes: :attr:`FetchOutcome.OK` (200), :attr:`FetchOutcome.FAILED` (deterministic: 3xx,
4xx, out of scope), :attr:`FetchOutcome.INCONCLUSIVE` (transient: 5xx, timeout, network
error -- never fails a plan).
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

import httpx

from ken_services.inventory import urls

__all__ = ["FetchOutcome", "FetchResult", "HttpProbe"]

_FORBIDDEN_HEADERS: Final = ("cookie", "authorization", "proxy-authorization")
ACCEPT: Final = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"


class FetchOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class FetchResult:
    outcome: FetchOutcome
    status: int | None = None
    body: str | None = None
    detail: str = ""
    """Fixed vocabulary: ``http_<status>``, ``redirect``, ``out_of_scope``, ``timeout``,
    ``network_error``, ``too_large``."""


async def _strip_credentials(request: httpx.Request) -> None:
    for name in _FORBIDDEN_HEADERS:
        if name in request.headers:
            del request.headers[name]


class HttpProbe:
    """GET-only, scope-guarded fetches against the verification base URL."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        user_agent: str,
        max_bytes: int = 5_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = urls.normalize_url(base_url)
        self._base = normalized.rstrip("/")
        self._netloc = urls.url_netloc(normalized)
        self._timeout = timeout_s
        self._agent = user_agent
        self._max = max_bytes
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self._base + "/"

    def published_url(self, page_url: str) -> str:
        """The page's URL on the verification site (base + path + query)."""
        parts = urlsplit(page_url)
        path = parts.path or "/"
        return self._base + path + (f"?{parts.query}" if parts.query else "")

    def in_scope(self, url: str) -> bool:
        normalized = urls.try_normalize_url(url)
        return normalized is not None and urls.url_netloc(normalized) == self._netloc

    async def fetch(self, url: str) -> FetchResult:
        if not self.in_scope(url):
            return FetchResult(FetchOutcome.FAILED, detail="out_of_scope")
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    headers={"User-Agent": self._agent, "Accept": ACCEPT},
                    event_hooks={"request": [_strip_credentials]},
                ) as client,
                client.stream("GET", url) as response,
            ):
                status = response.status_code
                if 300 <= status < 400:
                    return FetchResult(FetchOutcome.FAILED, status=status, detail="redirect")
                if status >= 500:
                    return FetchResult(
                        FetchOutcome.INCONCLUSIVE, status=status, detail=f"http_{status}"
                    )
                if status != 200:
                    return FetchResult(FetchOutcome.FAILED, status=status, detail=f"http_{status}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max:
                        return FetchResult(
                            FetchOutcome.INCONCLUSIVE, status=status, detail="too_large"
                        )
                    chunks.append(chunk)
                body = _decode(b"".join(chunks), response.charset_encoding)
                return FetchResult(FetchOutcome.OK, status=status, body=body)
        except httpx.TimeoutException:
            return FetchResult(FetchOutcome.INCONCLUSIVE, detail="timeout")
        except httpx.HTTPError:
            return FetchResult(FetchOutcome.INCONCLUSIVE, detail="network_error")


def _decode(body: bytes, charset: str | None) -> str:
    if charset:
        try:
            codecs.lookup(charset)
            return body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            pass
    return body.decode("utf-8", errors="replace")
