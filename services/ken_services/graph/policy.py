"""``policies/graph/graph.v<N>.yaml``: the versioned recipe of the link-graph metrics.

Weights, thresholds and iteration bounds live in policy data, not code constants (directive
sections 5 and 10). The ``metrics_version`` stored on every ``page_graph_metrics`` row combines
the ALGORITHM version (code, bumped on any behaviour change) with this file's ``version``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from ken_contracts.common import FrozenModel
from ken_core.config import REPO_ROOT

__all__ = [
    "GRAPH_ALGORITHM_VERSION",
    "GraphPolicy",
    "GraphPolicyError",
    "default_graph_policy_path",
    "load_graph_policy",
]

GRAPH_ALGORITHM_VERSION: Final = 1
"""Bump on any change to the algorithms in ``algorithms.py``."""


class GraphPolicyError(ValueError):
    """The graph policy file is missing, malformed or invalid."""


class PageRankPolicy(FrozenModel):
    damping: float = Field(default=0.85, gt=0.0, lt=1.0)
    tolerance: float = Field(default=1e-10, gt=0.0, le=1e-3)
    max_iterations: int = Field(default=200, ge=1, le=10_000)


class HitsPolicy(FrozenModel):
    tolerance: float = Field(default=1e-10, gt=0.0, le=1e-3)
    max_iterations: int = Field(default=200, ge=1, le=10_000)


class ClusterPolicy(FrozenModel):
    key: Literal["market_then_industry"] = "market_then_industry"


class GraphPolicy(FrozenModel):
    policy: Literal["graph"]
    version: int = Field(ge=1)
    status: str
    source: str
    pagerank: PageRankPolicy = Field(default_factory=PageRankPolicy)
    hits: HitsPolicy = Field(default_factory=HitsPolicy)
    nearly_orphan_max_inbound: int = Field(default=1, ge=0, le=100)
    entry_path_patterns: tuple[str, ...] = ("^/$",)
    clusters: ClusterPolicy = Field(default_factory=ClusterPolicy)

    @field_validator("entry_path_patterns")
    @classmethod
    def _compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

    @property
    def metrics_version(self) -> str:
        """Algorithm + policy recipe, e.g. ``graph-alg1-pol1`` (<= 64 chars, DB-checked)."""
        return f"graph-alg{GRAPH_ALGORITHM_VERSION}-pol{self.version}"


def default_graph_policy_path() -> Path:
    return REPO_ROOT / "policies" / "graph" / "graph.v1.yaml"


def load_graph_policy(path: str | Path | None = None) -> GraphPolicy:
    """Load and validate the graph policy.

    Raises:
        GraphPolicyError: missing file, malformed YAML, not a mapping, or invalid values.
    """
    target = Path(path) if path is not None else default_graph_policy_path()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GraphPolicyError("the graph policy could not be read") from exc
    if not isinstance(raw, dict):
        raise GraphPolicyError("the graph policy must be a mapping")
    try:
        return GraphPolicy.model_validate(raw)
    except ValidationError as exc:
        raise GraphPolicyError("the graph policy is invalid") from exc
