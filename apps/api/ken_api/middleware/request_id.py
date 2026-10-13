"""Request-id correlation and access logging (pure ASGI middleware).

* An inbound ``X-Request-ID`` is honoured only if it matches a strict
  character allowlist. Anything else -- newlines, ANSI escapes, JSON
  fragments, oversized values -- is replaced by a fresh UUID4, because the
  value is written verbatim into every log line for the request (log
  injection).
* The id is placed in ``ken_core.logging.request_id_var`` (so every log line
  emitted while handling the request carries it), in ``request.state`` (so
  exception handlers outside this middleware can read it), and echoed in the
  response header.
* Exactly one structured access log line is emitted per request.

Pure ASGI rather than ``BaseHTTPMiddleware``: the latter runs the endpoint in
a separate task, which breaks contextvar propagation and buffers bodies.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ken_core.logging import get_logger, request_id_var

REQUEST_ID_HEADER: Final = "X-Request-ID"
_VALID_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_LOGGED_PATH: Final = 512

logger = get_logger("ken_api.access")


def resolve_request_id(inbound: str | None) -> str:
    """Return ``inbound`` if it is safe to log verbatim, else a new UUID4."""
    # fullmatch, not match(^...$): "$" also matches before a trailing newline.
    if inbound is not None and _VALID_REQUEST_ID.fullmatch(inbound):
        return inbound
    return str(uuid.uuid4())


def _loggable_path(path: str) -> str:
    """Escape control characters: the path is percent-DECODED, so it can carry them."""
    safe = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in path)
    return safe[:_MAX_LOGGED_PATH]


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        status_code = 500  # what the client sees if the app raises before responding
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "http_request",
                method=scope.get("method"),
                path=_loggable_path(scope.get("path", "")),
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
                request_id=request_id,
            )
            request_id_var.reset(token)
