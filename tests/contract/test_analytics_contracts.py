"""Phase 7 analytics contracts (W0 freeze, ADR-0015 with the binding section 12).

* the additive changes keep every Phase 0 shape valid;
* ``AnalyticsBatchProvider.fetch_batch`` and every ``AnalyticsReader`` method are async and
  keyword-only; the protocols are runtime-checkable;
* the stored vocabulary excludes ``csv`` and the deferred items (queries, landings, jsonl);
* the exact ``AnalyticsUpdatedPayload`` field set and its rules;
* ``LinkMeasurement`` refuses synthetic data; ``MetricTotals.ctr`` is null iff no impressions;
* the collector wire schema forbids extra keys and bounds every field;
* the shared helpers (``normalized_path``, ``dedupe_key``, ``CollectorConfig.from_settings``);
* the policy file validates.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ken_contracts import analytics as a
from ken_contracts.common import AsyncClosable
from ken_contracts.model import ProviderHealth
from ken_services.analytics import protocol as p
from ken_services.analytics.policy import load_analytics_policy
from tests.conftest import make_settings

POLICY = Path(__file__).resolve().parents[2] / "policies" / "analytics" / "analytics.v1.yaml"
DAY = a.DateRange(start=date(2026, 9, 1), end=date(2026, 9, 1))
PLAN = uuid.UUID("11111111-1111-4111-8111-111111111111")


# --------------------------------------------------------------------------- #
# Additive, non-breaking
# --------------------------------------------------------------------------- #
def test_phase_0_shapes_still_validate() -> None:
    page = a.PageMetrics(page_id="p", source=a.MetricSource.GA4, range=DAY, sessions=3)
    assert (page.pageviews, page.position) == (0, None)
    link = a.LinkMetrics(link_id=str(PLAN), source=a.MetricSource.CSV, range=DAY)
    assert link.downstream_pageviews == 0
    assert set(a.MetricSource) >= {"ga4", "search_console", "first_party", "csv"}
    assert set(inspect.signature(a.AnalyticsProvider.fetch_link_metrics).parameters) == {
        "self",
        "link_ids",
        "date_range",
    }


def test_page_metrics_bounds() -> None:
    with pytest.raises(ValidationError):
        a.PageMetrics(page_id="p", source=a.MetricSource.SEARCH_CONSOLE, range=DAY, position=0.5)
    with pytest.raises(ValidationError):
        a.PageMetrics(page_id="p", source=a.MetricSource.GA4, range=DAY, pageviews=-1)


def test_the_stored_vocabulary() -> None:
    assert [s.value for s in a.AnalyticsSource] == ["ga4", "search_console", "first_party"]
    assert [v.value for v in a.ImportVia] == ["api", "csv", "collector", "synthetic"]
    assert [d.value for d in a.Dataset] == ["links", "pages", "queries"]
    assert [e.value for e in a.LinkEventType] == [
        "internal_link_impression",
        "internal_link_click",
    ]
    assert {a.AnalyticsSource.GA4, a.AnalyticsSource.FIRST_PARTY} == set(a.LINK_SOURCES)
    assert {a.AnalyticsSource.GA4, a.AnalyticsSource.SEARCH_CONSOLE} == set(a.PAGE_SOURCES)
    assert {a.AnalyticsSource.SEARCH_CONSOLE} == set(a.QUERY_SOURCES)
    assert [s.value for s in a.LinkSort] == ["clicks", "impressions", "ctr"]


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #
def _assert_async_keyword_only(protocol: type, method: str) -> None:
    fn = getattr(protocol, method)
    assert inspect.iscoroutinefunction(fn), method
    params = [x for x in inspect.signature(fn).parameters.values() if x.name != "self"]
    assert params, method
    assert all(x.kind is inspect.Parameter.KEYWORD_ONLY for x in params), method


def test_fetch_batch_is_async_keyword_only_and_frozen() -> None:
    _assert_async_keyword_only(a.AnalyticsBatchProvider, "fetch_batch")
    params = inspect.signature(a.AnalyticsBatchProvider.fetch_batch).parameters
    assert list(params) == ["self", "date_range", "datasets"]
    # (issubclass() is refused for protocols with data members such as ``name``.)
    assert a.AnalyticsProvider in a.AnalyticsBatchProvider.__mro__
    assert AsyncClosable in a.AnalyticsBatchProvider.__mro__


@pytest.mark.parametrize(
    "method", ["summary", "list_links", "get_link", "page_impact", "list_imports"]
)
def test_reader_methods_are_async_and_keyword_only(method: str) -> None:
    _assert_async_keyword_only(a.AnalyticsReader, method)


class _Provider:
    name = "fake"

    async def fetch_link_metrics(self, *, link_ids: Any, date_range: Any) -> list[Any]:
        return []

    async def fetch_page_metrics(self, *, page_ids: Any, date_range: Any) -> list[Any]:
        return []

    async def health(self) -> ProviderHealth:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class _BatchProvider(_Provider):
    async def fetch_batch(self, *, date_range: Any, datasets: Any) -> a.AnalyticsBatch:
        return a.AnalyticsBatch()


def test_protocols_are_runtime_checkable() -> None:
    assert isinstance(_BatchProvider(), a.AnalyticsBatchProvider)
    assert not isinstance(_Provider(), a.AnalyticsBatchProvider)

    class Resolver:
        def resolve(self, url: str) -> str | None:
            return None

    assert isinstance(Resolver(), a.PageUrlResolver)


def test_a_batch_carries_validated_query_rows() -> None:
    assert set(a.AnalyticsBatch.model_fields) == {
        "link_rows",
        "page_rows",
        "query_rows",
        "stats",
    }
    row = a.QueryMetrics(
        page_id="p",
        range=DAY,
        query="battery market",
        impressions=10,
        clicks=2,
        position=3.5,
    )
    assert a.AnalyticsBatch(query_rows=(row,)).query_rows == (row,)
    with pytest.raises(ValidationError):
        a.QueryMetrics(page_id="p", range=DAY, query="x\x00y")


def test_fetch_stats_sample_is_bounded_paths_only() -> None:
    assert a.FetchStats(unresolved_sample=("/a", "/b/c")).unresolved_sample == ("/a", "/b/c")
    for bad in ("/a?x=1", "https://h/a", "/a#f", "/a\x00", "/" + "x" * 600):
        with pytest.raises(ValidationError):
            a.FetchStats(unresolved_sample=(bad,))
    with pytest.raises(ValidationError):
        a.FetchStats(unresolved_sample=tuple(f"/{i}" for i in range(101)))


# --------------------------------------------------------------------------- #
# Event payload and measurement
# --------------------------------------------------------------------------- #
def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "import_id": uuid.uuid4(),
        "job_run_id": uuid.uuid4(),
        "source": "first_party",
        "dataset": "links",
        "via": "collector",
        "synthetic": False,
        "date_start": "2026-09-01",
        "date_end": "2026-09-03",
        "rows_written": 10,
        "rows_unresolved": 1,
        "rows_rejected": 0,
        "links_affected": 4,
        "pages_affected": 2,
        "plans_measured": 1,
        **overrides,
    }


def test_analytics_updated_payload_shape() -> None:
    assert list(a.AnalyticsUpdatedPayload.model_fields) == list(_payload())
    payload = a.AnalyticsUpdatedPayload.model_validate(_payload())
    assert payload.model_dump(mode="json")["source"] == "first_party"
    assert a.SUBJECT_ANALYTICS_IMPORT == "analytics_import"
    assert a.ANALYTICS_EMITTED_BY == "ken-analytics"
    assert a.ANALYTICS_ACTOR == "system:analytics"


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "csv"},
        {"via": "jsonl"},
        {"date_end": "2026-08-31"},
        {"synthetic": True, "plans_measured": 1},
        {"via": "synthetic", "synthetic": False, "plans_measured": 0},
        {"rows_written": -1},
        {"landings": 0},
    ],
)
def test_analytics_updated_payload_rules(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        a.AnalyticsUpdatedPayload.model_validate(_payload(**overrides))


def _measurement(**overrides: Any) -> dict[str, Any]:
    return {
        "plan_id": PLAN,
        "measured_at": datetime(2026, 10, 15, tzinfo=UTC),
        "analytics_policy_version": 1,
        "window_start": "2026-09-01",
        "window_end": "2026-09-29",
        "link_totals": {"first_party": {"impressions": 10, "clicks": 1, "ctr": 0.1}},
        "target_comparison": [],
        "source_comparison": [],
        "flags": [],
        "import_ids": [uuid.uuid4()],
        **overrides,
    }


def test_link_measurement_rules() -> None:
    assert a.LinkMeasurement.model_validate(_measurement()).plan_id == PLAN
    overrides: dict[str, Any]
    for overrides in (
        {"flags": ["synthetic_data"]},
        {"import_ids": []},
        {"window_end": "2026-08-01"},
        {"link_totals": {"csv": {"impressions": 0, "clicks": 0, "ctr": None}}},
    ):
        with pytest.raises(ValidationError):
            a.LinkMeasurement.model_validate(_measurement(**overrides))
    same = uuid.uuid4()
    with pytest.raises(ValidationError):
        a.LinkMeasurement.model_validate(_measurement(import_ids=[same, same]))


def test_metric_totals_ctr_is_null_exactly_without_impressions() -> None:
    assert a.MetricTotals.of(impressions=0, clicks=3).ctr is None
    assert a.MetricTotals.of(impressions=4, clicks=1).ctr == 0.25
    with pytest.raises(ValidationError):
        a.MetricTotals(impressions=0, clicks=0, ctr=0.0)
    with pytest.raises(ValidationError):
        a.MetricTotals(impressions=5, clicks=0, ctr=None)


def test_live_window() -> None:
    t0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
    t1 = datetime(2026, 9, 5, 12, tzinfo=UTC)
    window = a.LiveWindow(plan_id=PLAN, deployed_at=t0, rolled_back_at=t1)
    assert window.ever_live and window.contains(t0) and not window.contains(t1)
    assert not a.LiveWindow(plan_id=PLAN).ever_live
    assert not a.LiveWindow(plan_id=PLAN).contains(t0)
    with pytest.raises(ValidationError):
        a.LiveWindow(plan_id=PLAN, deployed_at=t1, rolled_back_at=t0)


def test_window_stats_consistency() -> None:
    stats = {"impressions": 0, "clicks": 0, "pageviews": 0, "sessions": 0}
    a.WindowStats(start=date(2026, 9, 1), end=date(2026, 9, 28), days=28, covered_days=9, **stats)
    with pytest.raises(ValidationError):
        a.WindowStats(start=date(2026, 9, 1), end=None, days=3, covered_days=0, **stats)
    with pytest.raises(ValidationError):
        a.WindowStats(start=date(2026, 9, 1), end=date(2026, 9, 2), days=2, covered_days=3, **stats)


def test_confounder_vocabulary() -> None:
    assert {f.value for f in a.ConfounderFlag} == {
        "overlapping_changes",
        "content_changed",
        "post_window_truncated",
        "rolled_back_in_window",
        "insufficient_data",
        "sampled_data",
        "synthetic_data",
    }


# --------------------------------------------------------------------------- #
# Collector wire schema and shared helpers
# --------------------------------------------------------------------------- #
def _wire(**overrides: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "pv": "0123456789abcdef0123456789abcdef",
        "path": "/reports/india",
        "events": [{"t": "imp", "l": str(PLAN)}, {"t": "clk", "h": "/x", "a": "India market"}],
        **overrides,
    }


def test_the_wire_schema_accepts_both_match_modes() -> None:
    payload = p.CollectorPayload.model_validate(_wire())
    assert isinstance(payload.events[0], p.AttrEvent)
    assert isinstance(payload.events[1], p.HrefEvent)


@pytest.mark.parametrize(
    "overrides",
    [
        {"v": 2},
        {"pv": "0123456789ABCDEF0123456789ABCDEF"},
        {"pv": "short"},
        {"path": "/a?b=1"},
        {"path": "/a#b"},
        {"path": "relative"},
        {"path": "/" + "x" * 512},
        {"events": []},
        {"events": [{"t": "imp", "l": str(PLAN)}] * 51},
        {"events": [{"t": "lnd", "l": str(PLAN)}]},
        {"events": [{"t": "imp", "l": str(PLAN), "h": "/x"}]},
        {"events": [{"t": "imp", "h": "/x", "a": "a" * 121}]},
        {"events": [{"t": "imp", "h": "x" * 2049, "a": "a"}]},
        {"events": [{"t": "imp", "h": "/x", "a": "bad\x00"}]},
        {"ua": "anything"},
        {"ip": "127.0.0.1"},
    ],
)
def test_the_wire_schema_forbids_extras_and_bounds_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        p.CollectorPayload.model_validate(_wire(**overrides))


def test_wire_models_forbid_extra_keys() -> None:
    for model in (p.CollectorPayload, p.AttrEvent, p.HrefEvent):
        assert model.model_config.get("extra") == "forbid"
    assert dict(p.WIRE_EVENT_TYPES) == {
        "imp": "internal_link_impression",
        "clk": "internal_link_click",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://Site.Test/a/b?utm_source=x#frag", "/a/b"),
        ("/a/./b/../c?q=1", "/a/c"),
        ("http://site.test", "/"),
        ("/", "/"),
        ("/caf%C3%A9", "/caf%C3%A9"),
    ],
)
def test_normalized_path(value: str, expected: str) -> None:
    assert p.normalized_path(value) == expected


def test_dedupe_key() -> None:
    pv = "0123456789abcdef0123456789abcdef"
    expected = hashlib.sha256(f"{pv}|clk|{PLAN}".encode()).hexdigest()
    assert p.dedupe_key(pv, "clk", PLAN) == expected
    assert p.dedupe_key(pv, "imp", PLAN) != expected


def test_link_resolution_is_a_plan_xor_a_rejection() -> None:
    p.LinkResolution(plan_id=PLAN, match_mode=a.MatchMode.ATTR)
    p.LinkResolution(
        plan_id=None, match_mode=a.MatchMode.HREF, rejection=p.CollectorRejection.AMBIGUOUS_LINK
    )
    with pytest.raises(ValueError, match="never both"):
        p.LinkResolution(plan_id=None, match_mode=a.MatchMode.ATTR)
    with pytest.raises(ValueError, match="never both"):
        p.LinkResolution(
            plan_id=PLAN, match_mode=a.MatchMode.ATTR, rejection=p.CollectorRejection.BOT
        )


def test_collector_config_marks_loopback_origins_synthetic() -> None:
    policy = load_analytics_policy(POLICY)
    local = p.CollectorConfig.from_settings(
        make_settings(), policy, default_origin="http://127.0.0.1:50123/"
    )
    assert local.allowed_origins == ("http://127.0.0.1:50123",)
    assert local.synthetic is True
    assert local.limits.max_body_bytes == 4096
    remote = p.CollectorConfig.from_settings(
        make_settings(
            collector_allow_remote=True,
            collector_allowed_origins="https://www.example.com, http://127.0.0.1:9",
        ),
        policy,
        default_origin="http://127.0.0.1:50123",
    )
    assert remote.allowed_origins == ("https://www.example.com", "http://127.0.0.1:9")
    assert remote.synthetic is False


def test_validate_range() -> None:
    p.validate_range(a.DateRange(start=date(2026, 1, 1), end=date(2027, 2, 4)))  # 400 days
    with pytest.raises(p.AnalyticsImportError) as info:
        p.validate_range(a.DateRange(start=date(2026, 1, 1), end=date(2027, 2, 5)))
    assert info.value.code is p.ImportErrorCode.RANGE_TOO_LONG
    with pytest.raises(p.AnalyticsImportError) as info:
        p.validate_range(a.DateRange(start=date(2026, 1, 2), end=date(2026, 1, 1)))
    assert info.value.code is p.ImportErrorCode.INVALID_REQUEST


def test_counter_vocabulary_has_no_deferred_items() -> None:
    assert "landings" not in p.COUNTER_NAMES
    assert {"rows_read", "rows_written", "merged_rows", "plans_measured"} <= p.COUNTER_NAMES


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_the_analytics_policy_file_validates() -> None:
    policy = load_analytics_policy(POLICY)
    assert policy.version == 1
    assert (policy.windows.comparison_days, policy.windows.min_covered_days) == (28, 14)
    assert policy.windows.measurement_window_days == 28
    assert (
        policy.lag_days(a.AnalyticsSource.GA4),
        policy.lag_days(a.AnalyticsSource("search_console")),
    ) == (1, 3)
    assert policy.collector.max_body_bytes == 4096
    assert policy.collector.rate_limit.max_tracked_ips > 0
    assert any("KenInterlinkBot" in pattern for pattern in policy.bot_ua_patterns)
    assert (policy.retention.link_events_days, policy.retention.daily_aggregates_months) == (
        90,
        25,
    )
    assert POLICY.stem == f"analytics.v{policy.version}"


def test_the_policy_refuses_bad_values() -> None:
    raw = load_analytics_policy(POLICY).model_dump(mode="json")
    for path, value in (
        (("collector", "max_body_bytes"), 8192),
        (("windows", "min_covered_days"), 29),
        (("bot_ua_patterns",), ["("]),
        (("bot_ua_patterns",), []),
    ):
        broken = copy.deepcopy(raw)
        target = broken
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError):
            a.AnalyticsPolicy.model_validate(broken)
