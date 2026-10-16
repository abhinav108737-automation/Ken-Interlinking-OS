"""Helpers for the first-party collector tests: an ASGI driver, a fake sink, live plans.

The LIVE INDEX is the real :class:`LiveLinkSnapshot` (so the frozen attr/href rules are what
the tests exercise); only the database and the clock are replaced.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from ken_contracts.analytics import AnalyticsPolicy, CollectorPolicy
from ken_services.analytics.collector.app import BODY_READ_TIMEOUT_SECONDS, CollectorApp
from ken_services.analytics.collector.index import LiveLinkSnapshot, LivePlan
from ken_services.analytics.collector.sink import NewLinkEvent
from ken_services.analytics.policy import load_configured_analytics_policy
from ken_services.analytics.protocol import CollectorConfig, LiveLinkIndex, normalized_path
from tests.conftest import make_settings

SITE: Final = "https://site.test"
ORIGIN: Final = "http://127.0.0.1:4100"
"""A LOOPBACK origin, so no test needs KEN_COLLECTOR_ALLOW_REMOTE."""
NOW: Final = datetime(2026, 10, 15, 11, 42, 37, 123456, tzinfo=UTC)
UA: Final = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15"
PEER: Final = "127.0.0.9"


def policy() -> AnalyticsPolicy:
    """The released analytics policy (every collector limit comes from it)."""
    return load_configured_analytics_policy(make_settings())


def config(
    *,
    origins: tuple[str, ...] = (ORIGIN,),
    allow_remote: bool = False,
    host: str = "127.0.0.1",
    limits: CollectorPolicy | None = None,
) -> CollectorConfig:
    loaded = policy()
    return CollectorConfig(
        host=host,
        allowed_origins=origins,
        allow_remote=allow_remote,
        synthetic=all(o.startswith("http://127.") for o in origins),
        limits=limits if limits is not None else loaded.collector,
        bot_ua_patterns=loaded.bot_ua_patterns,
        policy_version=loaded.version,
    )


def plan_id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def live_plan(
    n: int,
    *,
    path: str = "/reports/a",
    anchor: str = "battery storage",
    hrefs: tuple[str, ...] = (f"{SITE}/insights/b",),
) -> LivePlan:
    source_url = f"{SITE}{path}"
    normalised = normalized_path(source_url)
    assert normalised is not None
    return LivePlan(
        plan_id=plan_id(n),
        source_url=source_url,
        path=normalised,
        anchor_text=anchor,
        hrefs=hrefs,
    )


class FakeSink:
    """Remembers what it was asked to store and which dedupe keys it has already seen."""

    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[NewLinkEvent]] = []
        self.rows: list[NewLinkEvent] = []
        self.keys: set[str] = set()
        self.fail = fail

    @property
    def stored(self) -> list[NewLinkEvent]:
        """The rows that were actually INSERTED (``ON CONFLICT DO NOTHING`` semantics)."""
        return list(self.rows)

    @property
    def attempted(self) -> list[NewLinkEvent]:
        return [event for batch in self.batches for event in batch]

    async def store(self, events: Sequence[NewLinkEvent]) -> int:
        if self.fail:
            raise ConnectionError("database down")
        self.batches.append(list(events))
        new = [event for event in events if event.dedupe_key not in self.keys]
        self.keys.update(event.dedupe_key for event in new)
        self.rows.extend(new)
        return len(new)


class Clock:
    """A settable UTC clock (so minute truncation is observable)."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Ticker:
    """A settable monotonic clock for the rate limiter and the rejection log interval."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def build_app(
    plans: Iterable[LivePlan] = (),
    *,
    sink: Any = None,
    index: LiveLinkIndex | None = None,
    collector_config: CollectorConfig | None = None,
    clock: Clock | None = None,
    ticker: Ticker | None = None,
    body_read_timeout: float = BODY_READ_TIMEOUT_SECONDS,
) -> tuple[CollectorApp, Any]:
    """A collector app over the REAL resolution rules; ``index``/``sink`` override the fakes."""
    events = sink if sink is not None else FakeSink()
    app = CollectorApp(
        config=collector_config or config(),
        index=index if index is not None else LiveLinkSnapshot(plans),
        sink=events,
        nonce="a" * 32,
        clock=clock or Clock(),
        monotonic_clock=ticker or Ticker(),
        body_read_timeout=body_read_timeout,
    )
    return app, events


# --------------------------------------------------------------------------- #
# A tiny ASGI driver (no server, no sockets)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class _Receiver:
    chunks: list[bytes]
    disconnect: bool = False
    reads: int = field(default=0)
    endless: bool = False
    """Dribble one tiny chunk per read, for ever: a body under the size cap that never ends."""
    delay: float = 0.0
    """Seconds awaited per read, so an endless body gives the event loop a chance to time out."""

    async def __call__(self) -> dict[str, Any]:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.endless:
            self.reads += 1
            return {"type": "http.request", "body": b".", "more_body": True}
        if self.reads >= len(self.chunks):
            if self.disconnect:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = self.chunks[self.reads]
        self.reads += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": self.reads < len(self.chunks) or self.disconnect,
        }


async def call(
    app: CollectorApp,
    *,
    method: str = "POST",
    path: str = "/e",
    body: bytes | None = None,
    chunks: Sequence[bytes] | None = None,
    origin: str | None = ORIGIN,
    content_type: str | None = "text/plain",
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
    user_agent: str | None = UA,
    peer: str | None = PEER,
    disconnect: bool = False,
    endless: bool = False,
    chunk_delay: float = 0.0,
) -> tuple[Response, _Receiver]:
    """Drive ``app`` once; return the response and the receiver (to assert what was read)."""
    headers: list[tuple[bytes, bytes]] = []
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("latin-1")))
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    if user_agent is not None:
        headers.append((b"user-agent", user_agent.encode("latin-1")))
    headers.extend(extra_headers)
    parts = list(chunks) if chunks is not None else [body if body is not None else b""]
    receiver = _Receiver(chunks=parts, disconnect=disconnect, endless=endless, delay=chunk_delay)
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "client": (peer, 54321) if peer is not None else None,
        "server": ("127.0.0.1", 4100),
        "scheme": "http",
    }
    status = 0
    out_headers: dict[str, str] = {}
    chunks_out: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
            for name, value in message["headers"]:
                out_headers[bytes(name).decode("latin-1").lower()] = bytes(value).decode("latin-1")
        else:
            chunks_out.append(bytes(message.get("body") or b""))

    await app(scope, receiver, send)  # type: ignore[arg-type]
    return Response(status=status, headers=out_headers, body=b"".join(chunks_out)), receiver


def payload(
    *events: dict[str, Any], pv: str = "0" * 32, path: str = "/reports/a", version: int = 1
) -> bytes:
    return json.dumps({"v": version, "pv": pv, "path": path, "events": list(events)}).encode()


def imp(n: int) -> dict[str, Any]:
    return {"t": "imp", "l": str(plan_id(n))}


def clk(n: int) -> dict[str, Any]:
    return {"t": "clk", "l": str(plan_id(n))}


def href_event(
    *, href: str = f"{SITE}/insights/b", anchor: str = "battery storage", kind: str = "imp"
) -> dict[str, Any]:
    return {"t": kind, "h": href, "a": anchor}
