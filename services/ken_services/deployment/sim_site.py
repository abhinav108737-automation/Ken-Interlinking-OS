"""Serve the simulated CMS's pages as a local website (``make cms-sim-serve``, ADR-0014 s4/s8).

The Phase 1 crawler re-reads it and deployment HTTP verification fetches it: the published
URL of a page is ``base + path`` (:meth:`ken_services.deployment.http_probe.HttpProbe
.published_url`), so pages are served BY PATH (``SimulatedCMSProvider.page_by_path``).

Routes (GET and HEAD only; anything else is 405):

* ``/robots.txt`` -- allow all;
* :data:`HEALTH_PATH` -- this server instance's random nonce (listener verification);
* ``<path>[?query]`` -- the page stored at that path, ``text/html``. A full document (it has
  an ``<html>`` element) is served byte for byte; a fragment (the inventory's normalised
  content root) is wrapped in a minimal document with ``<title>`` and
  ``<link rel="canonical" href="{page.url}">``;
* ``/`` when no page lives there -- an index linking every page by path, so a crawl from the
  base URL discovers the site (``noindex, follow``);
* anything else -- 404. A storage error is a 503 (no detail).

**Binding.** Loopback only (an IP literal), port 0 (an OS-assigned free port) on a socket
bound HERE, before the server starts; a fixed port is never requested. Port 8765 (the user's
own dashboard) is refused outright even if the OS returned it, as is port 0.

**Beacon (Phase 7, additive).** ``render_page(page, beacon_src=...)`` and
``SimulatedSite(..., beacon_src=...)`` inject
``<script src="{collector}/k.js" defer data-ken-collector="{collector}/e"></script>`` before
``</body>`` (or at the end of the document when there is none), so ``make
analytics-sim-serve`` can measure the simulated site with the first-party collector. With
``beacon_src=None`` -- the default -- the rendered bytes are IDENTICAL to Phase 6 (a test
asserts it). Injecting the beacon cannot change a content hash either: inventory
normalisation and the deployment source map both drop ``<script>`` (ADR-0015 §12.1), which a
parity test asserts. The index page is navigation, not content, and never carries the beacon.

**Listener verification** (:meth:`SimulatedSite.start`): (1) the pre-bound socket's
``getsockname()`` must be the requested loopback address with a non-zero port that is not
8765; (2) after uvicorn reports started, an HTTP GET of :data:`HEALTH_PATH` on that exact
address must answer 200 with the 128-bit random nonce generated for THIS instance -- so the
URL handed out is proven to be served by this process's server, not by anything else. Only
then is the base URL returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import re
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import httpx
import uvicorn

from ken_contracts.cms import CMSPage
from ken_core.logging import get_logger

__all__ = [
    "FORBIDDEN_PORTS",
    "HEALTH_PATH",
    "PageSource",
    "SimSiteError",
    "SimulatedSite",
    "SimulatedSiteApp",
    "beacon_tag",
    "render_page",
    "serve_simulated_site",
]

logger = get_logger("ken_services.deployment.sim_site")

HEALTH_PATH: Final = "/__ken_sim_site/health"
FORBIDDEN_PORTS: Final = frozenset({0, 8765})
"""0 means "not bound"; 8765 is the user's own dashboard app and is NEVER used."""
ROBOTS_TXT: Final = b"User-agent: *\nAllow: /\n"
_HTML_ELEMENT: Final = re.compile(r"<html[\s>]", re.IGNORECASE)
_BODY_END: Final = re.compile(r"</body\s*>", re.IGNORECASE)
_ALLOWED_METHODS: Final = ("GET", "HEAD")

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]


class SimSiteError(RuntimeError):
    """The site could not be started or its listener could not be verified (safe text)."""


class PageSource(Protocol):
    """What the site reads: ``SimulatedCMSProvider`` satisfies it."""

    async def page_by_path(self, path: str) -> CMSPage | None: ...

    async def list_pages(self) -> list[CMSPage]: ...


# --------------------------------------------------------------------------- #
# Rendering (pure)
# --------------------------------------------------------------------------- #
def beacon_tag(beacon_src: str) -> str:
    """The first-party beacon's script tag for the collector at ``beacon_src`` (its base URL).

    ``defer`` so it never blocks rendering, and ``data-ken-collector`` so the beacon knows
    where to post without a second request. The URL is escaped for a double-quoted attribute.
    """
    base = html.escape(beacon_src.rstrip("/"), quote=True)
    return f'<script src="{base}/k.js" defer data-ken-collector="{base}/e"></script>'


def _with_beacon(document: str, beacon_src: str) -> str:
    """``document`` with the beacon tag before its LAST ``</body>``, else appended."""
    tag = beacon_tag(beacon_src)
    match = None
    for found in _BODY_END.finditer(document):
        match = found
    if match is None:
        return document + tag + "\n"
    return document[: match.start()] + tag + "\n" + document[match.start() :]


def render_page(page: CMSPage, *, beacon_src: str | None = None) -> str:
    """The HTML served for ``page``: a full document verbatim, a fragment wrapped.

    ``beacon_src`` (the collector's base URL) injects the first-party beacon; ``None`` -- the
    default -- renders exactly the Phase 6 bytes.
    """
    if _HTML_ELEMENT.search(page.body_html):
        document = page.body_html
    else:
        title = html.escape(page.title, quote=False)
        canonical = html.escape(page.url, quote=True)
        document = (
            '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
            f"<title>{title}</title>\n"
            f'<link rel="canonical" href="{canonical}">\n'
            f"</head>\n<body>\n{page.body_html}\n</body>\n</html>\n"
        )
    return document if beacon_src is None else _with_beacon(document, beacon_src)


def _path_of(url: str) -> str:
    """The path a page is served at (mirrors the simulated provider's own mapping)."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def render_index(pages: list[CMSPage]) -> str:
    items = "".join(
        f'<li><a href="{html.escape(_path_of(p.url), quote=True)}">'
        f"{html.escape(p.title or _path_of(p.url), quote=False)}</a></li>\n"
        for p in pages
    )
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        '<meta name="robots" content="noindex, follow">\n'
        "<title>Simulated CMS</title>\n</head>\n<body>\n<main>\n"
        f"<h1>Simulated CMS</h1>\n<ul>\n{items}</ul>\n</main>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# ASGI application
# --------------------------------------------------------------------------- #
class SimulatedSiteApp:
    """A minimal ASGI app (no framework): GET/HEAD by path over a :class:`PageSource`."""

    def __init__(self, source: PageSource, *, nonce: str, beacon_src: str | None = None) -> None:
        self._source = source
        self._nonce = nonce.encode("ascii")
        self._beacon_src = beacon_src

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan is off; websockets are not served
            return
        method = str(scope.get("method", "GET")).upper()
        if method not in _ALLOWED_METHODS:
            await self._respond(
                send,
                method,
                405,
                b"method not allowed\n",
                "text/plain; charset=utf-8",
                extra=[(b"allow", b"GET, HEAD")],
            )
            return
        raw_path = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
        path = bytes(raw_path).decode("latin-1") or "/"
        query = bytes(scope.get("query_string") or b"").decode("latin-1")
        status, body, content_type = await self._route(path, query)
        await self._respond(send, method, status, body, content_type)

    async def _route(self, path: str, query: str) -> tuple[int, bytes, str]:
        text_plain = "text/plain; charset=utf-8"
        if path == "/robots.txt" and not query:
            return 200, ROBOTS_TXT, text_plain
        if path == HEALTH_PATH:
            return 200, self._nonce, text_plain
        key = f"{path}?{query}" if query else path
        try:
            page = await self._source.page_by_path(key)
            if page is None and key == "/":
                pages = await self._source.list_pages()
                return 200, render_index(pages).encode("utf-8"), "text/html; charset=utf-8"
        except Exception as exc:
            logger.warning("sim_site_storage_error", error_class=type(exc).__name__)
            return 503, b"simulated CMS unavailable\n", text_plain
        if page is None:
            return 404, b"not found\n", text_plain
        body = render_page(page, beacon_src=self._beacon_src)
        return 200, body.encode("utf-8"), "text/html; charset=utf-8"

    @staticmethod
    async def _respond(
        send: Send,
        method: str,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        headers = [
            (b"content-type", content_type.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            *(extra or []),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
def _loopback(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise SimSiteError("the simulated site binds a loopback IP literal only") from None
    if not address.is_loopback:
        raise SimSiteError("the simulated site binds a loopback IP literal only")
    return address


def _bind(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> socket.socket:
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((str(address), 0))  # port 0: the OS picks a free port; never a fixed one
        bound_host, bound_port = sock.getsockname()[:2]
        if ipaddress.ip_address(bound_host) != address:
            raise SimSiteError("the socket was not bound to the requested loopback address")
        if bound_port in FORBIDDEN_PORTS:
            raise SimSiteError(f"refusing to serve on port {bound_port}")
        sock.listen(128)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class SimulatedSite:
    """One verified local server over a :class:`PageSource`. Prefer
    :func:`serve_simulated_site`; the CLI uses this class to wait for an interrupt."""

    def __init__(
        self,
        source: PageSource,
        *,
        host: str = "127.0.0.1",
        startup_timeout_s: float = 10.0,
        beacon_src: str | None = None,
    ) -> None:
        self._address = _loopback(host)
        self._source = source
        self._timeout = startup_timeout_s
        self._beacon_src = beacon_src
        self._nonce = secrets.token_hex(16)
        self._sock: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.port: int | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise SimSiteError("the simulated site is not running")
        host = str(self._address)
        host = f"[{host}]" if self._address.version == 6 else host
        return f"http://{host}:{self.port}"

    async def start(self) -> str:
        """Bind, serve and VERIFY the listener (module docstring); return the base URL."""
        if self._task is not None:
            raise SimSiteError("the simulated site is already running")
        sock = _bind(self._address)
        self._sock = sock
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            SimulatedSiteApp(self._source, nonce=self._nonce, beacon_src=self._beacon_src),
            lifespan="off",
            # None/None: uvicorn changes NO global logging state (no dictConfig, no
            # setLevel on uvicorn.*, no handler removal); ken_core.logging owns it and
            # already silences uvicorn.access.
            log_config=None,
            log_level=None,
            server_header=False,
            proxy_headers=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        try:
            await self._wait_started()
            await self._verify()
        except BaseException:
            await self.stop()
            raise
        logger.info("sim_site_started", port=self.port)
        return self.base_url

    async def _wait_started(self) -> None:
        server = self._server
        task = self._task
        if server is None or task is None:  # pragma: no cover - start() sets both
            raise SimSiteError("the simulated site is not running")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while not server.started:
            if task.done():
                raise SimSiteError("the simulated site server exited during startup")
            if loop.time() > deadline:
                raise SimSiteError("the simulated site server did not start in time")
            await asyncio.sleep(0.01)

    async def _verify(self) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(self.base_url + HEALTH_PATH)
        except httpx.HTTPError as exc:
            raise SimSiteError(
                f"the listener could not be verified ({type(exc).__name__})"
            ) from None
        if response.status_code != 200 or response.content != self._nonce.encode("ascii"):
            raise SimSiteError("the listener answered, but it is not this server")

    async def wait(self) -> None:
        """Until the server exits (e.g. SIGINT handled by uvicorn)."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def stop(self) -> None:
        server, task, sock = self._server, self._task, self._sock
        self._server, self._task, self._sock = None, None, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
            except TimeoutError:
                if server is not None:
                    server.force_exit = True
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception as exc:
                logger.warning("sim_site_stop_error", error_class=type(exc).__name__)
        if sock is not None:
            sock.close()
        self.port = None


@asynccontextmanager
async def serve_simulated_site(
    source: PageSource,
    *,
    host: str = "127.0.0.1",
    startup_timeout_s: float = 10.0,
    beacon_src: str | None = None,
) -> AsyncIterator[str]:
    """Serve ``source`` on ``host`` (loopback) at an OS-assigned port; yield the VERIFIED
    base URL (``http://127.0.0.1:<port>``, no trailing slash); stop on exit."""
    site = SimulatedSite(
        source, host=host, startup_timeout_s=startup_timeout_s, beacon_src=beacon_src
    )
    base_url = await site.start()
    try:
        yield base_url
    finally:
        await site.stop()
