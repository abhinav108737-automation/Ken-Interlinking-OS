"""The versioned graph policy and the experiment seam (no GNN without evidence)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_services.graph.experiments import (
    MIN_BENCHMARK_CASES,
    BenchmarkEvidence,
    EvidenceError,
    require_benchmark_evidence,
)
from ken_services.graph.policy import (
    GRAPH_ALGORITHM_VERSION,
    GraphPolicyError,
    load_graph_policy,
)


def test_the_shipped_policy_loads_and_versions_the_recipe() -> None:
    policy = load_graph_policy()
    assert policy.policy == "graph" and policy.version >= 1
    assert policy.metrics_version == f"graph-alg{GRAPH_ALGORITHM_VERSION}-pol{policy.version}"
    assert len(policy.metrics_version) <= 64  # the DB CHECK


def test_a_malformed_policy_is_a_typed_error(tmp_path: Path) -> None:
    bad = tmp_path / "graph.yaml"
    bad.write_text("policy: graph\nversion: 0\nstatus: x\nsource: y\n")
    with pytest.raises(GraphPolicyError):
        load_graph_policy(bad)
    with pytest.raises(GraphPolicyError):
        load_graph_policy(tmp_path / "missing.yaml")
    bad.write_text("- not a mapping\n")
    with pytest.raises(GraphPolicyError):
        load_graph_policy(bad)


def test_no_evidence_means_the_baseline_stays() -> None:
    with pytest.raises(EvidenceError):
        require_benchmark_evidence(None)


def test_too_few_cases_or_too_small_a_gain_is_refused() -> None:
    few = BenchmarkEvidence("ndcg", 0.5, 0.9, MIN_BENCHMARK_CASES - 1, 0.01)
    with pytest.raises(EvidenceError):
        require_benchmark_evidence(few)
    flat = BenchmarkEvidence("ndcg", 0.50, 0.505, MIN_BENCHMARK_CASES, 0.02)
    with pytest.raises(EvidenceError):
        require_benchmark_evidence(flat)


def test_a_real_gain_on_enough_cases_passes_in_either_direction() -> None:
    require_benchmark_evidence(BenchmarkEvidence("ndcg", 0.5, 0.6, MIN_BENCHMARK_CASES, 0.05))
    require_benchmark_evidence(
        BenchmarkEvidence("error", 0.4, 0.3, MIN_BENCHMARK_CASES, 0.05, higher_is_better=False)
    )
