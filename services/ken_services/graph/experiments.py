"""Experiment seam for graph learning (directive section 5): OFF the production path.

The deterministic metrics in :mod:`ken_services.graph.algorithms` are the baseline. A learned
model (a GNN, a node-embedding method, ...) may be tried ONLY behind :class:`GraphExperiment`
and only offline: nothing in the recommendation pipeline imports this module. A candidate
cannot replace the baseline unless :func:`require_benchmark_evidence` is satisfied -- evidence
that it beats the baseline on a held-out benchmark by a stated margin with a sufficient sample.
That check is code, so "no mandatory GNN" cannot erode into "GNN by default".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from ken_services.graph.algorithms import LinkGraph

__all__ = [
    "MIN_BENCHMARK_CASES",
    "BenchmarkEvidence",
    "EvidenceError",
    "GraphExperiment",
    "require_benchmark_evidence",
]

MIN_BENCHMARK_CASES: Final = 100
"""Fewer held-out cases than this prove nothing; the demo corpus is far below it, so no
learned graph model can be promoted on the demo corpus by construction."""


@runtime_checkable
class GraphExperiment(Protocol):
    """An experimental per-page score over the link graph. Read-only, deterministic given its
    own recorded seed, never persisted to ``page_graph_metrics``."""

    @property
    def name(self) -> str: ...

    def score(self, graph: LinkGraph) -> Mapping[uuid.UUID, float]: ...


@dataclass(frozen=True, slots=True)
class BenchmarkEvidence:
    metric: str
    baseline: float
    candidate: float
    cases: int
    margin: float
    """Required improvement over the baseline, in the metric's units."""
    higher_is_better: bool = True


class EvidenceError(Exception):
    """The experiment has not earned a place in the pipeline."""


def require_benchmark_evidence(evidence: BenchmarkEvidence | None) -> None:
    """Raise unless ``evidence`` shows the candidate beating the deterministic baseline.

    Raises:
        EvidenceError: no evidence, too few cases, or an improvement below the margin.
    """
    if evidence is None:
        raise EvidenceError("no benchmark evidence: the deterministic baseline stays")
    if evidence.cases < MIN_BENCHMARK_CASES:
        raise EvidenceError("too few benchmark cases to justify replacing the baseline")
    gain = (
        evidence.candidate - evidence.baseline
        if evidence.higher_is_better
        else evidence.baseline - evidence.candidate
    )
    if gain < max(0.0, evidence.margin):
        raise EvidenceError("the candidate does not beat the baseline by the required margin")
