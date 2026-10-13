"""Integrity of versioned policy data (TRD.md section 12)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"

TRD_SECTION_12_WEIGHTS = {
    "semantic": 0.30,
    "taxonomy": 0.20,
    "intent": 0.15,
    "graph": 0.15,
    "geography": 0.08,
    "commercial": 0.05,
    "underlinked": 0.04,
    "historical": 0.03,
}


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def test_ranking_v1_matches_trd_section_12_exactly() -> None:
    """v1 is the TRD baseline; a drift here means someone edited it in place."""
    policy = _load(POLICY_DIR / "ranking" / "ranking_weights.v1.yaml")
    assert policy["policy"] == "ranking"
    assert policy["version"] == 1
    assert policy["weights"] == TRD_SECTION_12_WEIGHTS


@pytest.mark.parametrize(
    "path", sorted((POLICY_DIR / "ranking").glob("ranking_weights.v*.yaml")), ids=lambda p: p.name
)
def test_every_ranking_policy_is_a_valid_distribution(path: Path) -> None:
    weights = _load(path)["weights"]
    assert all(isinstance(w, (int, float)) and w >= 0 for w in weights.values())
    assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9)


@pytest.mark.parametrize(
    "path", sorted((POLICY_DIR / "ranking").glob("ranking_weights.v*.yaml")), ids=lambda p: p.name
)
def test_policy_version_matches_its_filename(path: Path) -> None:
    assert path.stem == f"ranking_weights.v{_load(path)['version']}"
