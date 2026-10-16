"""The first-party collector ASGI app (ADR-0015 §1, hardened by §12.3 / §12.9).

Routes and methods; the ordered validation (streamed body cap -- chunked included -- content
type, exact Origin, bot/prefetch/GPC, rate limit, schema, link resolution, dedupe); the
invariant that EVERY outcome is 204; pseudonymity (what is stored, and that neither the IP
nor the User-Agent reaches a log line).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from ken_contracts.analytics import CollectorPolicy, LinkEventType, MatchMode, RateLimitPolicy
from ken_services.analytics.collector.app import CollectorConfigError
from ken_services.analytics.collector.beacon import CONFIG_GLOBAL, beacon_source
from ken_services.analytics.protocol import CollectorRejection as R
from ken_services.analytics.protocol import dedupe_key
from tests.unit.analytics._collector_support import (
    NOW,
    ORIGIN,
    PEER,
    SITE,
    UA,
    Clock,
    Ticker,
    build_app,
    call,
    clk,
    config,
    href_event,
    imp,
    live_plan,
    payload,
    plan_id,
    policy,
)


# --------------------------------------------------------------------------- #
# Routes and methods
# --------------------------------------------------------------------------- #
async def test_beacon_is_javascript_nosniff_and_cached_from_policy() -> None:
    app, _ = build_app()
    response, _ = await call(app, method="GET", path="/k.js", origin=None, content_type=None)
    limits = policy().collector
    assert response.status == 200
    assert response.headers["content-type"] == "application/javascript; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == f"max-age={limits.beacon_cache_max_age_seconds}"
    body = response.body.decode("utf-8")
    assert body.startswith(f"window.{CONFIG_GLOBAL}={{")
    assert body.endswith(beacon_source())  # the static file is served verbatim after the prelude
    assert json.loads(body.split("=", 1)[1].split(";\n", 1)[0])["maxEvents"] == float(
        limits.max_events_per_request
    )


async def test_health_answers_this_instance_nonce() -> None:
    app, _ = build_app()
    response, _ = await call(
        app, method="GET", path="/__ken_collector/health", origin=None, content_type=None
    )
    assert response.status == 200
    assert response.body.decode() == app.nonce


async def test_head_on_a_get_route_has_no_body() -> None:
    app, _ = build_app()
    response, _ = await call(app, method="HEAD", path="/k.js", origin=None, content_type=None)
    assert response.status == 200
    assert response.body == b""
    assert int(response.headers["content-length"]) > 0


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [("GET", "/e", "POST"), ("PUT", "/e", "POST"), ("POST", "/k.js", "GET, HEAD")],
)
async def test_wrong_method_on_a_known_path_is_405(method: str, path: str, allow: str) -> None:
    app, _ = build_app()
    response, _ = await call(app, method=method, path=path)
    assert response.status == 405
    assert response.headers["allow"] == allow
    assert app.counters.counts == {R.METHOD_NOT_ALLOWED.value: 1}


@pytest.mark.parametrize("path", ["/", "/collect", "/e/", "/k.js.map", "/__ken_collector/x"])
async def test_every_other_path_is_404(path: str) -> None:
    app, _ = build_app()
    response, _ = await call(app, method="GET", path=path)
    assert response.status == 404
    assert app.counters.counts == {R.NOT_FOUND.value: 1}


async def test_a_non_http_scope_is_ignored() -> None:
    app, _ = build_app()
    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:  # pragma: no cover - never called
        raise AssertionError("a websocket scope must not be read")

    await app({"type": "websocket"}, receive, send)
    assert sent == []


# --------------------------------------------------------------------------- #
# Every rejection is still a 204 (the response is never a validity oracle)
# --------------------------------------------------------------------------- #
async def _reject(**kwargs: Any) -> tuple[Any, Any]:
    app, sink = build_app([live_plan(1)])
    response, receiver = await call(app, **kwargs)
    assert response.status == 204
    assert response.body == b""
    assert "content-type" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert sink.stored == []
    return app, receiver


async def test_oversize_body_is_refused_on_the_streamed_bytes() -> None:
    limits = policy().collector
    app, _ = await _reject(body=b"x" * (limits.max_body_bytes + 1))
    assert app.counters.counts == {R.BODY_TOO_LARGE.value: 1}


async def test_a_chunked_body_is_abandoned_as_soon_as_it_passes_the_cap() -> None:
    limits = policy().collector
    chunk = b"y" * 1024
    chunks = [chunk] * 40  # 40 KiB in 1 KiB chunks: ten times the cap
    app, receiver = await _reject(chunks=chunks)
    assert app.counters.counts == {R.BODY_TOO_LARGE.value: 1}
    # It stopped reading at the cap instead of buffering the whole stream.
    assert receiver.reads <= limits.max_body_bytes // len(chunk) + 1 < len(chunks)


async def test_a_content_length_header_cannot_lift_the_cap() -> None:
    limits = policy().collector
    app, _ = await _reject(
        body=b"z" * (limits.max_body_bytes + 10),
        extra_headers=((b"content-length", b"10"),),
    )
    assert app.counters.counts == {R.BODY_TOO_LARGE.value: 1}


@pytest.mark.parametrize(
    "content_type",
    [None, "application/json", "text/plain; charset=utf-16", "text/html", "", "text/plain2"],
)
async def test_only_text_plain_is_accepted(content_type: str | None) -> None:
    app, _ = await _reject(body=payload(imp(1)), content_type=content_type)
    assert app.counters.counts == {R.BAD_CONTENT_TYPE.value: 1}


async def test_text_plain_with_a_utf8_charset_is_accepted() -> None:
    app, sink = build_app([live_plan(1)])
    response, _ = await call(app, body=payload(imp(1)), content_type="text/plain;charset=UTF-8")
    assert response.status == 204
    assert len(sink.stored) == 1


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://127.0.0.1:4100",  # scheme differs
        "http://127.0.0.1:4101",  # port differs
        "http://127.0.0.2:4100",  # host differs
        "http://127.0.0.1",  # port missing
        "http://evil.test",
    ],
)
async def test_origin_must_match_scheme_host_and_port_exactly(origin: str | None) -> None:
    app, _ = await _reject(body=payload(imp(1)), origin=origin)
    assert app.counters.counts == {R.ORIGIN_NOT_ALLOWED.value: 1}


async def test_a_repeated_origin_header_is_foreign() -> None:
    app, _ = await _reject(body=payload(imp(1)), extra_headers=((b"origin", ORIGIN.encode()),))
    assert app.counters.counts == {R.ORIGIN_NOT_ALLOWED.value: 1}


@pytest.mark.parametrize(
    "agent",
    [
        "KenInterlinkBot/1.0",
        "Mozilla/5.0 HeadlessChrome/120",
        "Googlebot/2.1",
        "Mozilla/5.0 (compatible; bingbot/2.0)",
        "Chrome-Lighthouse",
        "facebookexternalhit/1.1",
        "Mozilla/5.0 Spider",
        "some crawler",
    ],
)
async def test_bots_are_dropped_case_insensitively(agent: str) -> None:
    app, _ = await _reject(body=payload(imp(1)), user_agent=agent.lower())
    assert app.counters.counts == {R.BOT.value: 1}


@pytest.mark.parametrize("header", [b"sec-purpose", b"purpose"])
async def test_prefetch_is_dropped(header: bytes) -> None:
    app, _ = await _reject(body=payload(imp(1)), extra_headers=((header, b"prefetch;anonymous"),))
    assert app.counters.counts == {R.PREFETCH.value: 1}


async def test_global_privacy_control_is_honoured_server_side() -> None:
    app, _ = await _reject(body=payload(imp(1)), extra_headers=((b"sec-gpc", b"1"),))
    assert app.counters.counts == {R.GPC.value: 1}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"{}",
        json.dumps({"v": 2, "pv": "0" * 32, "path": "/a", "events": [{"t": "imp"}]}).encode(),
        json.dumps({"v": 1, "pv": "nothex", "path": "/a", "events": []}).encode(),
        json.dumps({"v": 1, "pv": "0" * 32, "path": "/a?x=1", "events": [{"t": "imp"}]}).encode(),
        json.dumps({"v": 1, "pv": "0" * 32, "path": "relative", "events": []}).encode(),
        json.dumps({"v": 1, "pv": "0" * 32, "path": "/a", "events": []}).encode(),
        json.dumps(
            {"v": 1, "pv": "0" * 32, "path": "/a", "events": [{"t": "lnd", "l": str(plan_id(1))}]}
        ).encode(),
        json.dumps(
            {
                "v": 1,
                "pv": "0" * 32,
                "path": "/a",
                "events": [{"t": "imp", "l": str(plan_id(1)), "x": 1}],
            }
        ).encode(),
        json.dumps({"v": 1, "pv": "0" * 32, "path": "/a", "events": [imp(1)] * 51}).encode(),
    ],
)
async def test_the_frozen_schema_is_enforced(body: bytes) -> None:
    app, _ = await _reject(body=body)
    assert app.counters.counts == {R.INVALID_SCHEMA.value: 1}


async def test_an_unknown_plan_is_not_live() -> None:
    app, _ = await _reject(body=payload(imp(7)))
    assert app.counters.counts == {R.LINK_NOT_LIVE.value: 1}


async def test_a_live_plan_on_another_path_is_a_path_mismatch() -> None:
    app, _ = await _reject(body=payload(imp(1), path="/reports/other"))
    assert app.counters.counts == {R.PATH_MISMATCH.value: 1}


async def test_a_disconnect_before_the_body_is_complete_stores_nothing() -> None:
    app, _ = await _reject(chunks=[b'{"v":1'], disconnect=True)
    assert app.counters.counts == {}


async def test_a_storage_failure_is_never_visible_to_the_browser() -> None:
    from tests.unit.analytics._collector_support import FakeSink

    sink = FakeSink(fail=True)
    app, _ = build_app([live_plan(1)], sink=sink)
    with capture_logs() as logs:
        response, _ = await call(app, body=payload(imp(1)))
    assert response.status == 204
    assert app.counters.accepted == 0
    assert [entry["event"] for entry in logs] == ["collector_store_failed"]
    assert logs[0]["error_class"] == "ConnectionError"


# --------------------------------------------------------------------------- #
# Accepted events
# --------------------------------------------------------------------------- #
async def test_attr_mode_stores_exactly_the_pseudonymous_row() -> None:
    clock = Clock()
    app, sink = build_app([live_plan(1)], clock=clock)
    response, _ = await call(app, body=payload(imp(1), clk(1)))
    assert response.status == 204
    assert [(e.event_type, e.match_mode) for e in sink.stored] == [
        (LinkEventType.IMPRESSION, MatchMode.ATTR),
        (LinkEventType.CLICK, MatchMode.ATTR),
    ]
    stored = sink.stored[0]
    assert stored.plan_id == plan_id(1)
    assert stored.received_at == NOW.replace(second=0, microsecond=0)
    assert stored.dedupe_key == dedupe_key("0" * 32, "imp", plan_id(1))
    assert stored.synthetic is True
    # A row has no field that could carry an identifier: the dataclass is the column set.
    assert set(vars(type(stored))["__slots__"]) == {
        "event_type",
        "plan_id",
        "match_mode",
        "synthetic",
        "received_at",
        "dedupe_key",
    }
    assert app.counters.accepted == 2


async def test_received_at_is_truncated_to_the_minute() -> None:
    clock = Clock()
    app, sink = build_app([live_plan(1)], clock=clock)
    await call(app, body=payload(imp(1)))
    assert sink.stored[0].received_at.second == 0
    assert sink.stored[0].received_at.microsecond == 0
    assert sink.stored[0].row()["event_date"] == NOW.date()


async def test_synthetic_comes_from_the_configuration_not_the_client() -> None:
    remote = config(origins=("https://site.test",), allow_remote=True)
    app, sink = build_app([live_plan(1)], collector_config=remote)
    assert app.synthetic is False
    await call(app, body=payload(imp(1)), origin="https://site.test")
    assert sink.stored[0].synthetic is False


async def test_a_duplicate_page_view_is_stored_once() -> None:
    app, sink = build_app([live_plan(1)])
    await call(app, body=payload(imp(1), imp(1)))  # a duplicate inside one batch
    await call(app, body=payload(imp(1)))  # and again in a later request
    assert len(sink.stored) == 1
    assert app.counters.counts[R.DUPLICATE.value] == 2
    assert app.counters.accepted == 1


async def test_a_different_page_view_of_the_same_link_is_a_new_event() -> None:
    app, sink = build_app([live_plan(1)])
    await call(app, body=payload(imp(1), pv="0" * 32))
    await call(app, body=payload(imp(1), pv="1" * 32))
    assert len(sink.stored) == 2


# --------------------------------------------------------------------------- #
# href mode (ADR-0015 §12.6.4 / §12.9.7)
# --------------------------------------------------------------------------- #
async def test_href_mode_resolves_one_live_plan() -> None:
    app, sink = build_app([live_plan(1)])
    await call(app, body=payload(href_event()))
    assert len(sink.stored) == 1
    assert sink.stored[0].match_mode is MatchMode.HREF
    assert sink.stored[0].plan_id == plan_id(1)


async def test_href_mode_accepts_a_relative_href_resolved_against_the_source_url() -> None:
    app, sink = build_app([live_plan(1)])
    await call(app, body=payload(href_event(href="/insights/b")))
    assert len(sink.stored) == 1


@pytest.mark.parametrize(
    "event",
    [
        href_event(href=f"{SITE}/insights/other"),
        href_event(anchor="battery  storage"),
        href_event(anchor="Battery storage"),
        href_event(href="mailto:x@site.test"),
    ],
)
async def test_href_mode_without_a_match_is_unresolved(event: dict[str, Any]) -> None:
    app, _ = await _reject(body=payload(event))
    assert app.counters.counts == {R.UNRESOLVED_LINK.value: 1}


async def test_href_mode_with_two_matching_plans_is_ambiguous() -> None:
    plans = [live_plan(1), live_plan(2)]  # same path, anchor and href
    app, sink = build_app(plans)
    response, _ = await call(app, body=payload(href_event()))
    assert response.status == 204
    assert sink.stored == []
    assert app.counters.counts == {R.AMBIGUOUS_LINK.value: 1}


async def test_href_mode_on_another_path_resolves_nothing() -> None:
    app, _ = await _reject(body=payload(href_event(), path="/reports/elsewhere"))
    assert app.counters.counts == {R.UNRESOLVED_LINK.value: 1}


# --------------------------------------------------------------------------- #
# Rate limiting through the app
# --------------------------------------------------------------------------- #
async def test_the_per_ip_window_refuses_further_requests() -> None:
    ticker = Ticker()
    limits = policy().collector.rate_limit
    app, sink = build_app([live_plan(1)], ticker=ticker)
    sent = 0
    while sent < limits.per_ip_events_per_minute:
        await call(app, body=payload(imp(1), pv=f"{sent:032x}"))
        sent += 1
    assert len(sink.stored) == sent
    response, _ = await call(app, body=payload(imp(1), pv="f" * 32))
    assert response.status == 204
    assert len(sink.stored) == sent  # nothing more was stored
    assert app.counters.counts[R.RATE_LIMITED.value] == 1
    # Another IP in the same window is unaffected.
    await call(app, body=payload(imp(1), pv="e" * 32), peer="127.0.0.8")
    assert len(sink.stored) == sent + 1
    # And the next window lets the first IP back in.
    ticker.value += 61
    await call(app, body=payload(imp(1), pv="d" * 32))
    assert len(sink.stored) == sent + 2


async def test_the_global_cap_bounds_what_address_rotation_buys() -> None:
    limits = CollectorPolicy(
        rate_limit=RateLimitPolicy(
            per_ip_events_per_minute=2, global_events_per_minute=5, max_tracked_ips=50
        )
    )
    ticker = Ticker()
    app, sink = build_app([live_plan(1)], collector_config=config(limits=limits), ticker=ticker)
    for n in range(3):  # three addresses, two events each: 6 > the global cap of 5
        await call(app, body=payload(imp(1), clk(1), pv=f"{n:032x}"), peer=f"127.0.0.{n}")
    assert len(sink.stored) == 6
    response, _ = await call(app, body=payload(imp(1), pv="a" * 32), peer="127.0.0.77")
    assert response.status == 204
    assert len(sink.stored) == 6
    assert app.counters.counts[R.RATE_LIMITED.value] == 1
    ticker.value += 61  # the next window starts clean
    await call(app, body=payload(imp(1), pv="b" * 32), peer="127.0.0.77")
    assert len(sink.stored) == 7


async def test_the_bounded_map_evicts_the_least_recently_charged_address() -> None:
    """The map is capped, so an evicted address gets a fresh budget. That is deliberate: the
    GLOBAL cap -- not the per-IP map -- is what bounds address rotation (§12.3)."""
    limits = CollectorPolicy(
        rate_limit=RateLimitPolicy(
            per_ip_events_per_minute=1, global_events_per_minute=1000, max_tracked_ips=2
        )
    )
    app, sink = build_app([live_plan(1)], collector_config=config(limits=limits))
    first = "127.0.0.1"
    await call(app, body=payload(imp(1), pv="1" * 32), peer=first)
    await call(app, body=payload(imp(1), pv="2" * 32), peer=first)
    assert app.counters.counts[R.RATE_LIMITED.value] == 1  # its budget is used up
    for n, peer in enumerate(("127.0.0.2", "127.0.0.3")):  # two more addresses evict the first
        await call(app, body=payload(imp(1), pv=f"{n + 3:032x}"), peer=peer)
    await call(app, body=payload(imp(1), pv="5" * 32), peer=first)
    assert len(sink.stored) == 4  # the evicted address was let back in
    assert app.counters.counts[R.RATE_LIMITED.value] == 1


async def test_a_missing_peer_address_still_gets_a_bucket() -> None:
    app, sink = build_app([live_plan(1)])
    response, _ = await call(app, body=payload(imp(1)), peer=None)
    assert response.status == 204
    assert len(sink.stored) == 1


async def test_a_forwarded_header_cannot_choose_a_bucket() -> None:
    """The peer IP is the only client identity: ``proxy_headers=False`` (§12.3)."""
    ticker = Ticker()
    limits = policy().collector.rate_limit
    app, _ = build_app([live_plan(1)], ticker=ticker)
    for n in range(limits.per_ip_events_per_minute):
        await call(app, body=payload(imp(1), pv=f"{n:032x}"))
    response, _ = await call(
        app,
        body=payload(imp(1), pv="f" * 32),
        extra_headers=((b"x-forwarded-for", b"10.0.0.1"), (b"x-real-ip", b"10.0.0.2")),
    )
    assert response.status == 204
    assert app.counters.counts[R.RATE_LIMITED.value] == 1


# --------------------------------------------------------------------------- #
# Configuration gating and logging hygiene
# --------------------------------------------------------------------------- #
def test_a_non_loopback_origin_needs_allow_remote() -> None:
    with pytest.raises(CollectorConfigError):
        build_app(collector_config=config(origins=("https://site.test",)))


def test_adding_a_non_loopback_origin_needs_allow_remote() -> None:
    app, _ = build_app()
    with pytest.raises(CollectorConfigError):
        app.add_allowed_origin("https://site.test")
    assert app.allowed_origins == (ORIGIN,)
    assert app.synthetic is True


def test_adding_the_simulated_sites_origin_keeps_the_events_synthetic() -> None:
    app, _ = build_app()
    assert app.add_allowed_origin("http://127.0.0.1:4200/") == "http://127.0.0.1:4200"
    assert app.allowed_origins == (ORIGIN, "http://127.0.0.1:4200")
    assert app.synthetic is True


def test_adding_a_remote_origin_turns_synthetic_off() -> None:
    app, _ = build_app(collector_config=config(allow_remote=True))
    app.add_allowed_origin("https://site.test")
    assert app.synthetic is False


async def test_neither_the_ip_nor_the_user_agent_reaches_a_log_line() -> None:
    structlog.reset_defaults()
    ticker = Ticker()
    app, _ = build_app([live_plan(1)], ticker=ticker)
    with capture_logs() as logs:
        await call(app, body=payload(imp(1)))
        await call(app, body=payload(imp(1)), user_agent="Googlebot/2.1")
        await call(app, body=payload(imp(1)), origin="http://evil.test")
        ticker.value += policy().collector.rejection_log_interval_seconds + 1
        await call(app, body=payload(imp(1), pv="2" * 32))  # triggers the interval log
    text = json.dumps(logs)
    assert "collector_counters" in text
    for secret in (PEER, UA, "Googlebot/2.1", "http://evil.test", "/reports/a", "0" * 32):
        assert secret not in text


async def test_the_counter_log_carries_counts_only() -> None:
    ticker = Ticker()
    app, _ = build_app([live_plan(1)], ticker=ticker)
    await call(app, body=payload(imp(1)))
    await call(app, body=payload(imp(9)))
    ticker.value += policy().collector.rejection_log_interval_seconds + 1
    with capture_logs() as logs:
        await call(app, method="GET", path="/nope")
    assert logs[0]["event"] == "collector_counters"
    assert logs[0]["accepted"] == 1
    assert logs[0][R.LINK_NOT_LIVE.value] == 1
    assert app.counters.counts == {R.NOT_FOUND.value: 1}  # the window was reset


# --------------------------------------------------------------------------- #
# Abuse costs something, and a body read has a deadline (Phase 7 QA finding 3)
# --------------------------------------------------------------------------- #
async def test_invalid_bodies_are_charged_so_a_flood_is_rate_limited() -> None:
    """QA: 600 malformed bodies from one peer used to cost NOTHING, because the charge was
    applied only to a payload that had already validated."""
    limits = CollectorPolicy(
        rate_limit=RateLimitPolicy(
            per_ip_events_per_minute=3, global_events_per_minute=1000, max_tracked_ips=50
        )
    )
    app, sink = build_app([live_plan(1)], collector_config=config(limits=limits))
    for _ in range(10):
        response, _ = await call(app, body=b"{not json")
        assert response.status == 204
        assert response.body == b""
    assert sink.stored == []
    counts = app.counters.counts
    # The first three requests burned the per-IP budget; the rest never reached the schema.
    assert counts[R.INVALID_SCHEMA.value] == 3
    assert counts[R.RATE_LIMITED.value] == 7


async def test_an_unresolvable_event_is_charged_too() -> None:
    limits = CollectorPolicy(
        rate_limit=RateLimitPolicy(
            per_ip_events_per_minute=2, global_events_per_minute=1000, max_tracked_ips=50
        )
    )
    app, sink = build_app([live_plan(1)], collector_config=config(limits=limits))
    for n in range(4):  # a live-looking request for a plan that is not live
        await call(app, body=payload(imp(99), pv=f"{n:032x}"))
    assert sink.stored == []
    assert app.counters.counts[R.LINK_NOT_LIVE.value] == 2
    assert app.counters.counts[R.RATE_LIMITED.value] == 2


async def test_a_valid_batch_still_costs_exactly_its_event_count() -> None:
    """The fixed charge must not change the accounting of a valid batch (§12.10.11)."""
    limits = CollectorPolicy(
        rate_limit=RateLimitPolicy(
            per_ip_events_per_minute=4, global_events_per_minute=1000, max_tracked_ips=50
        )
    )
    app, sink = build_app([live_plan(1)], collector_config=config(limits=limits))
    await call(app, body=payload(imp(1), clk(1), pv="1" * 32))  # 2 events -> 2 charged
    await call(app, body=payload(imp(1), clk(1), pv="2" * 32))  # 2 more -> the budget is used
    assert len(sink.stored) == 4
    assert R.RATE_LIMITED.value not in app.counters.counts
    response, _ = await call(app, body=payload(imp(1), pv="3" * 32))
    assert response.status == 204
    assert len(sink.stored) == 4
    assert app.counters.counts[R.RATE_LIMITED.value] == 1


async def test_a_body_that_never_ends_is_abandoned_on_the_read_deadline() -> None:
    """Many tiny chunks stay under the 4 KiB cap for ever, so the read is also time-bounded."""
    app, sink = build_app([live_plan(1)], body_read_timeout=0.05)
    with capture_logs() as logs:
        # The outer deadline is a safety net: without the app's own one this would never
        # return, and a hanging test is a worse signal than a failing one.
        async with asyncio.timeout(5):
            response, receiver = await call(app, endless=True, chunk_delay=0.001)
    assert response.status == 204
    assert response.body == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-type" not in response.headers
    assert sink.stored == []
    assert receiver.reads > 1  # it really was reading, and then gave up
    assert [entry["event"] for entry in logs if "body_read" in entry["event"]] == [
        "collector_body_read_timeout"
    ]


async def test_the_read_deadline_answers_exactly_like_any_other_rejection() -> None:
    slow, _ = build_app([live_plan(1)], body_read_timeout=0.05)
    async with asyncio.timeout(5):
        timed_out, _ = await call(slow, endless=True, chunk_delay=0.001)
    normal_app, _ = build_app([live_plan(1)])
    rejected, _ = await call(normal_app, body=b"{not json")
    assert (timed_out.status, timed_out.headers, timed_out.body) == (
        rejected.status,
        rejected.headers,
        rejected.body,
    )
