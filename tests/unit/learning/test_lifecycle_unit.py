"""Pure tests for workstream D: the plan-set diff, the stage helpers, the planned policy files,
and the static rules that keep shadow read-only and experiments free of inference.

No database here; the database behaviour is in ``tests/integration/test_learning_*``.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Final

import pytest
import yaml

from ken_contracts.learning import ExperimentArm, ProposalStatus, TargetKind
from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy
from ken_services.learning.lifecycle import (
    missing_stages,
    shadow_requirements_met,
    stages_reached,
)
from ken_services.learning.policy import load_learning_policy
from ken_services.learning.promote import PolicyWriteRefusedError, plan_promoted_files
from ken_services.learning.protocol import experiment_arm, required_stages_met
from ken_services.learning.shadow import MAX_LISTED, ShadowDiff, compute_diff

REPO: Final = Path(__file__).resolve().parents[3]
PACKAGE: Final = REPO / "services" / "ken_services" / "learning"
MAKE: Final = shutil.which("make") or "make"
# The two `make -n` tests below assert the Makefile's WIRING, so they need GNU make on PATH.
# It is absent on a stock Windows box, where skipping is the honest outcome: the thing under
# test cannot be reached, which is not the same as the wiring being wrong.
requires_make: Final = pytest.mark.skipif(
    shutil.which("make") is None,
    reason="GNU make is not on PATH, so `make -n` cannot be run to inspect the target.",
)
POLICY: Final = load_learning_policy(REPO / "policies" / "learning" / "learning.v1.yaml")


Selected = dict[tuple[uuid.UUID, uuid.UUID], tuple[int, float]]


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _diff(production: Selected, candidate: Selected) -> ShadowDiff:
    return compute_diff(
        production=production,
        candidate=candidate,
        sources_compared=3,
        fingerprint="a" * 64,
        candidate_version="2-abc",
        learning_policy_version=1,
        recommendation_run_id=_id(99),
    )


# --------------------------------------------------------------------------- #
# The plan-set diff
# --------------------------------------------------------------------------- #
def test_the_diff_separates_added_dropped_reordered_and_unchanged() -> None:
    s = _id(1)
    production = {(s, _id(10)): (0, 0.9), (s, _id(11)): (1, 0.8), (s, _id(12)): (2, 0.7)}
    candidate = {(s, _id(11)): (0, 0.85), (s, _id(10)): (1, 0.9), (s, _id(13)): (2, 0.6)}

    diff = _diff(production, candidate)

    assert (diff.added_count, diff.dropped_count, diff.reordered_count) == (1, 1, 2)
    assert diff.unchanged == 0
    assert diff.compared_pairs == 4
    assert [d.target_page_id for d in diff.added] == [_id(13)]
    assert [d.target_page_id for d in diff.dropped] == [_id(12)]
    assert {d.target_page_id for d in diff.reordered} == {_id(10), _id(11)}
    moved = {d.target_page_id: d for d in diff.reordered}
    assert (moved[_id(10)].production_rank, moved[_id(10)].candidate_rank) == (0, 1)
    assert moved[_id(11)].score_delta == pytest.approx(0.05)
    assert {d.target_page_id for d in diff.score_deltas} == {_id(11)}
    assert diff.max_abs_score_delta == pytest.approx(0.05)
    assert diff.mean_abs_score_delta == pytest.approx(0.025)


def test_an_identical_selection_has_an_empty_diff() -> None:
    s = _id(1)
    same = {(s, _id(10)): (0, 0.9), (s, _id(11)): (1, 0.8)}
    diff = _diff(same, dict(same))
    assert (diff.added_count, diff.dropped_count, diff.reordered_count) == (0, 0, 0)
    assert diff.unchanged == diff.compared_pairs == 2
    assert diff.score_deltas == ()


def test_an_empty_world_is_a_valid_empty_diff() -> None:
    diff = _diff({}, {})
    assert diff.compared_pairs == diff.unchanged == 0
    assert diff.mean_abs_score_delta is None


def test_a_large_diff_is_capped_but_its_counts_stay_exact() -> None:
    s = _id(1)
    production = {(s, _id(1000 + n)): (n, 0.5) for n in range(MAX_LISTED + 50)}
    diff = _diff(production, {})
    assert diff.dropped_count == MAX_LISTED + 50
    assert len(diff.dropped) == MAX_LISTED
    assert diff.truncated is True


def test_the_diff_states_only_observed_differences() -> None:
    names = set(_diff({}, {}).model_dump())
    assert not names & {"p_value", "significance", "lift", "causal_effect", "revenue", "better"}


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
class _Row:
    def __init__(self, to_status: str) -> None:
        self.to_status = to_status


def test_stages_reached_starts_at_proposed_and_dedupes() -> None:
    rows = [_Row("offline_test"), _Row("shadow"), _Row("canary"), _Row("shadow")]
    assert stages_reached(rows) == (
        ProposalStatus.PROPOSED,
        ProposalStatus.OFFLINE_TEST,
        ProposalStatus.SHADOW,
        ProposalStatus.CANARY,
    )


def test_required_stages_follow_the_kind_in_the_policy() -> None:
    reached = (ProposalStatus.PROPOSED, ProposalStatus.OFFLINE_TEST)
    assert required_stages_met(policy=POLICY, key="agents", reached=reached) is True
    assert required_stages_met(policy=POLICY, key="skill", reached=reached) is True
    assert required_stages_met(policy=POLICY, key="ranking", reached=reached) is False
    assert missing_stages(policy=POLICY, key="ranking", reached=reached) == (ProposalStatus.SHADOW,)


def test_the_shadow_floors_count_runs_and_compared_pairs_of_this_fingerprint() -> None:
    fp = "a" * 64
    good = [{"fingerprint": fp, "compared_pairs": 10}] * 3
    assert shadow_requirements_met(policy=POLICY, fingerprint=fp, shadow_metrics=good) is True
    few = [{"fingerprint": fp, "compared_pairs": 10}] * 2
    assert shadow_requirements_met(policy=POLICY, fingerprint=fp, shadow_metrics=few) is False
    thin = [{"fingerprint": fp, "compared_pairs": 2}] * 3
    assert shadow_requirements_met(policy=POLICY, fingerprint=fp, shadow_metrics=thin) is False
    stale = [{"fingerprint": "b" * 64, "compared_pairs": 99}] * 5
    assert shadow_requirements_met(policy=POLICY, fingerprint=fp, shadow_metrics=stale) is False


# --------------------------------------------------------------------------- #
# The planned policy files
# --------------------------------------------------------------------------- #
def _ranking_doc() -> dict:  # type: ignore[type-arg]
    raw = yaml.safe_load((REPO / "policies/ranking/ranking_weights.v1.yaml").read_text())
    doc = RankingPolicy.model_validate(raw).model_dump(mode="json")
    doc["version"] = 2
    doc["weights"]["semantic"], doc["weights"]["taxonomy"] = 0.35, 0.15
    return doc


def _active() -> RecommendationPolicy:
    raw = yaml.safe_load((REPO / "policies/recommendation/recommendation.v1.yaml").read_text())
    return RecommendationPolicy.model_validate(raw)


def test_a_ranking_promotion_plans_two_files_weights_first_then_the_pointer() -> None:
    plan = plan_promoted_files(
        target_kind=TargetKind.RANKING.value,
        candidate_document=_ranking_doc(),
        proposal_id=_id(5),
        active_recommendation=_active(),
    )
    assert plan.paths == (
        "policies/ranking/ranking_weights.v2.yaml",
        "policies/recommendation/recommendation.v2.yaml",
    )
    weights, recommendation = (yaml.safe_load(text) for _, text in plan.files)
    assert weights["version"] == 2
    assert recommendation["version"] == _active().version + 1
    assert recommendation["ranking_policy"] == plan.paths[0]
    RankingPolicy.model_validate(weights)
    RecommendationPolicy.model_validate(recommendation)


def test_a_recommendation_promotion_plans_one_file() -> None:
    document = _active().model_dump(mode="json")
    document["version"] = 2
    plan = plan_promoted_files(
        target_kind=TargetKind.RECOMMENDATION.value,
        candidate_document=document,
        proposal_id=_id(5),
    )
    assert plan.paths == ("policies/recommendation/recommendation.v2.yaml",)


def test_a_ranking_plan_without_the_active_document_is_refused() -> None:
    with pytest.raises(PolicyWriteRefusedError):
        plan_promoted_files(
            target_kind=TargetKind.RANKING.value,
            candidate_document=_ranking_doc(),
            proposal_id=_id(5),
        )


def test_a_candidate_that_no_longer_validates_is_never_planned() -> None:
    broken = _ranking_doc()
    broken["weights"]["semantic"] = 5.0
    with pytest.raises(ValueError):
        plan_promoted_files(
            target_kind=TargetKind.RANKING.value,
            candidate_document=broken,
            proposal_id=_id(5),
            active_recommendation=_active(),
        )


# --------------------------------------------------------------------------- #
# Deterministic arms
# --------------------------------------------------------------------------- #
def test_the_arm_is_a_pure_function_of_key_and_page() -> None:
    page = _id(42)
    first = experiment_arm(key="ab-one", page_id=page, treatment_fraction=0.5)
    assert all(
        experiment_arm(key="ab-one", page_id=page, treatment_fraction=0.5) is first
        for _ in range(5)
    )
    assert {
        experiment_arm(key=f"k-{n}", page_id=page, treatment_fraction=0.5) for n in range(40)
    } == set(ExperimentArm)


# --------------------------------------------------------------------------- #
# Static rules
# --------------------------------------------------------------------------- #
def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("name", ["experiments", "shadow", "lifecycle", "promote", "reader"])
def test_no_statistics_dependency_exists_in_the_lifecycle_modules(name: str) -> None:
    """ADR-0016 section 6/11: no significance testing, so no library that would carry it."""
    forbidden = {"numpy", "scipy", "statistics", "pandas", "statsmodels", "sklearn"}
    assert not _imports(PACKAGE / f"{name}.py") & forbidden


def test_shadow_calls_nothing_that_writes_a_plan_a_candidate_a_transition_or_an_event() -> None:
    tree = ast.parse((PACKAGE / "shadow.py").read_text(encoding="utf-8"))
    forbidden = {
        "insert_plan",
        "insert_candidates",
        "insert_transition",
        "record_initial",
        "transition",
        "append_event",
        "replace_opportunities",
        "set_plan_status",
        "lock_plan",
    }
    called = {
        (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & forbidden, called & forbidden
    on_session = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session"
    }
    assert not on_session & {"add", "add_all", "delete", "merge", "flush"}, on_session
    # The single write it makes is the evaluation row.
    assert "insert_shadow_evaluation" in called


def test_the_only_experiment_status_writers_are_the_lifecycle_service() -> None:
    for path in PACKAGE.glob("*.py"):
        if path.name in {"experiments.py", "lifecycle_cli.py"}:
            continue
        assert "set_experiment_status" not in path.read_text(encoding="utf-8"), path.name


@requires_make
def test_make_shadow_resolves_to_the_shadow_subcommand() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [MAKE, "-n", "shadow", "CANDIDATE=00000000-0000-0000-0000-000000000001"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ken_services.learning.cli shadow --candidate" in result.stdout


@requires_make
@pytest.mark.parametrize("target", ["experiment-create", "experiment-assign", "policy-promote"])
def test_the_lifecycle_make_targets_exist(target: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [MAKE, "-n", target, "KEY=k", "AS_USER=a", "PROPOSAL=p"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ken_services.learning.cli" in result.stdout
