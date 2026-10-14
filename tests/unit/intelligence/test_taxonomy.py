"""Taxonomy loading, caching, validation and the precompiled index."""

from __future__ import annotations

import os
import shutil
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ken_contracts.intelligence import TaxonomyPolicy
from ken_services.intelligence.taxonomy import (
    TaxonomyError,
    load_taxonomy,
    taxonomy_index,
)
from ken_services.intelligence.text import fold
from tests.unit.intelligence._support import V1_PATH, mini_policy_data, v1


def test_v1_loads_and_validates() -> None:
    policy = v1()
    assert policy.version == 1
    assert policy.default_page_type == "other"
    assert len(policy.markets) > 20


def test_load_is_cached_by_path_and_mtime(tmp_path: Path) -> None:
    copy = tmp_path / "taxonomy.yaml"
    shutil.copy(V1_PATH, copy)
    first = load_taxonomy(copy)
    assert load_taxonomy(str(copy)) is first  # str and Path resolve to the same key
    stat = copy.stat()
    os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = load_taxonomy(copy)
    assert second is not first
    assert second == first


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_taxonomy(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("markets: [unclosed", "not valid YAML"),
        ("- a\n- b\n", "must contain a mapping"),
        ("", "must contain a mapping"),
        ("policy: taxonomy\nversion: 1\n", "invalid"),
    ],
)
def test_invalid_files(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TaxonomyError, match=message):
        load_taxonomy(path)


def test_referential_integrity_is_enforced(tmp_path: Path) -> None:
    data = mini_policy_data()
    data["markets"][1]["parent"] = "no-such-market"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(TaxonomyError):
        load_taxonomy(path)
    assert isinstance(TaxonomyError("x"), ValueError)


def test_parent_cycle_is_rejected(tmp_path: Path) -> None:
    data = mini_policy_data()
    data["markets"][0]["parent"] = "smart-widgets"  # tech-ind -> smart-widgets -> widgets -> ...
    # The contract rejects cycles at validation time; the index keeps its own guard
    # for policies built without validation (model_construct).
    with pytest.raises(ValidationError, match="cycle"):
        TaxonomyPolicy.model_validate(data)
    with pytest.raises(TaxonomyError, match="cycle"):
        taxonomy_index(
            TaxonomyPolicy.model_construct(
                **TaxonomyPolicy.model_validate(mini_policy_data()).__dict__
                | {
                    "markets": tuple(
                        type(m).model_construct(**(m.__dict__ | {"parent": "smart-widgets"}))
                        if i == 0
                        else m
                        for i, m in enumerate(
                            TaxonomyPolicy.model_validate(mini_policy_data()).markets
                        )
                    )
                }
            )
        )
    path = tmp_path / "cyclic.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(TaxonomyError, match="invalid"):
        load_taxonomy(path)


def test_index_is_cached_per_policy_object() -> None:
    policy = v1()
    assert taxonomy_index(policy) is taxonomy_index(policy)
    clone = TaxonomyPolicy.model_validate(policy.model_dump())
    assert taxonomy_index(clone) is not taxonomy_index(policy)


def test_market_tree_helpers() -> None:
    index = taxonomy_index(v1())
    assert index.market_depth("automotive") == 0
    assert index.market_depth("electric-vehicles") == 1
    assert index.market_depth("electric-two-wheelers") == 2
    assert index.market_ancestors("electric-two-wheelers") == ("electric-vehicles", "automotive")
    assert index.market_ancestors("automotive") == ()
    assert index.market_root("electric-two-wheelers") == "automotive"
    assert index.market_root("automotive") == "automotive"
    assert index.market_parent("ev-charging") == "electric-vehicles"
    assert index.has_market("solar-power")
    assert not index.has_market("nope")
    with pytest.raises(KeyError):
        index.market_depth("nope")


def test_geography_helpers() -> None:
    index = taxonomy_index(v1())
    assert index.geography_ancestors("india") == ("south-asia", "asia-pacific", "global")
    assert index.geography_ancestors("global") == ()
    assert index.geography("uae").kind == "country"


@pytest.mark.parametrize(
    ("path", "page_type"),
    [
        ("/", "hub"),
        ("", "hub"),
        ("/reports", "hub"),
        ("/reports/", "hub"),
        ("/industry-reports/", "hub"),
        ("/insights", "hub"),
        ("/industries/automotive", "hub"),
        ("/tags", "hub"),
        ("/reports/india-ev-market", "report"),
        ("/industry-reports/sea-solar", "report"),
        ("/insights/ev-outlook", "insight"),
        ("/blog/post", "insight"),
        ("/news/2026/x", "insight"),
        ("/contact", "transactional"),
        ("/request-sample/x", "transactional"),
        ("/pricing", "transactional"),
        ("/about/", "corporate"),
        ("/privacy", "corporate"),
        ("/cookie-policy", "corporate"),
        ("/something-else", "other"),
        ("/reportsx", "other"),  # not a /reports path
    ],
)
def test_classify_path_every_v1_rule(path: str, page_type: str) -> None:
    assert taxonomy_index(v1()).classify_path(path).id == page_type


def test_classify_path_falls_back_to_default() -> None:
    policy = TaxonomyPolicy.model_validate(mini_policy_data())
    index = taxonomy_index(policy)
    assert index.classify_path("/reports/a").id == "report"
    assert index.classify_path("/anything").id == "misc"
    assert index.page_type("report").commercial_importance == 3


def test_v1_has_no_phrase_claimed_by_two_nodes() -> None:
    policy = v1()
    for nodes in (policy.markets, policy.geographies, policy.entities):
        counts = Counter(
            phrase for node in nodes for phrase in {fold(a) for a in (node.label, *node.aliases)}
        )
        assert [p for p, n in counts.items() if n > 1] == []
