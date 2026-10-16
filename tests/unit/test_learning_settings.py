"""Phase 8 learning settings (ADR-0016 15.7.10 / 16.1).

``learning_demonstration_mode`` is the mechanism that makes "Phase 8 promotes nothing" true by
construction rather than by promise, so its DEFAULT is the thing worth testing: the corpus is
~65 synthetic pages and the one stream the design calls trustworthy -- human editorial
decisions -- is the stream with no real data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_core.config import REPO_ROOT, Settings
from tests.conftest import make_settings


def _settings() -> Settings:
    """Defaults only, isolated from the developer's ``.env`` AND inherited ``KEN_*``."""
    settings: Settings = make_settings()
    return settings


def _from_env() -> Settings:
    """Reads the environment (so a ``monkeypatch.setenv`` is visible), not ``.env``."""
    return Settings(_env_file=None)


def test_demonstration_mode_defaults_to_true() -> None:
    assert _settings().learning_demonstration_mode is True


def test_demonstration_mode_is_an_explicit_operator_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEN_LEARNING_DEMONSTRATION_MODE", "false")
    assert _from_env().learning_demonstration_mode is False


def test_settings_are_frozen_so_demonstration_mode_cannot_be_flipped_at_runtime() -> None:
    settings = _settings()
    with pytest.raises(Exception, match=r"frozen"):
        settings.learning_demonstration_mode = False  # type: ignore[misc]


def test_holdout_experiments_are_off_by_default() -> None:
    """A holdout deliberately WITHHOLDS inbound links from its control target pages, which is
    a real editorial and SEO cost on the live site. The user decided it is built and gated, so
    the default has to be off -- and the lifecycle service withholds the transaction-local
    'ken.learning_experiment_activator' setting while it is, so not even a raw UPDATE can
    start one."""
    assert _settings().experiments_allow_holdout is False


def test_holdout_experiments_can_be_enabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEN_EXPERIMENTS_ALLOW_HOLDOUT", "true")
    assert _from_env().experiments_allow_holdout is True


def test_the_holdout_cap_is_not_a_setting() -> None:
    """20% is the hard maximum, in the contract and in a database CHECK. There is deliberately
    no environment variable that raises it."""
    from ken_contracts.learning import MAX_HOLDOUT_TREATMENT_FRACTION

    assert MAX_HOLDOUT_TREATMENT_FRACTION == 0.20
    assert not any("holdout" in name and "fraction" in name for name in Settings.model_fields)


def test_the_learning_policy_path_defaults_into_the_repository() -> None:
    settings = _settings()
    path = Path(settings.learning_policy_path)
    assert path.is_absolute()
    assert path == REPO_ROOT / "policies" / "learning" / "learning.v1.yaml"
    assert path.exists()


def test_a_relative_learning_policy_path_resolves_against_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEN_LEARNING_POLICY_PATH", "policies/learning/learning.v1.yaml")
    assert Path(_from_env().learning_policy_path).is_absolute()


def test_the_learning_policy_path_is_not_published_in_meta() -> None:
    """``/meta`` is an allowlist, never a dump: a filesystem path is not public settings."""
    public = _settings().public_settings()
    assert "learning_policy_path" not in public
    assert not any("learning" in key and "path" in key for key in public)
