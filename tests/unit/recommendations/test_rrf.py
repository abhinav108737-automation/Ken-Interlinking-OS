from __future__ import annotations

import pytest

from ken_services.recommendations.rrf import precision_at_k, recall_at_k, reciprocal_rank_fusion


def test_rrf_rewards_candidates_seen_by_multiple_channels() -> None:
    fused = reciprocal_rank_fusion(
        {"semantic": ["a", "b", "c"], "lexical": ["b", "d", "a"]}, rank_constant=60
    )
    assert [item.candidate_id for item in fused[:2]] == ["b", "a"]
    assert fused[0].ranks == {"lexical": 1, "semantic": 2}


def test_rrf_deduplicates_within_a_channel_and_breaks_ties_by_id() -> None:
    fused = reciprocal_rank_fusion({"one": ["b", "b", "a"]}, rank_constant=10)
    assert [item.candidate_id for item in fused] == ["b", "a"]
    assert fused[0].ranks == {"one": 1}
    assert fused[1].ranks == {"one": 3}


def test_rrf_limit_and_metric_helpers() -> None:
    fused = reciprocal_rank_fusion({"one": ["a", "b", "c"]}, limit=2)
    assert [item.candidate_id for item in fused] == ["a", "b"]
    assert precision_at_k(["a", "b"], {"a"}, k=2) == 0.5
    assert recall_at_k(["a", "b"], {"a", "c"}, k=2) == 0.5


@pytest.mark.parametrize("rank_constant", [0, -1])
def test_rrf_rejects_invalid_rank_constant(rank_constant: int) -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion({"one": ["a"]}, rank_constant=rank_constant)
