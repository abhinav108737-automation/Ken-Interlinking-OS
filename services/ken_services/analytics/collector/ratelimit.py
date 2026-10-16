"""Bounded in-memory rate limiting for the first-party collector (ADR-0015 §12.2 / §12.3).

The drafted token bucket keyed by ``HMAC(salt, ip)`` was SIMPLIFIED by the architecture
review: the key is never persisted, so hashing it adds nothing. What is binding instead:

* a **fixed window** of one minute, counting EVENTS (not requests);
* keyed on the **ASGI peer IP only** -- never a forwarded header (uvicorn runs with
  ``proxy_headers=False``), so a client cannot choose its own bucket;
* a **bounded** map: at most ``rate_limit.max_tracked_ips`` entries, least-recently-charged
  evicted first, and the whole map is dropped at every window roll. IPv6 address rotation
  therefore cannot exhaust memory -- and the global cap still bounds what rotation buys;
* nothing here is ever persisted or logged: the map lives in this process, for at most one
  window, and the IP never reaches a log line or the database.

:meth:`FixedWindowRateLimiter.allows` runs at step 5 of the validation order, BEFORE the
body is parsed, so the event count is not yet known; it refuses once a window is used up.
Every request it lets through is then charged ONE event immediately -- so a request that
turns out to be malformed, unresolvable or duplicate is never free (before that fix a peer
could send unlimited invalid bodies at no cost) -- and a valid batch is charged the rest of
its event count after the schema check. A window can therefore overshoot its cap by at most
one request (``max_events_per_request`` events), which is deliberate: the alternative is to
parse untrusted bodies before rate limiting them.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Final

from ken_contracts.analytics import RateLimitPolicy

__all__ = ["WINDOW_SECONDS", "FixedWindowRateLimiter"]

WINDOW_SECONDS: Final = 60.0
"""The policy states both caps per minute."""


class FixedWindowRateLimiter:
    """Per-IP and global fixed-window event counters. Not thread-safe (one event loop)."""

    def __init__(self, limits: RateLimitPolicy) -> None:
        self._per_ip = limits.per_ip_events_per_minute
        self._global = limits.global_events_per_minute
        self._max_tracked = limits.max_tracked_ips
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._window: int | None = None
        self._global_count = 0

    @property
    def tracked_ips(self) -> int:
        """How many keys the bounded map currently holds (observability and tests)."""
        return len(self._counts)

    def _roll(self, now: float) -> None:
        window = int(now // WINDOW_SECONDS)
        if window != self._window:
            self._window = window
            self._global_count = 0
            self._counts.clear()

    def allows(self, ip: str, *, now: float) -> bool:
        """Whether a request from ``ip`` may be processed in the current window."""
        self._roll(now)
        if self._global_count >= self._global:
            return False
        used = self._counts.get(ip)
        if used is None:
            return True
        self._counts.move_to_end(ip)
        return used < self._per_ip

    def charge(self, ip: str, events: int, *, now: float) -> None:
        """Count ``events`` against ``ip`` and the global window."""
        if events <= 0:
            return
        self._roll(now)
        self._global_count += events
        self._counts[ip] = self._counts.pop(ip, 0) + events
        while len(self._counts) > self._max_tracked:
            self._counts.popitem(last=False)
