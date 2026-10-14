"""Agent policy loading (ADR-0012 section 10): validation and the cross-check."""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_contracts.agents import AgentPolicy
from ken_contracts.recommendations import RecommendationPolicy
from ken_core.config import REPO_ROOT
from ken_services.agents.policy import (
    AgentPolicyMismatchError,
    PolicyError,
    load_agent_policy,
    load_configured_agent_policy,
)
from ken_services.recommendations.policy import load_recommendation_policy
from tests.conftest import make_settings

AGENTS_V1 = REPO_ROOT / "policies" / "agents" / "agents.v1.yaml"
RECOMMENDATION_V1 = REPO_ROOT / "policies" / "recommendation" / "recommendation.v1.yaml"
VALID = """
version: 7
planner:
  use_model: false
critic:
  use_model: false
  accept_min_confidence: 0.6
max_plans_per_run: {n}
"""


def _capped(cap: int) -> RecommendationPolicy:
    policy = load_recommendation_policy(RECOMMENDATION_V1)
    selection = policy.selection.model_copy(update={"max_plans_per_source": cap})
    return policy.model_copy(update={"selection": selection})


def test_the_v1_policy_loads_and_passes_the_cross_check() -> None:
    policy = load_agent_policy(AGENTS_V1)
    assert isinstance(policy, AgentPolicy)
    assert policy.version == 1
    cap = load_recommendation_policy(RECOMMENDATION_V1).selection.max_plans_per_source
    assert policy.max_plans_per_run <= cap
    assert load_agent_policy(AGENTS_V1) is policy  # cached by path and mtime


def test_more_plans_per_run_than_the_engine_keeps_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(VALID.format(n=4), encoding="utf-8")
    assert load_agent_policy(path, recommendation_policy=_capped(4)).max_plans_per_run == 4
    with pytest.raises(AgentPolicyMismatchError) as info:
        load_agent_policy(path, recommendation_policy=_capped(3))
    assert (info.value.max_plans_per_run, info.value.max_plans_per_source) == (4, 3)
    assert isinstance(info.value, PolicyError)
    # The file path is never part of the message.
    assert str(tmp_path) not in str(info.value)


def test_the_cross_check_reads_the_named_recommendation_policy(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(VALID.format(n=5), encoding="utf-8")
    with pytest.raises(AgentPolicyMismatchError):  # v1 keeps 3 plans per source
        load_agent_policy(path, recommendation_policy_path=RECOMMENDATION_V1)
    with pytest.raises(AgentPolicyMismatchError):  # the default is v1 too
        load_agent_policy(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("version: [unclosed", "not valid YAML"),
        ("- a list", "must contain a mapping"),
        ("version: 1\nplanner:\n  use_model: false\n", "invalid"),
        (VALID.format(n=0), "invalid"),
        (VALID.format(n=3) + "anchor_text: invented\n", "invalid"),  # unknown keys refused
    ],
)
def test_bad_files_raise_policy_error_without_contents(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PolicyError) as info:
        load_agent_policy(path, recommendation_policy=_capped(10))
    assert message in str(info.value)
    assert "invented" not in str(info.value)


def test_a_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_agent_policy(tmp_path / "missing.yaml")


def test_the_setting_resolves_against_the_repo_root_and_is_used() -> None:
    settings = make_settings()
    assert Path(settings.agents_policy_path) == AGENTS_V1
    relative = make_settings(agents_policy_path="policies/agents/agents.v1.yaml")
    assert Path(relative.agents_policy_path) == AGENTS_V1
    assert load_configured_agent_policy(settings).version == 1


def test_the_setting_reads_its_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ken_core.config import Settings

    path = tmp_path / "agents.v9.yaml"
    path.write_text(VALID.format(n=2), encoding="utf-8")
    monkeypatch.setenv("KEN_AGENTS_POLICY_PATH", str(path))
    settings = Settings(_env_file=None)
    assert settings.agents_policy_path == str(path)
    assert load_configured_agent_policy(settings).version == 7
