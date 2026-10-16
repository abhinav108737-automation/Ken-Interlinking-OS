"""Running the collector: bind, verify, refresh (ADR-0015 §1, binding §12.9.5).

**Binding** mirrors :mod:`ken_services.deployment.sim_site` exactly:

* an IP literal, LOOPBACK unless ``KEN_COLLECTOR_ALLOW_REMOTE=true`` (which is an ASK-level
  setting; a non-loopback allowed ORIGIN needs it too, §12.9.5);
* **port 0** on a socket bound here, before uvicorn starts: a fixed port is never requested;
* **port 8765 is refused outright** even if the OS handed it back -- it is the user's own
  dashboard -- and so is port 0 ("not bound");
* **listener verification**: the pre-bound socket's ``getsockname()`` must be the requested
  address, and after uvicorn reports started an HTTP GET of
  :data:`~ken_services.analytics.collector.app.HEALTH_PATH` on that exact address must answer
  200 with the 128-bit nonce generated for THIS instance. Only then is the URL returned, so
  the URL handed out is proven to be served by this process.

``proxy_headers=False``: the client IP the rate limiter sees is the real peer, never a
forwarded header. ``server_header=False`` and uvicorn's ``log_config=None`` /
``log_level=None`` keep it from touching global logging state.

While the server runs, the live-link index is refreshed every
``collector.live_index_refresh_seconds``; a failed refresh keeps the previous snapshot.

Deploying this collector to a real site is an ASK-level action (ADR-0015 §12.8): locally it
runs only as part of ``make analytics-sim-serve``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Final

import httpx
import uvicorn

from ken_core.logging import get_logger
from ken_services.analytics.collector.app import HEALTH_PATH, CollectorApp
from ken_services.deployment.sim_site import FORBIDDEN_PORTS

__all__ = ["CollectorError", "CollectorServer", "serve_collector"]

logger = get_logger("ken_services.analytics.collector.server")

_BACKLOG: Final = 128

type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class CollectorError(RuntimeError):
    """The collector could not be started or its listener could not be verified."""


def _address_of(host: str, *, allow_remote: bool) -> _Address:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise CollectorError("the collector binds an IP literal only") from None
    if not address.is_loopback and not allow_remote:
        raise CollectorError(
            "the collector binds a loopback IP literal unless KEN_COLLECTOR_ALLOW_REMOTE=true"
        )
    return address


def _bind(address: _Address) -> socket.socket:
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((str(address), 0))  # port 0: the OS picks a free port; never a fixed one
        bound_host, bound_port = sock.getsockname()[:2]
        if ipaddress.ip_address(bound_host) != address:
            raise CollectorError("the socket was not bound to the requested address")
        if bound_port in FORBIDDEN_PORTS:
            raise CollectorError(f"refusing to serve on port {bound_port}")
        sock.listen(_BACKLOG)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class CollectorServer:
    """One verified local collector listener plus its live-index refresh loop."""

    def __init__(self, app: CollectorApp, *, startup_timeout_s: float = 10.0) -> None:
        self._app = app
        self._address = _address_of(app.config.host, allow_remote=app.config.allow_remote)
        self._timeout = startup_timeout_s
        self._refresh_interval = float(app.config.limits.live_index_refresh_seconds)
        self._sock: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._refresher: asyncio.Task[None] | None = None
        self.port: int | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise CollectorError("the collector is not running")
        host = str(self._address)
        host = f"[{host}]" if self._address.version == 6 else host
        return f"http://{host}:{self.port}"

    async def start(self) -> str:
        """Bind, serve, VERIFY the listener and start refreshing; return the base URL."""
        if self._task is not None:
            raise CollectorError("the collector is already running")
        sock = _bind(self._address)
        self._sock = sock
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            self._app,
            lifespan="off",
            # None/None: uvicorn changes NO global logging state; ken_core.logging owns it.
            log_config=None,
            log_level=None,
            server_header=False,
            proxy_headers=False,  # the peer IP is the only client identity (§12.3)
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        try:
            await self._wait_started()
            await self._verify()
            await self._app.index.refresh()
            self._refresher = asyncio.create_task(self._refresh_loop())
        except BaseException:
            await self.stop()
            raise
        logger.info("collector_started", port=self.port, synthetic=self._app.synthetic)
        return self.base_url

    async def _wait_started(self) -> None:
        server, task = self._server, self._task
        if server is None or task is None:  # pragma: no cover - start() sets both
            raise CollectorError("the collector is not running")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while not server.started:
            if task.done():
                raise CollectorError("the collector server exited during startup")
            if loop.time() > deadline:
                raise CollectorError("the collector server did not start in time")
            await asyncio.sleep(0.01)

    async def _verify(self) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(self.base_url + HEALTH_PATH)
        except httpx.HTTPError as exc:
            raise CollectorError(
                f"the listener could not be verified ({type(exc).__name__})"
            ) from None
        if response.status_code != 200 or response.text != self._app.nonce:
            raise CollectorError("the listener answered, but it is not this collector")

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval)
            await self._app.index.refresh()

    async def wait(self) -> None:
        """Until the server exits (e.g. SIGINT handled by uvicorn)."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def stop(self) -> None:
        server, task, sock = self._server, self._task, self._sock
        refresher = self._refresher
        self._server, self._task, self._sock, self._refresher = None, None, None, None
        if refresher is not None:
            refresher.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await refresher
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
                logger.warning("collector_stop_error", error_class=type(exc).__name__)
        if sock is not None:
            sock.close()
        self._app.counters.log()
        self.port = None


@asynccontextmanager
async def serve_collector(
    app: CollectorApp, *, startup_timeout_s: float = 10.0
) -> AsyncIterator[CollectorServer]:
    """Run ``app`` on its configured host at an OS-assigned port; yield the VERIFIED server."""
    server = CollectorServer(app, startup_timeout_s=startup_timeout_s)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
