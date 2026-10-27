"""Headless-browser page rendering for the public crawler (``CrawlConfig.render == "browser"``).

Why a browser: some pages build their content with JavaScript, and the plain HTTP fetch sees only
the empty shell. Playwright (TRD, Apache-2.0) renders the page so the DOM the reader would see is
what gets stored. The browser is a TRANSPORT only: every safety rule of the HTTP crawler still
applies, and in fact is enforced per request by route interception, because a browser on its own
would follow redirects, load third-party resources and fire a burst of requests.

What is enforced, for EVERY request the page makes:

* the destination passes the crawler's gate (https only, approved host, SSRF guard, robots.txt);
* the request is paced by the crawl's shared rate limiter (subresources are throttled like pages);
* redirects are NEVER followed by the browser: a 3xx on the document aborts the navigation and is
  returned as a redirect to the crawler's own hop-by-hop checks; a 3xx on a subresource is dropped;
* only the document and same-host ``script`` / ``xhr`` / ``fetch`` requests are allowed, at most
  ``max_subrequests`` per page; images, fonts, stylesheets, media, websockets and everything else
  is aborted, which also keeps the load on the site small;
* the context is fresh per page: no cookies, no storage, no service workers, no downloads, no
  permissions; the User-Agent is the crawler's honest one (no browser impersonation, no stealth).

Known limits: a browser cannot report the connected IP, so the DNS-rebinding peer check of the HTTP
path is replaced by the pre-request DNS check; a response body is buffered before the size cap
applies (bounded by the timeout and the declared Content-Length check).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = ["BrowserRenderer", "BrowserUnavailableError", "RenderResult"]

ALLOWED_SUBREQUEST_TYPES: Final = frozenset({"script", "xhr", "fetch"})
SETTLE_MS: Final = 3000

type Gate = Callable[[str, bool], Awaitable[str | None]]
"""``gate(url, is_document)`` -> a refusal reason, or ``None`` when the request may be made."""


class BrowserUnavailableError(RuntimeError):
    """Playwright or its Chromium build is not installed. The message is safe to print."""


@dataclass(frozen=True, slots=True)
class RenderResult:
    kind: str  # "page" | "redirect" | "failure"
    status: int = 0
    location: str | None = None
    html: str = ""
    content_type: str = ""
    content_language: str | None = None
    failure: str | None = None  # one of: refused, too_large, non_html, navigation, timeout


class BrowserRenderer:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_s: float,
        max_bytes: int,
        max_subrequests: int,
        gate: Gate,
        pace: Callable[[], Awaitable[None]],
        on_status: Callable[[int, str | None], None],
        screenshot_dir: Path | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._max_subrequests = max_subrequests
        self._gate = gate
        self._pace = pace
        self._on_status = on_status
        self._screenshot_dir = screenshot_dir
        self._playwright: Any = None
        self._browser: Any = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(
                "browser rendering needs the 'browser' extra: pip install 'playwright'"
            ) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # message names the fix, never the exception text
            await self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailableError(
                "Chromium is not installed: run `python -m playwright install chromium`"
            ) from exc

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def render(self, url: str) -> RenderResult:
        await self.start()
        state: dict[str, Any] = {"main": None, "redirect": None, "refused": None, "subs": 0}
        context = await self._browser.new_context(
            user_agent=self._user_agent,
            service_workers="block",
            accept_downloads=False,
            java_script_enabled=True,
            permissions=[],
            locale="en-US",
        )
        try:
            await context.route("**/*", lambda route: self._route(route, url, state))
            page = await context.new_page()
            page.set_default_timeout(self._timeout_s * 1000)
            try:
                async with asyncio.timeout(self._timeout_s):
                    await page.goto(url, wait_until="domcontentloaded")
                    if state["main"] is not None:
                        with contextlib.suppress(Exception):  # best effort settle, never fatal
                            await page.wait_for_load_state(
                                "networkidle", timeout=min(SETTLE_MS, self._timeout_s * 1000)
                            )
            except TimeoutError:
                return RenderResult("failure", failure="timeout")
            except Exception:
                # aborting the navigation to hand a redirect / refusal back raises here
                return self._aborted_result(state) or RenderResult("failure", failure="navigation")
            result = self._aborted_result(state, only_if_set=True)
            if result is not None:
                return result
            main = state["main"]
            if main is None:
                return RenderResult("failure", failure="navigation")
            if self._normalise(page.url) != self._normalise(url):
                return RenderResult("failure", failure="navigation")
            html = await page.content()
            if len(html.encode("utf-8", errors="ignore")) > self._max_bytes:
                return RenderResult("failure", status=main["status"], failure="too_large")
            if self._screenshot_dir is not None:
                self._screenshot_dir.mkdir(parents=True, exist_ok=True)
                name = hashlib.sha256(url.encode()).hexdigest()[:16] + ".png"
                await page.screenshot(path=str(self._screenshot_dir / name), full_page=True)
            return RenderResult(
                "page",
                status=main["status"],
                html=html,
                content_type=main["content_type"],
                content_language=main["language"],
            )
        finally:
            await context.close()

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _normalise(url: str) -> str:
        return url.split("#", 1)[0].rstrip("/")

    @staticmethod
    def _aborted_result(state: dict[str, Any], *, only_if_set: bool = False) -> RenderResult | None:
        if state["redirect"] is not None:
            status, location = state["redirect"]
            return RenderResult("redirect", status=status, location=location)
        if state["refused"] is not None:
            return RenderResult("failure", failure="refused", location=state["refused"])
        main = state["main"]
        if main is not None and main.get("no_render"):
            return RenderResult(
                "failure",
                status=main["status"],
                content_type=main["content_type"],
                failure=main["no_render"],
            )
        return None if only_if_set else RenderResult("failure", failure="navigation")

    async def _route(self, route: Any, document_url: str, state: dict[str, Any]) -> None:
        request = route.request
        url: str = request.url
        if request.resource_type == "document" and request.frame.parent_frame is not None:
            await route.abort("blockedbyclient")  # iframes: never loaded
            return
        is_document = request.is_navigation_request() and request.resource_type == "document"
        if not is_document:
            if request.resource_type not in ALLOWED_SUBREQUEST_TYPES:
                await route.abort("blockedbyclient")
                return
            if state["subs"] >= self._max_subrequests:
                await route.abort("blockedbyclient")
                return
            state["subs"] += 1
        reason = await self._gate(url, is_document)
        if reason is not None:
            if is_document:
                state["refused"] = reason
            await route.abort("blockedbyclient")
            return
        try:
            await self._pace()
        except RuntimeError:  # the crawl was aborted while this page was loading
            if is_document:
                state["refused"] = "crawl aborted"
            await route.abort("blockedbyclient")
            return
        try:
            response = await route.fetch(max_redirects=0, timeout=self._timeout_s * 1000)
        except Exception:
            if is_document:
                state["refused"] = "request failed"
            await route.abort("failed")
            return
        status: int = response.status
        headers = response.headers
        self._on_status(status, headers.get("retry-after"))
        if 300 <= status < 400:
            if is_document and status in (301, 302, 303, 307, 308):
                state["redirect"] = (status, headers.get("location"))
            await route.abort("blockedbyclient")  # never let the browser follow a hop itself
            return
        if is_document:
            content_type = headers.get("content-type", "")
            media = content_type.split(";", 1)[0].strip().lower()
            declared = headers.get("content-length")
            too_big = (
                declared is not None and declared.isdigit() and int(declared) > self._max_bytes
            )
            state["main"] = {
                "status": status,
                "content_type": media,
                "language": (headers.get("content-language") or "").strip() or None,
            }
            reason_code: str | None = None
            if too_big:
                reason_code = "too_large"
            elif not 200 <= status < 300:
                reason_code = "status"
            elif media not in {"text/html", "application/xhtml+xml"}:
                reason_code = "non_html"
            elif len(await response.body()) > self._max_bytes:
                reason_code = "too_large"
            if reason_code is not None:
                state["main"]["no_render"] = reason_code
                await route.abort("blockedbyclient")  # report the status, render nothing
                return
        await route.fulfill(response=response)
