"""Recommendation / ranking policy loading: validation, caching, errors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from ken_contracts.recommendations import RANKING_COMPONENTS, RetrievalChannel
from ken_services.recommendations import policy as policy_module
from ken_services.recommendations.policy import (
    PolicyError,
    load_ranking_policy,
    load_recommendation_policy,
    resolve_ranking_policy,
)
from tests.unit.recommendations._support import RANKING_V1, RECOMMENDATION_V1, REPO_ROOT

RANKING_V2 = REPO_ROOT / "policies" / "ranking" / "ranking_weights.v2.yaml"
RECOMMENDATION_V2 = REPO_ROOT / "policies" / "recommendation" / "recommendation.v2.yaml"
RANKING_V3 = REPO_ROOT / "policies" / "ranking" / "ranking_weights.v3.yaml"
RECOMMENDATION_V3 = REPO_ROOT / "policies" / "recommendation" / "recommendation.v3.yaml"


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    policy_module._clear_caches()


def _write(path: Path, data: Any) -> Path:
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


def _raw(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))
    return data


def test_v1_recommendation_policy_loads_with_its_values() -> None:
    loaded = load_recommendation_policy(RECOMMENDATION_V1)
    assert (loaded.policy, loaded.version) == ("recommendation", 1)
    assert loaded.selection.min_relevance == 0.30
    assert loaded.selection.min_score == 0.35
    assert loaded.placement.block_kinds == ("p", "li")
    assert "this article" in loaded.placement.forbidden_anchors
    # ADR-0018 added ANALYTICS and ORPHAN_RECOVERY to the enum, OFF by default: the
    # immutable v1 document still enables exactly the original four channels.
    assert loaded.retrieval.channels_enabled == (
        RetrievalChannel.SEMANTIC,
        RetrievalChannel.LEXICAL,
        RetrievalChannel.TAXONOMY,
        RetrievalChannel.GRAPH,
    )
    assert RetrievalChannel.ANALYTICS not in loaded.retrieval.channels_enabled
    assert loaded.retrieval.analytics_k == 0
    assert loaded.retrieval.orphan_recovery_k == 0
    assert loaded.ranking_policy == "policies/ranking/ranking_weights.v1.yaml"


def test_v1_ranking_policy_loads_and_weights_every_component() -> None:
    loaded = load_ranking_policy(RANKING_V1)
    assert (loaded.policy, loaded.version) == ("ranking", 1)
    assert tuple(loaded.weights) == RANKING_COMPONENTS
    assert loaded.weights["historical"] == 0.03
    assert not loaded.analytics_priority.enabled


def test_v2_enables_bounded_analytics_prioritisation_without_changing_relevance_weights() -> None:
    recommendation = load_recommendation_policy(RECOMMENDATION_V2)
    ranking_v2 = load_ranking_policy(RANKING_V2)
    assert recommendation.version == ranking_v2.version == 2
    assert RetrievalChannel.ANALYTICS in recommendation.retrieval.channels_enabled
    assert recommendation.retrieval.analytics_k == 50
    assert ranking_v2.weights == load_ranking_policy(RANKING_V1).weights
    assert ranking_v2.analytics_priority.enabled
    assert ranking_v2.analytics_priority.max_boost == 0.05
    assert resolve_ranking_policy(recommendation, root=REPO_ROOT) is ranking_v2


def test_v3_adds_directional_authority_transfer_without_changing_relevance_weights() -> None:
    recommendation = load_recommendation_policy(RECOMMENDATION_V3)
    ranking_v3 = load_ranking_policy(RANKING_V3)
    assert recommendation.version == ranking_v3.version == 3
    assert RetrievalChannel.ANALYTICS in recommendation.retrieval.channels_enabled
    assert RetrievalChannel.ORPHAN_RECOVERY in recommendation.retrieval.channels_enabled
    assert ranking_v3.weights == load_ranking_policy(RANKING_V1).weights
    assert ranking_v3.analytics_priority.weights["authority_transfer"] == 0.35
    assert ranking_v3.analytics_priority.max_boost == 0.05
    assert resolve_ranking_policy(recommendation, root=REPO_ROOT) is ranking_v3


def test_resolve_ranking_policy_follows_the_repo_relative_reference() -> None:
    loaded = resolve_ranking_policy(load_recommendation_policy(RECOMMENDATION_V1), root=REPO_ROOT)
    assert loaded is load_ranking_policy(RANKING_V1)


@pytest.mark.parametrize("reference", ["/etc/ranking.yaml", "../outside/ranking.yaml"])
def test_resolve_ranking_policy_refuses_absolute_or_escaping_paths(reference: str) -> None:
    base = load_recommendation_policy(RECOMMENDATION_V1)
    escaped = base.model_copy(update={"ranking_policy": reference})
    with pytest.raises(PolicyError):
        resolve_ranking_policy(escaped, root=REPO_ROOT)


def test_loads_are_cached_by_path_and_mtime(tmp_path: Path) -> None:
    path = _write(tmp_path / "rec.yaml", _raw(RECOMMENDATION_V1))
    first = load_recommendation_policy(path)
    assert load_recommendation_policy(str(path)) is first  # str and Path share the key

    changed = _raw(RECOMMENDATION_V1)
    changed["version"] = 2
    _write(path, changed)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = load_recommendation_policy(path)
    assert second is not first
    assert second.version == 2


def test_ranking_loads_are_cached(tmp_path: Path) -> None:
    path = _write(tmp_path / "rank.yaml", _raw(RANKING_V1))
    assert load_ranking_policy(path) is load_ranking_policy(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("policy: [unclosed", "not valid YAML"),
        ("- a\n- list\n", "must contain a mapping"),
        ("", "must contain a mapping"),
    ],
)
def test_malformed_files_raise_policy_error(tmp_path: Path, content: str, message: str) -> None:
    path = _write(tmp_path / "bad.yaml", content)
    with pytest.raises(PolicyError, match=message):
        load_recommendation_policy(path)
    with pytest.raises(PolicyError, match=message):
        load_ranking_policy(path)


def _bad_recommendation(**changes: Any) -> dict[str, Any]:
    raw = _raw(RECOMMENDATION_V1)
    raw.update(changes)
    return raw


@pytest.mark.parametrize(
    "raw",
    [
        _bad_recommendation(policy="ranking"),
        _bad_recommendation(intent_compatibility={"informational": {"informational": 1.5}}),
        _bad_recommendation(intent_compatibility={"not-an-intent": {"informational": 0.5}}),
        _bad_recommendation(geography_scores={"same_country": 2.0}),
        _bad_recommendation(selection={"min_relevance": 1.5}),
        _bad_recommendation(unexpected_key=True),
        {k: v for k, v in _raw(RECOMMENDATION_V1).items() if k != "page_type_targets"},
    ],
)
def test_invalid_recommendation_policy_raises(tmp_path: Path, raw: dict[str, Any]) -> None:
    path = _write(tmp_path / "rec.yaml", raw)
    with pytest.raises(PolicyError, match=r"recommendation policy file is invalid: \d+ error"):
        load_recommendation_policy(path)


@pytest.mark.parametrize(
    "weights",
    [
        {"semantic": 1.0},  # missing components
        {**dict.fromkeys(RANKING_COMPONENTS, 0.125), "semantic": 0.2},  # sums to 1.075
        {**dict.fromkeys(RANKING_COMPONENTS, 0.0), "semantic": 1.2, "taxonomy": -0.2},
    ],
)
def test_invalid_ranking_policy_raises(tmp_path: Path, weights: dict[str, float]) -> None:
    raw = _raw(RANKING_V1)
    raw["weights"] = weights
    path = _write(tmp_path / "rank.yaml", raw)
    with pytest.raises(PolicyError, match="ranking policy file is invalid"):
        load_ranking_policy(path)


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_recommendation_policy(tmp_path / "absent.yaml")
    with pytest.raises(FileNotFoundError):
        load_ranking_policy(tmp_path / "absent.yaml")


def test_error_messages_never_echo_file_contents(tmp_path: Path) -> None:
    path = _write(tmp_path / "rec.yaml", _bad_recommendation(source={"secret": "s3cr3t-value"}))
    with pytest.raises(PolicyError) as caught:
        load_recommendation_policy(path)
    assert "s3cr3t" not in str(caught.value)
