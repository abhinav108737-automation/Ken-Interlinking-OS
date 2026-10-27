"""Taxonomy v2 classifies the real www.kenresearch.com URL layout (seen in the operator's GA4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_core.config import REPO_ROOT
from ken_services.intelligence.pipeline import load_policy
from ken_services.intelligence.taxonomy import TaxonomyIndex

V2 = Path(REPO_ROOT) / "policies" / "taxonomy" / "taxonomy.v2.yaml"


@pytest.fixture(scope="module")
def index() -> TaxonomyIndex:
    return TaxonomyIndex(load_policy(str(V2)))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "hub"),
        ("/articles", "hub"),
        ("/africa-chia-seeds-market", "report"),
        ("/africa-construction-market", "report"),
        ("/reports/anything", "report"),
        ("/articles/egypt-gypsum-board-export-growth-opportunity", "insight"),
        ("/about-us", "corporate"),
        ("/contact-us", "transactional"),
        ("/some-unlisted-page", "other"),
    ],
)
def test_real_layout(index: TaxonomyIndex, path: str, expected: str) -> None:
    assert index.classify_path(path).id == expected


def test_v1_is_untouched_and_v2_is_version_two() -> None:
    assert load_policy(str(V2)).version == 2
    v1 = Path(REPO_ROOT) / "policies" / "taxonomy" / "taxonomy.v1.yaml"
    assert load_policy(str(v1)).version == 1
