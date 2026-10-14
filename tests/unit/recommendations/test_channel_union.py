"""The candidate cap ranks scale-free: no channel dominates because of its units."""

from __future__ import annotations

import uuid

from ken_contracts.recommendations import RetrievalChannel
from ken_services.recommendations.channels import union_candidates

SOURCE = uuid.UUID(int=0)


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i) for i in range(1, n + 1)]


def test_lexical_scale_does_not_crowd_out_semantic() -> None:
    a, b, c, d = _ids(4)
    found = {
        # ts_rank_cd values are typically several times larger than cosines.
        RetrievalChannel.LEXICAL: {a: 8.0, b: 7.5},
        RetrievalChannel.SEMANTIC: {c: 0.92, d: 0.90},
    }
    kept = {x.target_page_id for x in union_candidates(found, source_page_id=SOURCE, cap=2)}
    # Each channel's best hit survives; the raw-max rule would have kept only a and b.
    assert kept == {a, c}


def test_agreement_between_channels_ranks_first() -> None:
    a, b, c = _ids(3)
    found = {
        RetrievalChannel.LEXICAL: {a: 5.0, b: 5.0},
        RetrievalChannel.SEMANTIC: {b: 0.8, c: 0.8},
    }
    ranked = [x.target_page_id for x in union_candidates(found, source_page_id=SOURCE, cap=3)]
    assert ranked[0] == b


def test_raw_scores_are_kept_and_the_source_is_excluded() -> None:
    a, b = _ids(2)
    found = {RetrievalChannel.LEXICAL: {SOURCE: 9.0, a: 6.0}, RetrievalChannel.GRAPH: {b: 0.5}}
    kept = union_candidates(found, source_page_id=SOURCE, cap=10)
    assert {x.target_page_id for x in kept} == {a, b}
    assert next(x for x in kept if x.target_page_id == a).channel_scores == {"lexical": 6.0}
