"""The first-party collector ASGI application (ADR-0015 §1, hardened by §12.3 / §12.9).

A minimal ASGI app with no framework, built like
:mod:`ken_services.deployment.sim_site`, deliberately NOT part of the control API (whose
public routes stay exactly ``/health``, ``/ready`` and ``POST /auth/login``, ADR-0013):
untrusted browser traffic never sits next to the auth surface.

Routes
------
``GET /k.js``                      the beacon (``application/javascript``, ``nosniff``,
                                   ``max-age`` from policy);
``POST /e``                        events (always 204);
``GET /__ken_collector/health``    this instance's random nonce (listener verification);
anything else                      404, or 405 with ``Allow`` for a known path.

``GET`` routes also answer ``HEAD`` (no body). Everything else on a known path is a 405.

Validation order (binding; EVERY rejection still answers **204**, so the response can never
be used as a validity oracle, and reasons are counted in memory and logged every
``rejection_log_interval_seconds`` -- counts only, never a path, IP, User-Agent or body)
-----------------------------------------------------------------------------------------
1. method and route;
2. body cap on the bytes ACTUALLY STREAMED (never ``Content-Length``, which a client
   controls): a chunked body is abandoned as soon as it passes ``max_body_bytes``, and the
   whole read has a :data:`BODY_READ_TIMEOUT_SECONDS` deadline so that many tiny chunks
   under the cap cannot hold a handler open;
3. content type ``text/plain`` (what a CORS-simple ``sendBeacon`` sends);
4. ``Origin`` present and an EXACT scheme+host+port match against the allowed origins;
   ``null``, a missing header and a repeated header are all foreign;
5. bot and prefetch drop: the policy's User-Agent patterns (case-insensitively),
   ``Sec-Purpose`` / ``Purpose: prefetch``, and ``Sec-GPC: 1``;
6. rate limit: a bounded per-IP fixed window plus a global cap, keyed on the ASGI PEER IP
   only (uvicorn runs with ``proxy_headers=False``), never persisted
   (:mod:`~ken_services.analytics.collector.ratelimit`). Every request that reaches the
   limiter is charged one event immediately, so a malformed or unresolvable request is never
   free; a valid batch still costs exactly its event count;
7. the frozen wire schema (``extra=forbid``);
8. link resolution through the :class:`~ken_services.analytics.protocol.LiveLinkIndex`
   (attr and href rules are frozen in the protocol module);
9. insert with ``ON CONFLICT (dedupe_key) DO NOTHING``.

Pseudonymity (binding)
----------------------
A stored row is only (event type, plan, match mode, synthetic, UTC date, minute-truncated
``received_at``, dedupe key). The IP, User-Agent, ``pv``, referrer and query string exist
only in memory while the request is handled; ``synthetic`` comes from the collector's own
configuration, never from the client. First-party counts are nevertheless SPOOFABLE (both
``Origin`` and ``pv`` are attacker-chosen), so Phase 9 must never graduate a decision class
on first-party data alone.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import ValidationError

from ken_contracts.analytics import LinkEventType
from ken_core.config import is_loopback_host, parse_origin
from ken_core.logging import get_logger
from ken_services.analytics.collector.beacon import BEACON_PATH, BeaconAsset, build_beacon
from ken_services.analytics.collector.ratelimit import FixedWindowRateLimiter
from ken_services.analytics.collector.sink import LinkEventSink, NewLinkEvent, truncate_to_minute
from ken_services.analytics.protocol import (
    WIRE_EVENT_TYPES,
    AttrEvent,
    CollectorConfig,
    CollectorPayload,
    CollectorRejection,
    HrefEvent,
    LinkResolution,
    LiveLinkIndex,
    dedupe_key,
    normalized_path,
)

__all__ = [
    "BODY_READ_TIMEOUT_SECONDS",
    "EVENTS_PATH",
    "HEALTH_PATH",
    "CollectorApp",
    "CollectorConfigError",
    "RejectionCounter",
]

logger = get_logger("ken_services.analytics.collector")

EVENTS_PATH: Final = "/e"
HEALTH_PATH: Final = "/__ken_collector/health"
BODY_READ_TIMEOUT_SECONDS: Final = 5.0
"""Deadline for reading one request body. The 4 KiB cap bounds the SIZE of a body, not the
TIME a sender may take over it, and the policy document has no field for this (the analytics
policy schema is frozen), so the bound lives here."""
_MINIMUM_CHARGE: Final = 1
"""What every request reaching the rate limiter costs, valid or not (§12.10.11)."""
_ALLOWED_CONTENT_TYPE: Final = "text/plain"
_GET_METHODS: Final = ("GET", "HEAD")
_PREFETCH_HEADERS: Final = ("sec-purpose", "purpose")

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]


class CollectorConfigError(RuntimeError):
    """The collector configuration is refused (safe text; never echoes a secret)."""


class _BodyOversizeError(Exception):
    """The streamed body passed the cap; the rest was abandoned."""


class _DisconnectedError(Exception):
    """The client went away before the body was complete."""


class RejectionCounter:
    """In-memory rejection counts, logged at most every ``interval_s``.

    Counts ONLY: a reason and a number. No path, IP, User-Agent, origin or body ever reaches
    a log line from here.
    """

    def __init__(self, *, interval_s: float, clock: Callable[[], float] = monotonic) -> None:
        self._interval = interval_s
        self._clock = clock
        self._counts: dict[str, int] = {}
        self._accepted = 0
        self._last = clock()

    @property
    def counts(self) -> Mapping[str, int]:
        return dict(self._counts)

    @property
    def accepted(self) -> int:
        return self._accepted

    def reject(self, reason: CollectorRejection, count: int = 1) -> None:
        self._counts[reason.value] = self._counts.get(reason.value, 0) + count

    def accept(self, count: int = 1) -> None:
        self._accepted += count

    def maybe_log(self) -> None:
        if self._clock() - self._last < self._interval:
            return
        self.log()

    def log(self) -> None:
        self._last = self._clock()
        if not self._counts and not self._accepted:
            return
        logger.info("collector_counters", accepted=self._accepted, **self._counts)
        self._counts.clear()
        self._accepted = 0


def _headers(scope: Scope) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw_name, raw_value in scope.get("headers") or ():
        name = bytes(raw_name).decode("latin-1").lower()
        out.setdefault(name, []).append(bytes(raw_value).decode("latin-1"))
    return out


def _one(headers: Mapping[str, list[str]], name: str) -> str | None:
    """The single value of ``name``; ``None`` when absent or repeated."""
    values = headers.get(name)
    if values is None or len(values) != 1:
        return None
    return values[0]


def _path_of(scope: Scope) -> str:
    raw_path = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
    return bytes(raw_path).decode("latin-1") or "/"


def _peer_ip(scope: Scope) -> str:
    """The ASGI peer address only -- never ``X-Forwarded-For`` or any other header."""
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return ""


async def _read_capped(receive: Receive, cap: int) -> bytes:
    """The request body, refusing as soon as the STREAMED bytes pass ``cap``.

    The caller wraps this in :func:`asyncio.timeout`: the size cap alone does not bound a
    sender that dribbles many tiny chunks, which would otherwise hold this coroutine open
    for as long as it likes.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            raise _DisconnectedError
        body = bytes(message.get("body") or b"")
        total += len(body)
        if total > cap:
            raise _BodyOversizeError  # the remaining chunks are never read
        chunks.append(body)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _is_text_plain(headers: Mapping[str, list[str]]) -> bool:
    value = _one(headers, "content-type")
    if value is None:
        return False
    media, _, params = value.partition(";")
    if media.strip().lower() != _ALLOWED_CONTENT_TYPE:
        return False
    for param in params.split(";"):
        key, _, raw = param.partition("=")
        if key.strip().lower() == "charset" and raw.strip().strip('"').lower() not in {
            "utf-8",
            "utf8",
        }:
            return False
    return True


class CollectorApp:
    """The collector. One instance per process; its ``nonce`` proves its own listener."""

    def __init__(
        self,
        *,
        config: CollectorConfig,
        index: LiveLinkIndex,
        sink: LinkEventSink,
        nonce: str | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        body_read_timeout: float = BODY_READ_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._index = index
        self._sink = sink
        self._now = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._body_timeout = body_read_timeout
        self._origins: set[str] = set()
        for origin in config.allowed_origins:
            self._add_origin(origin)
        self._synthetic = self._compute_synthetic()
        self._bots = tuple(re.compile(p, re.IGNORECASE) for p in config.bot_ua_patterns)
        self._limiter = FixedWindowRateLimiter(config.limits.rate_limit)
        self._counters = RejectionCounter(
            interval_s=config.limits.rejection_log_interval_seconds, clock=self._monotonic
        )
        self._beacon: BeaconAsset = build_beacon(config.limits)
        self.nonce = nonce or secrets.token_hex(16)

    # ------------------------------------------------------------------ state
    @property
    def config(self) -> CollectorConfig:
        return self._config

    @property
    def index(self) -> LiveLinkIndex:
        return self._index

    @property
    def counters(self) -> RejectionCounter:
        return self._counters

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(sorted(self._origins))

    @property
    def synthetic(self) -> bool:
        """What is stamped on every stored event: true while every allowed origin is loopback
        (ADR-0015 §12.6.5). The client can never set it."""
        return self._synthetic

    def _compute_synthetic(self) -> bool:
        return all(is_loopback_host(urlsplit(origin).hostname) for origin in self._origins)

    def _add_origin(self, origin: str) -> str:
        try:
            normalised = parse_origin(origin)
        except ValueError as exc:
            raise CollectorConfigError("an allowed collector origin is not a bare origin") from exc
        if not is_loopback_host(urlsplit(normalised).hostname) and not self._config.allow_remote:
            raise CollectorConfigError(
                "a non-loopback collector origin requires KEN_COLLECTOR_ALLOW_REMOTE=true"
            )
        self._origins.add(normalised)
        return normalised

    def add_allowed_origin(self, origin: str) -> str:
        """Allow one more exact origin (``make analytics-sim-serve`` adds the simulated site,
        whose port only exists once it is bound). Returns the normalised origin."""
        normalised = self._add_origin(origin)
        self._synthetic = self._compute_synthetic()
        return normalised

    # -------------------------------------------------------------- ASGI entry
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan is off; websockets are not served
            return
        self._counters.maybe_log()
        method = str(scope.get("method", "GET")).upper()
        path = _path_of(scope)
        if path == EVENTS_PATH:
            if method != "POST":
                await self._method_not_allowed(send, method, b"POST")
                return
            await self._events(scope, receive, send)
            return
        if path in (BEACON_PATH, HEALTH_PATH):
            if method not in _GET_METHODS:
                await self._method_not_allowed(send, method, b"GET, HEAD")
                return
            if path == BEACON_PATH:
                await _respond(send, method, 200, self._beacon.body, self._beacon.headers)
                return
            await _respond(send, method, 200, self.nonce.encode("ascii"), _TEXT_HEADERS)
            return
        self._counters.reject(CollectorRejection.NOT_FOUND)
        await _respond(send, method, 404, b"not found\n", _TEXT_HEADERS)

    async def _method_not_allowed(self, send: Send, method: str, allow: bytes) -> None:
        self._counters.reject(CollectorRejection.METHOD_NOT_ALLOWED)
        await _respond(
            send,
            method,
            405,
            b"method not allowed\n",
            (*_TEXT_HEADERS, (b"allow", allow)),
        )

    # ---------------------------------------------------------------- POST /e
    async def _events(self, scope: Scope, receive: Receive, send: Send) -> None:
        reason = await self._validate(scope, receive)
        if reason is not None:
            self._counters.reject(reason)
        await _no_content(send)

    async def _validate(self, scope: Scope, receive: Receive) -> CollectorRejection | None:
        """Run the ordered checks and store what survives. The caller answers 204 regardless.

        Returns the rejection reason of a request dropped as a whole, or ``None`` when the
        request was processed (per-event rejections are counted here).
        """
        headers = _headers(scope)
        try:
            async with asyncio.timeout(self._body_timeout):
                body = await _read_capped(receive, self._config.limits.max_body_bytes)
        except _BodyOversizeError:
            return CollectorRejection.BODY_TOO_LARGE
        except _DisconnectedError:
            return None
        except TimeoutError:
            # A body that never arrives is treated exactly like a disconnect: nothing is
            # stored and the answer is still a bare 204. Logged (counts only) because the
            # frozen CollectorRejection has no member for it.
            logger.info("collector_body_read_timeout")
            return None
        if not _is_text_plain(headers):
            return CollectorRejection.BAD_CONTENT_TYPE
        if not self._origin_allowed(headers):
            return CollectorRejection.ORIGIN_NOT_ALLOWED
        privacy = self._privacy_drop(headers)
        if privacy is not None:
            return privacy
        ip = _peer_ip(scope)
        now_monotonic = self._monotonic()
        if not self._limiter.allows(ip, now=now_monotonic):
            return CollectorRejection.RATE_LIMITED
        # Every request that gets this far costs at least one event, BEFORE its body is
        # parsed: otherwise a peer could send unlimited malformed bodies for free, since the
        # charge used to depend on a payload that never validated. A valid batch still costs
        # exactly its event count (the +1 here plus len(events) - 1 below).
        self._limiter.charge(ip, _MINIMUM_CHARGE, now=now_monotonic)
        try:
            payload = CollectorPayload.model_validate_json(body)
        except ValidationError:
            return CollectorRejection.INVALID_SCHEMA
        path = normalized_path(payload.path)
        if path is None:
            return CollectorRejection.INVALID_SCHEMA
        self._limiter.charge(ip, len(payload.events) - _MINIMUM_CHARGE, now=now_monotonic)
        await self._store(payload, path)
        return None

    def _origin_allowed(self, headers: Mapping[str, list[str]]) -> bool:
        """EXACT scheme+host+port match. A missing, repeated or ``null`` Origin is foreign."""
        origin = _one(headers, "origin")
        if origin is None:
            return False
        candidate = origin.strip()
        return candidate in self._origins or candidate.lower() in self._origins

    def _privacy_drop(self, headers: Mapping[str, list[str]]) -> CollectorRejection | None:
        """Step 5, in the ADR's own order: bot User-Agent, then prefetch, then GPC."""
        agent = " ".join(headers.get("user-agent", ()))
        if agent and any(pattern.search(agent) for pattern in self._bots):
            return CollectorRejection.BOT
        for name in _PREFETCH_HEADERS:
            for value in headers.get(name, ()):
                if "prefetch" in value.lower():
                    return CollectorRejection.PREFETCH
        for value in headers.get("sec-gpc", ()):
            if value.strip() == "1":
                return CollectorRejection.GPC
        return None

    async def _store(self, payload: CollectorPayload, path: str) -> None:
        received_at = truncate_to_minute(self._now())
        rows: dict[str, NewLinkEvent] = {}
        for event in payload.events:
            resolution = self._resolve(event, path)
            if resolution.rejection is not None or resolution.plan_id is None:
                self._counters.reject(resolution.rejection or CollectorRejection.LINK_NOT_LIVE)
                continue
            key = dedupe_key(payload.pv, event.t, resolution.plan_id)
            if key in rows:
                self._counters.reject(CollectorRejection.DUPLICATE)
                continue
            rows[key] = NewLinkEvent(
                event_type=LinkEventType(WIRE_EVENT_TYPES[event.t]),
                plan_id=resolution.plan_id,
                match_mode=resolution.match_mode,
                synthetic=self._synthetic,
                received_at=received_at,
                dedupe_key=key,
            )
        if not rows:
            return
        try:
            stored = await self._sink.store(list(rows.values()))
        except Exception as exc:  # a storage failure is never a browser's business
            logger.warning("collector_store_failed", error_class=type(exc).__name__)
            return
        self._counters.accept(stored)
        if stored < len(rows):
            self._counters.reject(CollectorRejection.DUPLICATE, len(rows) - stored)

    def _resolve(self, event: AttrEvent | HrefEvent, path: str) -> LinkResolution:
        if isinstance(event, AttrEvent):
            return self._index.resolve_attr(plan_id=event.l, path=path)
        return self._index.resolve_href(path=path, href=event.h, anchor_text=event.a)


_TEXT_HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
)


async def _respond(
    send: Send,
    method: str,
    status: int,
    body: bytes,
    headers: Iterable[tuple[bytes, bytes]],
) -> None:
    out: list[tuple[bytes, bytes]] = list(headers)
    if not any(name == b"content-length" for name, _ in out):
        out.append((b"content-length", str(len(body)).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": out})
    await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})


async def _no_content(send: Send) -> None:
    """The ONLY answer ``POST /e`` ever gives: 204, no body, no cache (never an oracle)."""
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")],
        }
    )
    await send({"type": "http.response.body", "body": b""})
