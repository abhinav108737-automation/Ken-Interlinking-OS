"""``analysis_version_absent``: the guard that stops a four-hour silent no-op (item 28).

Eligibility requires a page's ``analysis_version`` to match the engine's, and that string is
derived from the loaded taxonomy. Point the engine at v1 while the corpus was analysed under v3
and every page is refused as ``stale_analysis`` -- yet the job used to exit ``succeeded``, with
the only signal buried in a rejection counter. One full-corpus run was wasted that way.

The guard has to tell three states apart, which is why each has a test here: "wrong version"
(fail), "never analysed" (a legitimate state, carry on) and "right version" (carry on).
"""

from __future__ import annotations

import uuid
from typing import Final

from ken_core.db.repositories.recommendations import PageFacts
from ken_services.recommendations.pipeline import analysis_version_absent
from tests.unit.recommendations._support import analysis

OURS: Final = "tax1-alg2"
"""What the engine would compute from the v1 taxonomy at the current algorithm version."""


def facts(*, analysed: str | None, has_any: bool | None = None) -> PageFacts:
    """One page's live facts. ``analysed`` is the version of its preferred analysis.

    ``has_any_analysis`` defaults to "it has one if we found one", and is settable on its own
    because the repository can return them that way: an analysis of an OLDER page version
    leaves ``analysis`` None while ``has_any_analysis`` stays true.
    """
    page_id = uuid.uuid4()
    preferred = None
    if analysed is not None:
        preferred = analysis(page_id=page_id, market_id="solar_power").model_copy(
            update={"analysis_version": analysed}
        )
    return PageFacts(
        id=page_id,
        canonical_url=f"https://example.test/{page_id}",
        title="A report",
        current_version=1,
        publication_state="published",
        gone=False,
        word_count=900,
        internal_link_count=2,
        robots_directives=(),
        canonical_status="self",
        http_status=200,
        protected=False,
        has_any_analysis=(analysed is not None) if has_any is None else has_any,
        analysis=preferred,
    )


def test_a_corpus_analysed_under_another_version_fails_the_run() -> None:
    corpus = [facts(analysed="tax3-alg2"), facts(analysed="tax3-alg2")]
    assert analysis_version_absent(corpus, analysis_version=OURS) is True


def test_one_matching_page_is_enough_to_proceed() -> None:
    """Not every page need match: a partially re-analysed corpus is a real, workable run."""
    corpus = [facts(analysed="tax3-alg2"), facts(analysed=OURS), facts(analysed=None)]
    assert analysis_version_absent(corpus, analysis_version=OURS) is False


def test_a_corpus_that_was_never_analysed_is_not_a_failure() -> None:
    """A fresh install, or the inventory running ahead of analysis. Nothing is misconfigured."""
    corpus = [facts(analysed=None), facts(analysed=None)]
    assert analysis_version_absent(corpus, analysis_version=OURS) is False


def test_an_empty_corpus_is_not_a_failure() -> None:
    assert analysis_version_absent((), analysis_version=OURS) is False


def test_an_analysis_of_an_older_page_version_still_counts_as_analysed() -> None:
    """``analysis`` is None but the page HAS an analysis: a re-crawl without a re-analysis.

    The run would produce nothing, so it fails rather than reporting success -- the operator
    hears it in seconds instead of hours.
    """
    corpus = [facts(analysed=None, has_any=True)]
    assert analysis_version_absent(corpus, analysis_version=OURS) is True


def test_the_version_comparison_is_explicit_not_a_none_test() -> None:
    """Regression: ``PageFacts.analysis`` only PREFERS our version.

    The repository falls back to the newest analysis of ANY version, so a non-None
    ``analysis`` does not mean the versions agree. The first draft of this guard tested for
    None and would have let a wholly stale corpus through.
    """
    stale = facts(analysed="tax1-alg1")
    assert stale.analysis is not None
    assert analysis_version_absent([stale], analysis_version=OURS) is True
