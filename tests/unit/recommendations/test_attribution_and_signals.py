"""Directive sections 6 and 10: attribution per generator, separate signals, analytics that
can only block, and a ranker that explains itself."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

import pytest

from ken_contracts.recommendations import (
    AnalyticsPriorityPolicy,
    CandidateView,
    GraphSignals,
    RetrievalChannel,
)
from ken_services.recommendations.channels import RetrievedCandidate, union_candidates
from ken_services.recommendations.generators import (
    AnalyticsBlockCode,
    AnalyticsCandidateGenerator,
    AnalyticsSupportGate,
    GraphCandidateGenerator,
    LexicalCandidateGenerator,
    OrphanRecoveryCandidateGenerator,
    SemanticCandidateGenerator,
    TopicCandidateGenerator,
    default_generators,
)
from ken_services.recommendations.scoring import (
    PairFeatures,
    explain_ranking,
    score_pair,
    with_placement,
)
from tests.unit.recommendations._support import analysis, policy, ranking, taxonomy

SOURCE = uuid.UUID(int=0)
A, B, C = (uuid.UUID(int=i) for i in (1, 2, 3))
S, L, AN, OR = (
    RetrievalChannel.SEMANTIC,
    RetrievalChannel.LEXICAL,
    RetrievalChannel.ANALYTICS,
    RetrievalChannel.ORPHAN_RECOVERY,
)


def test_there_is_one_generator_per_channel_in_declaration_order() -> None:
    generators = default_generators()
    assert [g.channel for g in generators] == list(RetrievalChannel)
    kinds = {type(g) for g in generators}
    assert kinds == {
        SemanticCandidateGenerator,
        LexicalCandidateGenerator,
        TopicCandidateGenerator,
        GraphCandidateGenerator,
        AnalyticsCandidateGenerator,
        OrphanRecoveryCandidateGenerator,
    }


def test_every_candidate_records_which_generators_nominated_it() -> None:
    found = {S: {A: 0.9, B: 0.8}, L: {A: 6.0}, OR: {B: 0.4}}
    kept = {c.target_page_id: c for c in union_candidates(found, source_page_id=SOURCE, cap=10)}
    assert kept[A].channels == (S, L) and kept[B].channels == (S, OR)
    for candidate in kept.values():
        assert [x.channel for x in candidate.attribution] == list(candidate.channels)
        # attribution is a valid CandidateView attribution (one entry per retrieving channel)
        CandidateView(
            run_id=uuid.uuid4(),
            source_page_id=SOURCE,
            target_page_id=candidate.target_page_id,
            target_url="https://x/",
            channels=candidate.channels,
            channel_scores=candidate.channel_scores,
            attribution=candidate.attribution,
        )
    a_semantic = kept[A].attribution[0]
    assert (a_semantic.raw, a_semantic.rank_in_channel, a_semantic.normalised) == (0.9, 1, 1.0)
    assert kept[B].attribution[0].rank_in_channel == 2
    assert kept[B].attribution[0].normalised == pytest.approx(0.8 / 0.9)


def test_attribution_carries_the_generators_evidence_and_signals_stay_separate() -> None:
    kept = union_candidates(
        {S: {A: 0.9}, L: {A: 6.0}},
        source_page_id=SOURCE,
        cap=5,
        evidence={S: {A: {"point_id": "p"}}, L: {A: {"keywords": 3}}},
        signals={A: {"semantic_similarity": 0.9, "lexical_similarity": 6.0}},
    )[0]
    assert kept.attribution[0].evidence == {"point_id": "p"}
    assert kept.attribution[1].evidence == {"keywords": 3}
    assert kept.signals == {"semantic_similarity": 0.9, "lexical_similarity": 6.0}


def test_analytics_never_adds_to_the_ranking_of_a_candidate_another_channel_found() -> None:
    """A is found by semantic only; B by semantic AND analytics. With equal semantic scores the
    analytics hit must NOT lift B above A -- it surfaces, it does not justify."""
    found = {S: {A: 0.9, B: 0.9}, AN: {B: 0.99}}
    ranked = [c.target_page_id for c in union_candidates(found, source_page_id=SOURCE, cap=2)]
    assert ranked == [A, B]  # tie broken by id, not by analytics agreement


def test_an_analytics_only_candidate_is_ranked_by_its_non_analytics_support() -> None:
    found = {S: {A: 0.5}, AN: {B: 5.0, C: 9.0}}
    ranked = union_candidates(found, source_page_id=SOURCE, cap=3, support={B: 0.95, C: 0.2})
    # A's normalised semantic share is 1.0; B and C rank by their NON-analytics support.
    assert [c.target_page_id for c in ranked][:1] == [A]
    assert [c.target_page_id for c in ranked][1:] == [B, C]  # by support, not by analytics raw


def test_the_analytics_gate_can_only_block_and_exposes_one_method() -> None:
    public = {
        name
        for name, member in inspect.getmembers(AnalyticsSupportGate)
        if not name.startswith("_") and inspect.isfunction(member)
    }
    assert public == {"blocking_reasons"}


def test_the_analytics_gate_blocks_unsupported_analytics_only_targets() -> None:
    gate = AnalyticsSupportGate(frozenset({A, B, C}), {A: 0.0, B: 0.7})
    assert gate.blocking_reasons() == (
        (A, AnalyticsBlockCode.ANALYTICS_ONLY_WITHOUT_SUPPORT),
        (C, AnalyticsBlockCode.ANALYTICS_ONLY_WITHOUT_SUPPORT),
    )
    assert AnalyticsSupportGate(frozenset(), {}).blocking_reasons() == ()


def test_a_non_analytics_candidate_is_never_touched_by_the_gate() -> None:
    kept = union_candidates({S: {A: 0.8}}, source_page_id=SOURCE, cap=5)
    assert [type(c) for c in kept] == [RetrievedCandidate]


def _graph(in_degree: int) -> GraphSignals:
    return GraphSignals(
        page_id=uuid.uuid4(),
        page_version=1,
        in_degree=in_degree,
        out_degree=2,
        internal_pagerank=0.2,
        hub_score=0.1,
        authority_score=0.3,
        crawl_depth=2,
        is_orphan=in_degree == 0,
        is_nearly_orphan=in_degree <= 1,
        is_bridge=False,
        component_id=0,
        cluster_id=1,
        metrics_version="graph-alg1-pol1",
        computed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _features(**extra: object) -> PairFeatures:
    return PairFeatures(
        cosine=0.9,
        source=analysis(commercial_importance=2, market_id="ev"),
        target=analysis(commercial_importance=3, market_id="ev"),
        source_links_into_target_market=0,
        target_inbound_links=1,
        **extra,  # type: ignore[arg-type]
    )


def _score(features: PairFeatures):  # type: ignore[no-untyped-def]
    return score_pair(
        features, policy=policy(), ranking=ranking(), taxonomy=taxonomy(), semantic_floor=0.5
    )


def test_signals_are_reported_separately_by_name() -> None:
    scored = _score(
        _features(
            lexical_similarity=4.2,
            graph=_graph(1),
            orphan_recovery_value=0.4,
            gsc_opportunity=0.7,
            ga4_engagement=0.3,
        )
    )
    s = scored.signals
    assert s["semantic_similarity"] == 0.9 and s["lexical_similarity"] == 4.2
    assert s["destination_priority"] == 1.0 and "topic_relevance" in s
    assert s["graph_in_degree"] == 1.0 and s["graph_pagerank"] == 0.2
    assert (s["gsc_opportunity"], s["ga4_engagement"], s["orphan_recovery_value"]) == (
        0.7,
        0.3,
        0.4,
    )


def test_analytics_and_graph_signals_do_not_change_the_score() -> None:
    """Recorded, not weighted: the relevance is identical with or without them."""
    plain = _score(_features())
    loud = _score(
        _features(
            lexical_similarity=9.0,
            graph=_graph(0),
            orphan_recovery_value=1.0,
            gsc_opportunity=1.0,
            ga4_engagement=1.0,
        )
    )
    assert loud.breakdown.relevance == plain.breakdown.relevance
    assert loud.breakdown.components == plain.breakdown.components
    assert loud.priority_score == loud.breakdown.relevance


def test_v2_analytics_priority_reorders_without_changing_relevance_or_confidence() -> None:
    proposed = ranking().model_copy(
        update={
            "version": 2,
            "analytics_priority": AnalyticsPriorityPolicy(
                enabled=True,
                max_boost=0.05,
                weights={"gsc_opportunity": 0.7, "ga4_engagement": 0.3},
            ),
        }
    )
    plain = score_pair(
        _features(), policy=policy(), ranking=proposed, taxonomy=taxonomy(), semantic_floor=0.5
    )
    measured = score_pair(
        _features(gsc_opportunity=0.8, ga4_engagement=0.4),
        policy=policy(),
        ranking=proposed,
        taxonomy=taxonomy(),
        semantic_floor=0.5,
    )
    assert measured.breakdown == plain.breakdown
    assert measured.analytics_priority == pytest.approx(0.68)
    assert measured.priority_score == pytest.approx(measured.breakdown.relevance + 0.034)
    final = with_placement(measured.breakdown, 0.8)
    why = explain_ranking(
        final,
        measured,
        channels=(S, AN),
        attribution=(),
        recommendation_policy_version=2,
    )
    assert why.final_score == why.confidence == final.total
    assert why.priority_score > why.final_score
    assert why.analytics_priority == pytest.approx(0.68)


def test_v3_authority_transfer_reorders_only_an_eligible_relevant_pair() -> None:
    proposed = ranking().model_copy(
        update={
            "version": 3,
            "analytics_priority": AnalyticsPriorityPolicy(
                enabled=True,
                max_boost=0.05,
                weights={
                    "gsc_opportunity": 0.45,
                    "ga4_engagement": 0.20,
                    "authority_transfer": 0.35,
                },
            ),
        }
    )
    plain = score_pair(
        _features(), policy=policy(), ranking=proposed, taxonomy=taxonomy(), semantic_floor=0.5
    )
    transfer = score_pair(
        _features(
            gsc_opportunity=0.4,
            ga4_engagement=0.5,
            source_authority=0.9,
            target_search_need=0.8,
            authority_transfer=0.72,
        ),
        policy=policy(),
        ranking=proposed,
        taxonomy=taxonomy(),
        semantic_floor=0.5,
    )
    expected = 0.45 * 0.4 + 0.20 * 0.5 + 0.35 * 0.72
    assert transfer.breakdown == plain.breakdown
    assert transfer.analytics_priority == pytest.approx(expected)
    assert transfer.priority_score == pytest.approx(transfer.breakdown.relevance + expected * 0.05)
    assert transfer.signals["source_authority"] == 0.9
    assert transfer.signals["target_search_need"] == 0.8
    assert transfer.signals["authority_transfer"] == 0.72


def test_the_explanation_carries_score_signals_policy_confidence_and_sources() -> None:
    scored = _score(_features(gsc_opportunity=0.6))
    final = with_placement(scored.breakdown, 0.8)
    kept = union_candidates({S: {A: 0.9}, L: {A: 3.0}}, source_page_id=SOURCE, cap=1)[0]
    why = explain_ranking(
        final,
        scored,
        channels=kept.channels,
        attribution=kept.attribution,
        recommendation_policy_version=policy().version,
    )
    assert why.final_score == final.total == pytest.approx(scored.breakdown.relevance * 0.8)
    assert why.confidence == why.final_score and why.calibration_version is None  # RAW
    assert why.ranking_policy_version == ranking().version
    assert why.recommendation_policy_version == policy().version
    assert why.sources == (S, L) and len(why.attribution) == 2
    assert why.factors == {"placement_quality": 0.8}
    assert why.signals["gsc_opportunity"] == 0.6
    assert sum(c.contribution for c in why.components) == pytest.approx(scored.breakdown.relevance)
