"""Related-report modules: ADR-0027 §1c, the owner's Priority 1.

The gates that matter are asserted directly: a module never contains a different market, a
page of the same geography, or the source itself. Ranking is asserted as an ORDER, not as
opaque numbers, so a change of policy shows up as a changed order.
"""

from __future__ import annotations

import uuid

from ken_contracts.intelligence import GeographyNode
from ken_contracts.recommendations import ReasonCode
from ken_services.recommendations.modules import (
    DEFAULT_MODULE_SIZE,
    ModuleCandidate,
    ModuleItem,
    ModuleSource,
    module_sizes,
    regional_sibling_module,
)
from tests.unit.recommendations._support import taxonomy

TAX = taxonomy()


def _geo(geo_id: str | None) -> GeographyNode | None:
    if geo_id is None:
        return None
    return next(g for g in TAX.geographies if g.id == geo_id)


def _source(geo: str | None, core: str = "ev") -> ModuleSource:
    return ModuleSource(page_id=uuid.uuid4(), market_core=core, geography=_geo(geo))


def _cand(
    geo: str | None,
    *,
    core: str | None = "ev",
    inbound: int = 5,
    commercial: int = 1,
    similarity: float | None = 0.5,
    url: str | None = None,
) -> ModuleCandidate:
    label = geo or "global"
    return ModuleCandidate(
        page_id=uuid.uuid4(),
        url=url or f"/industry-reports/{label}-ev-market",
        title=f"{label.title()} EV Market",
        market_core=core,
        geography=_geo(geo),
        inbound_links=inbound,
        commercial_importance=commercial,
        similarity=similarity,
    )


def _module(
    source: ModuleSource, *candidates: ModuleCandidate, limit: int = DEFAULT_MODULE_SIZE
) -> tuple[ModuleItem, ...]:
    return regional_sibling_module(source, candidates, taxonomy=TAX, limit=limit)


# --------------------------------------------------------------------------- #
# What may never appear
# --------------------------------------------------------------------------- #
def test_a_different_market_is_never_in_the_module() -> None:
    """The owner's original rejection: "Indonesia natural personal care" must not offer
    "India cosmetics". A module is the same market elsewhere, nothing looser."""
    assert _module(_source("india"), _cand("germany", core="cosmetics")) == ()


def test_a_candidate_without_a_market_core_is_skipped() -> None:
    assert _module(_source("india"), _cand("germany", core=None)) == ()


def test_the_same_geography_is_not_a_sibling() -> None:
    """It would send the reader to the market they are already reading about."""
    assert _module(_source("india"), _cand("india")) == ()


def test_a_global_target_is_a_sibling_of_a_global_source_only_once() -> None:
    """Both global = same geography = not a sibling."""
    assert _module(_source(None), _cand(None)) == ()


def test_the_source_is_never_in_its_own_module() -> None:
    source = _source("india")
    same_page = ModuleCandidate(
        page_id=source.page_id,
        url="/industry-reports/india-ev-market",
        title="India EV Market",
        market_core="ev",
        geography=_geo("germany"),
    )
    assert _module(source, same_page) == ()


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def test_a_containing_region_outranks_global_outranks_a_neighbour() -> None:
    """An India reader is served best by Asia Pacific (a superset of India), then by a
    global view, then by a neighbour in the same region, then by another region."""
    module = _module(
        _source("india"),
        _cand("germany", url="/d-other-region"),
        _cand("vietnam", url="/c-same-region"),
        _cand(None, url="/b-global"),
        _cand("asia-pacific", url="/a-containing"),
    )
    assert [item.url for item in module] == [
        "/a-containing",
        "/b-global",
        "/c-same-region",
        "/d-other-region",
    ]


def test_the_global_root_does_not_make_every_pair_share_a_region() -> None:
    """Regression. ``global`` is an ancestor of every geography, so an ancestry test that
    keeps it makes "same region" true for ALL pairs: every candidate collapses into one
    tier and the module degenerates to alphabetical order by URL. That is what the first
    corpus run produced for an Oman source -- Asia Pacific ranked first for a Middle East
    reader, purely because "asia-pacific" sorts early."""
    module = _module(
        _source("uae"),
        _cand("asia-pacific", url="/a-other-region"),
        _cand("qatar", url="/z-same-region"),
    )
    # Qatar shares the Middle East with the UAE; Asia Pacific shares only the global root.
    assert [item.url for item in module] == ["/z-same-region", "/a-other-region"]


def test_a_containing_region_is_found_through_the_hierarchy() -> None:
    """India sits under south-asia, which sits under asia-pacific: both contain India."""
    module = _module(
        _source("india"),
        _cand("asia-pacific", url="/apac"),
        _cand("south-asia", url="/south-asia"),
        _cand("germany", url="/germany"),
    )
    assert [item.url for item in module][:2] == ["/apac", "/south-asia"]
    assert module[-1].url == "/germany"


def test_an_orphan_target_comes_first_within_its_tier() -> None:
    """Spreading authority rather than piling it on already-linked pages."""
    module = _module(
        _source("india"),
        _cand("vietnam", inbound=40, url="/well-linked"),
        _cand("thailand", inbound=0, url="/orphan"),
    )
    assert [item.url for item in module] == ["/orphan", "/well-linked"]
    assert ReasonCode.ORPHAN_TARGET in module[0].reason_codes
    assert ReasonCode.ORPHAN_TARGET not in module[1].reason_codes


def test_commercial_importance_then_similarity_break_the_tie() -> None:
    module = _module(
        _source("india"),
        _cand("vietnam", commercial=1, similarity=0.9, url="/low-value"),
        _cand("thailand", commercial=5, similarity=0.1, url="/high-value"),
    )
    assert [item.url for item in module] == ["/high-value", "/low-value"]

    by_similarity = _module(
        _source("india"),
        _cand("vietnam", commercial=3, similarity=0.2, url="/less-similar"),
        _cand("thailand", commercial=3, similarity=0.8, url="/more-similar"),
    )
    assert [item.url for item in by_similarity] == ["/more-similar", "/less-similar"]


def test_a_missing_similarity_never_outranks_a_real_one() -> None:
    module = _module(
        _source("india"),
        _cand("vietnam", similarity=None, url="/unknown"),
        _cand("thailand", similarity=0.05, url="/known-low"),
    )
    assert [item.url for item in module] == ["/known-low", "/unknown"]


def test_the_order_does_not_depend_on_the_input_order() -> None:
    a = _cand("vietnam", url="/a")
    b = _cand("thailand", url="/b")
    assert [i.url for i in _module(_source("india"), a, b)] == [
        i.url for i in _module(_source("india"), b, a)
    ]


def test_the_module_is_capped_and_ranks_from_one() -> None:
    candidates = [_cand("vietnam", url=f"/p{n:02d}") for n in range(20)]
    module = regional_sibling_module(_source("india"), candidates, taxonomy=TAX, limit=4)
    assert len(module) == 4
    assert [item.rank for item in module] == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# Explainability and summary
# --------------------------------------------------------------------------- #
def test_every_item_says_why_it_is_there() -> None:
    module = _module(_source("india"), _cand("asia-pacific"), _cand(None))
    assert module
    for item in module:
        assert ReasonCode.REGIONAL_SIBLING_MARKET in item.reason_codes
        assert item.geography_label is not None or item.url
    assert ReasonCode.GLOBAL_TARGET in module[1].reason_codes


def test_module_sizes_summarises_a_run() -> None:
    full = [
        ModuleItem(uuid.uuid4(), f"/x{n}", "t", "India", n + 1, ())
        for n in range(DEFAULT_MODULE_SIZE)
    ]
    counts = module_sizes([full, full[:2], []])
    assert counts == {
        "pages_considered": 3,
        "pages_with_a_module": 2,
        "items_total": DEFAULT_MODULE_SIZE + 2,
        "pages_with_a_full_module": 1,
    }
