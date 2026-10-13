"""A tiny threaded HTTP fixture server bound to 127.0.0.1 on an ephemeral port.

Used by crawler tests instead of any real website. Routes map a request target
(path plus query, exactly as sent) to ``(status, headers, body)`` or
``(status, headers, body, delay_s)``. Status ``0`` closes the connection without
any response (a protocol-level network error). Unknown targets answer 404. Every request
is logged with its method, target and headers so tests can assert GET-only,
the User-Agent, and the absence of Cookie / Authorization.

    with LocalSite({"/": (200, {"Content-Type": "text/html"}, "<a href=/a>a</a>")}) as site:
        site.url("/")  # http://127.0.0.1:<port>/
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

type RouteSpec = (
    tuple[int, Mapping[str, str], bytes | str] | tuple[int, Mapping[str, str], bytes | str, float]
)

HTML: Mapping[str, str] = {"Content-Type": "text/html; charset=utf-8"}


@dataclass(frozen=True)
class LoggedRequest:
    method: str
    target: str
    headers: dict[str, str]
    """Header names lower-cased."""
    received_at: float
    """``time.monotonic()`` when the server parsed the request."""


def html_page(*hrefs: str, extra: str = "") -> str:
    links = "".join(f'<a href="{href}">{href}</a>' for href in hrefs)
    return f"<html><head><title>t</title>{extra}</head><body>{links}</body></html>"


class LocalSite:
    """Context manager running a ThreadingHTTPServer on 127.0.0.1:0."""

    def __init__(self, routes: Mapping[str, RouteSpec]) -> None:
        self.routes: dict[str, RouteSpec] = dict(routes)
        self.requests: list[LoggedRequest] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- addressing --------------------------------------------------------
    @property
    def netloc(self) -> str:
        assert self._server is not None, "server not started"
        host, port = self._server.server_address[:2]
        return f"{host!s}:{port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.netloc}/"

    def url(self, target: str) -> str:
        return f"http://{self.netloc}{target}"

    def targets(self, method: str | None = None) -> list[str]:
        with self._lock:
            return [r.target for r in self.requests if method is None or r.method == method]

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> LocalSite:
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _log(self) -> None:
                logged = LoggedRequest(
                    method=self.command,
                    target=self.path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    received_at=time.monotonic(),
                )
                with site._lock:
                    site.requests.append(logged)

            def do_GET(self) -> None:
                self._log()
                spec = site.routes.get(self.path)
                if spec is None:
                    self._send(404, {"Content-Type": "text/plain"}, b"not found")
                    return
                status, headers, body = spec[0], spec[1], spec[2]
                delay = spec[3] if len(spec) == 4 else 0.0
                if delay:
                    time.sleep(delay)
                if status == 0:
                    self.close_connection = True
                    return
                self._send(status, headers, body.encode() if isinstance(body, str) else body)

            def _reject(self) -> None:
                self._log()
                self._send(405, {"Content-Type": "text/plain"}, b"method not allowed")

            def do_HEAD(self) -> None:
                self._reject()

            def do_POST(self) -> None:
                self._reject()

            def do_PUT(self) -> None:
                self._reject()

            def do_DELETE(self) -> None:
                self._reject()

            def _send(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
                try:
                    self.send_response(status)
                    names = {name.lower() for name in headers}
                    for name, value in headers.items():
                        self.send_header(name, value)
                    if "content-length" not in names:
                        self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (timeout tests)

            def log_message(self, *args: Any) -> None:
                return  # keep test output quiet

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.block_on_close = False
        server.handle_error = lambda request, client_address: None  # type: ignore[method-assign]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
