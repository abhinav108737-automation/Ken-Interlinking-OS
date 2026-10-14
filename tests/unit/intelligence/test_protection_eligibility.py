"""Policy protections and read-time link eligibility."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ken_contracts.intelligence import IneligibilityReason, ProtectionSource, TaxonomyPolicy
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility
from ken_services.intelligence.protection import match_protections
from tests.unit.intelligence._support import mini_policy_data, v1

R = IneligibilityReason


# --------------------------------------------------------------------------- #
# Protections
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", ["/privacy", "/privacy/", "/terms/of-use", "/cookie-policy", "/legal"]
)
def test_legal_paths_are_protected(path: str) -> None:
    matches = match_protections(url=f"https://site.test{path}", metadata={}, taxonomy=v1())
    assert [(m.rule_id, m.source) for m in matches] == [("legal-pages", ProtectionSource.POLICY)]
    assert matches[0].reason.startswith("Legal")


@pytest.mark.parametrize(
    "path", ["/about", "/reports/privacy-tech-market", "/privacyx", "/blog/terms"]
)
def test_other_paths_are_not_protected(path: str) -> None:
    assert match_protections(url=f"https://site.test{path}", metadata={}, taxonomy=v1()) == ()


def test_cms_protected_metadata() -> None:
    url = "https://site.test/reports/x"
    hit = match_protections(url=url, metadata={"cms_protected": "true"}, taxonomy=v1())
    assert [m.rule_id for m in hit] == ["cms-protected"]
    for value in ("True", "false", "1", ""):
        assert match_protections(url=url, metadata={"cms_protected": value}, taxonomy=v1()) == ()
    assert match_protections(url=url, metadata={"other": "true"}, taxonomy=v1()) == ()


def test_both_rules_in_declaration_order() -> None:
    matches = match_protections(
        url="https://site.test/terms", metadata={"cms_protected": "true"}, taxonomy=v1()
    )
    assert [m.rule_id for m in matches] == ["legal-pages", "cms-protected"]


def test_rule_with_path_and_metadata_matches_either_and_needs_all_pairs() -> None:
    data = mini_policy_data()
    data["protections"] = [
        {
            "id": "combo",
            "reason": "test",
            "path_patterns": ["^/locked/"],
            "metadata_equals": {"a": "1", "b": "2"},
        }
    ]
    policy = TaxonomyPolicy.model_validate(data)
    hit = match_protections(url="https://s.test/locked/x", metadata={}, taxonomy=policy)
    assert [m.rule_id for m in hit] == ["combo"]
    both = {"a": "1", "b": "2"}
    assert match_protections(url="https://s.test/open", metadata=both, taxonomy=policy)
    assert not match_protections(url="https://s.test/open", metadata={"a": "1"}, taxonomy=policy)


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
OK = EligibilityState(
    publication_state=PublicationState.PUBLISHED,
    gone=False,
    protected=False,
    robots_directives=("follow", "index"),
    canonical_status=CanonicalStatus.SELF,
    http_status=200,
    word_count=500,
    page_type="report",
    has_analysis=True,
    analysis_fresh=True,
)


def _reasons(state: EligibilityState) -> tuple[list[R], list[R]]:
    result = compute_eligibility(state, taxonomy=v1())
    assert result.as_source is (not result.source_reasons)
    assert result.as_target is (not result.target_reasons)
    return result.source_reasons, result.target_reasons


def test_fully_eligible() -> None:
    result = compute_eligibility(OK, taxonomy=v1())
    assert (result.as_source, result.as_target) == (True, True)
    assert result.source_reasons == result.target_reasons == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"publication_state": PublicationState.DRAFT}, R.NOT_PUBLISHED),
        ({"publication_state": PublicationState.UNPUBLISHED}, R.NOT_PUBLISHED),
        ({"publication_state": PublicationState.UNKNOWN}, R.NOT_PUBLISHED),
        ({"gone": True}, R.GONE),
        ({"protected": True}, R.PROTECTED),
        ({"robots_directives": ("noindex",)}, R.NOINDEX),
        ({"robots_directives": ("none",)}, R.NOINDEX),
        ({"robots_directives": ("NOINDEX", "follow")}, R.NOINDEX),
        ({"canonical_status": CanonicalStatus.DECLARED}, R.NOT_CANONICAL),
        ({"http_status": 404}, R.HTTP_NOT_OK),
        ({"http_status": 301}, R.HTTP_NOT_OK),
        ({"word_count": 0}, R.THIN_CONTENT),
        ({"page_type": "transactional"}, R.PAGE_TYPE_EXCLUDED),
        ({"page_type": "corporate"}, R.PAGE_TYPE_EXCLUDED),
        ({"has_analysis": False, "analysis_fresh": False}, R.NOT_ANALYZED),
        ({"analysis_fresh": False}, R.STALE_ANALYSIS),
    ],
)
def test_each_reason_blocks_both_roles(changes: dict[str, object], reason: R) -> None:
    assert _reasons(replace(OK, **changes)) == ([reason], [reason])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"robots_directives": ("nofollow",)},
        {"canonical_status": CanonicalStatus.MISSING},
        {"canonical_status": CanonicalStatus.OUT_OF_SCOPE},
        {"canonical_status": CanonicalStatus.INVALID},
        {"http_status": None},
        {"page_type": None},
        {"page_type": "not-in-this-taxonomy"},
        {"page_type": "insight"},
        {"page_type": "other"},
    ],
)
def test_harmless_states(changes: dict[str, object]) -> None:
    assert _reasons(replace(OK, **changes)) == ([], [])  # type: ignore[arg-type]


def test_thin_content_thresholds_differ_per_role() -> None:
    policy = v1()
    assert (policy.eligibility.min_words_source, policy.eligibility.min_words_target) == (120, 60)
    assert _reasons(replace(OK, word_count=120)) == ([], [])
    assert _reasons(replace(OK, word_count=119)) == ([R.THIN_CONTENT], [])
    assert _reasons(replace(OK, word_count=60)) == ([R.THIN_CONTENT], [])
    assert _reasons(replace(OK, word_count=59)) == ([R.THIN_CONTENT], [R.THIN_CONTENT])


def test_hub_is_target_only() -> None:
    assert _reasons(replace(OK, page_type="hub")) == ([R.PAGE_TYPE_EXCLUDED], [])


def test_every_reason_at_once_in_declaration_order() -> None:
    worst = EligibilityState(
        publication_state=PublicationState.DRAFT,
        gone=True,
        protected=True,
        robots_directives=("noindex",),
        canonical_status=CanonicalStatus.DECLARED,
        http_status=410,
        word_count=0,
        page_type="transactional",
        has_analysis=False,
        analysis_fresh=False,
    )
    expected = [r for r in IneligibilityReason if r is not R.STALE_ANALYSIS]
    assert _reasons(worst) == (expected, expected)
    stale = replace(worst, has_analysis=True)
    expected_stale = [r for r in IneligibilityReason if r is not R.NOT_ANALYZED]
    assert _reasons(stale) == (expected_stale, expected_stale)


def test_not_analyzed_wins_over_stale() -> None:
    # analysis_fresh=True without an analysis is contradictory; not_analyzed still applies.
    assert _reasons(replace(OK, has_analysis=False)) == ([R.NOT_ANALYZED], [R.NOT_ANALYZED])
