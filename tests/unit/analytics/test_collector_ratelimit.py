"""The collector's bounded per-IP fixed window (ADR-0015 §12.2 / §12.3).

The cap counts EVENTS per minute per ASGI peer IP, plus a global cap, and the map is
LRU-bounded at ``max_tracked_ips`` so address rotation cannot exhaust memory.
"""

from __future__ import annotations

from ken_contracts.analytics import RateLimitPolicy
from ken_services.analytics.collector.ratelimit import WINDOW_SECONDS, FixedWindowRateLimiter


def limiter(**overrides: int) -> FixedWindowRateLimiter:
    policy = RateLimitPolicy(
        **{
            "per_ip_events_per_minute": 10,
            "global_events_per_minute": 25,
            "max_tracked_ips": 3,
            **overrides,
        }
    )
    return FixedWindowRateLimiter(policy)


def test_the_window_counts_events_not_requests() -> None:
    limits = limiter()
    assert limits.allows("a", now=0.0)
    limits.charge("a", 9, now=0.0)
    assert limits.allows("a", now=0.0)
    limits.charge("a", 1, now=0.0)
    assert not limits.allows("a", now=0.0)


def test_each_ip_has_its_own_budget() -> None:
    limits = limiter()
    limits.charge("a", 10, now=0.0)
    assert not limits.allows("a", now=0.0)
    assert limits.allows("b", now=0.0)


def test_a_new_window_resets_everything() -> None:
    limits = limiter()
    limits.charge("a", 10, now=0.0)
    assert not limits.allows("a", now=WINDOW_SECONDS - 0.1)
    assert limits.allows("a", now=WINDOW_SECONDS)
    assert limits.tracked_ips == 0


def test_the_global_cap_bounds_what_address_rotation_buys() -> None:
    limits = limiter(per_ip_events_per_minute=10, global_events_per_minute=25)
    for n in range(5):
        assert limits.allows(f"ip-{n}", now=1.0)
        limits.charge(f"ip-{n}", 5, now=1.0)
    assert not limits.allows("ip-fresh", now=1.0)
    assert limits.allows("ip-fresh", now=1.0 + WINDOW_SECONDS)


def test_the_map_is_lru_bounded() -> None:
    limits = limiter(max_tracked_ips=3)
    for n in range(10):
        limits.charge(f"ip-{n}", 1, now=2.0)
        assert limits.tracked_ips <= 3
    assert limits.tracked_ips == 3
    # The evicted keys are the least recently charged, so the newest are still counted.
    limits.charge("ip-9", 9, now=2.0)
    assert not limits.allows("ip-9", now=2.0)


def test_a_zero_charge_changes_nothing() -> None:
    limits = limiter()
    limits.charge("a", 0, now=0.0)
    assert limits.tracked_ips == 0


def test_recently_seen_ips_survive_eviction() -> None:
    limits = limiter(max_tracked_ips=2)
    limits.charge("a", 1, now=0.0)
    limits.charge("b", 1, now=0.0)
    limits.allows("a", now=0.0)  # touches 'a'
    limits.charge("c", 1, now=0.0)  # evicts the least recently used ('b')
    assert limits.tracked_ips == 2
    limits.charge("a", 9, now=0.0)
    assert not limits.allows("a", now=0.0)
