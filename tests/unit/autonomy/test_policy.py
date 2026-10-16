"""The autonomy policy is versioned DATA with hard floors it cannot go below.

The directive makes autonomy *policy-governed*: thresholds live in a reviewed, versioned file,
not in constants only a code change can alter. And a human-editable safety document must not
be able to undo the safety work, so a value LESS conservative than the contract's floor is
refused at load. These tests prove both, and that the shipped file meets its own floors.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import ValidationError

from ken_contracts.autonomy import (
    AUTONOMOUS_CONFIDENCE_FLOOR_MIN,
    COOLDOWN_FLOOR_S,
    FAILED_ATTEMPTS_FOR_HIGH_CEILING,
    GUARDED_CONFIDENCE_FLOOR_MIN,
    HUB_INBOUND_LINKS_CEILING,
    POSITIVE_OUTCOME_RATE_FLOOR_MIN,
    PUBLISH_ATTEMPTS_CEILING,
    REDIRECT_HOPS_CEILING,
    REQUIRED_COMMERCIAL_PAGE_TYPES,
    SENSITIVE_ANCHOR_CHARS_CEILING,
    AutonomyPolicy,
)
from ken_core.config import REPO_ROOT
from ken_services.autonomy.policy import (
    DEFAULT_AUTONOMY_POLICY_PATH,
    PolicyError,
    effective_retry,
    load_autonomy_policy,
)
from tests.unit.autonomy._support import settings

POLICY_FILE: Final = REPO_ROOT / "policies" / "autonomy" / "autonomy.v1.yaml"
PACKAGE: Final = REPO_ROOT / "services" / "ken_services" / "autonomy"


def _raw() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))
    return loaded


def _with(section: str, key: str, value: object) -> dict[str, Any]:
    raw = _raw()
    raw[section][key] = value
    return raw


# --------------------------------------------------------------------------- #
# The shipped file
# --------------------------------------------------------------------------- #
def test_the_released_policy_loads_and_meets_every_floor() -> None:
    policy = load_autonomy_policy()
    assert DEFAULT_AUTONOMY_POLICY_PATH == POLICY_FILE
    assert policy.policy == "autonomy" and policy.version == 1
    assert policy.confidence.guarded_min_confidence >= GUARDED_CONFIDENCE_FLOOR_MIN
    assert policy.confidence.autonomous_min_confidence >= AUTONOMOUS_CONFIDENCE_FLOOR_MIN
    assert policy.confidence.min_positive_outcome_rate >= POSITIVE_OUTCOME_RATE_FLOOR_MIN
    assert policy.retry.max_attempts <= PUBLISH_ATTEMPTS_CEILING
    assert policy.retry.cooldown_s >= COOLDOWN_FLOOR_S
    assert policy.risk.hub_inbound_links <= HUB_INBOUND_LINKS_CEILING
    assert policy.risk.sensitive_anchor_chars <= SENSITIVE_ANCHOR_CHARS_CEILING
    assert policy.risk.failed_attempts_for_high <= FAILED_ATTEMPTS_FOR_HIGH_CEILING
    assert policy.blocks.max_redirect_hops <= REDIRECT_HOPS_CEILING
    assert set(policy.risk.commercial_page_types) >= REQUIRED_COMMERCIAL_PAGE_TYPES


def test_the_file_name_carries_the_version_it_declares() -> None:
    """A released version is never edited in place: v2 is a new file, so the name and the
    ``version:`` field must agree or a reader could not find the document that governed a row."""
    policy = load_autonomy_policy()
    assert POLICY_FILE.name == f"autonomy.v{policy.version}.yaml"


# --------------------------------------------------------------------------- #
# A file may only be MORE conservative than the floor, never less
# --------------------------------------------------------------------------- #
LESS_CONSERVATIVE: list[tuple[str, str, object]] = [
    ("confidence", "guarded_min_confidence", GUARDED_CONFIDENCE_FLOOR_MIN - 0.01),
    ("confidence", "autonomous_min_confidence", AUTONOMOUS_CONFIDENCE_FLOOR_MIN - 0.01),
    ("confidence", "min_positive_outcome_rate", POSITIVE_OUTCOME_RATE_FLOOR_MIN - 0.01),
    ("retry", "max_attempts", PUBLISH_ATTEMPTS_CEILING + 1),
    ("retry", "max_attempts", 0),
    ("retry", "cooldown_s", COOLDOWN_FLOOR_S - 1),
    ("retry", "cooldown_s", 0),
    ("risk", "hub_inbound_links", HUB_INBOUND_LINKS_CEILING + 1),
    ("risk", "sensitive_anchor_chars", SENSITIVE_ANCHOR_CHARS_CEILING + 1),
    ("risk", "failed_attempts_for_high", FAILED_ATTEMPTS_FOR_HIGH_CEILING + 1),
    ("blocks", "max_redirect_hops", REDIRECT_HOPS_CEILING + 1),
]


@pytest.mark.parametrize(
    ("section", "key", "value"),
    LESS_CONSERVATIVE,
    ids=[f"{s}.{k}={v}" for s, k, v in LESS_CONSERVATIVE],
)
def test_a_value_below_a_floor_is_refused(section: str, key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AutonomyPolicy.model_validate(_with(section, key, value))


def test_a_commercial_page_type_cannot_be_removed_but_one_can_be_added() -> None:
    raw = _raw()
    raw["risk"]["commercial_page_types"] = [
        t for t in raw["risk"]["commercial_page_types"] if t != "pricing"
    ]
    with pytest.raises(ValidationError, match="pricing"):
        AutonomyPolicy.model_validate(raw)
    raw = _raw()
    raw["risk"]["commercial_page_types"] = [*raw["risk"]["commercial_page_types"], "booking"]
    assert "booking" in AutonomyPolicy.model_validate(raw).risk.commercial_page_types


def test_a_stricter_value_is_accepted() -> None:
    raw = _raw()
    raw["confidence"]["guarded_min_confidence"] = 0.95
    raw["retry"]["max_attempts"] = 1
    raw["retry"]["cooldown_s"] = 86400
    policy = AutonomyPolicy.model_validate(raw)
    assert policy.confidence.guarded_min_confidence == 0.95
    assert policy.retry.max_attempts == 1


def test_guarded_cannot_be_laxer_than_autonomous() -> None:
    raw = _raw()
    raw["confidence"]["guarded_min_confidence"] = 0.80
    raw["confidence"]["autonomous_min_confidence"] = 0.90
    with pytest.raises(ValidationError, match="stricter mode"):
        AutonomyPolicy.model_validate(raw)


def test_an_unknown_key_is_refused_rather_than_silently_ignored() -> None:
    raw = _raw()
    raw["confidence"]["guarded_min_confidense"] = 0.1  # a typo must not be a no-op
    with pytest.raises(ValidationError):
        AutonomyPolicy.model_validate(raw)


def test_a_loader_refuses_a_file_below_the_floor_and_names_no_value(tmp_path: Path) -> None:
    bad = tmp_path / "autonomy.v9.yaml"
    raw = _raw()
    raw["confidence"]["guarded_min_confidence"] = 0.31
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(PolicyError) as caught:
        load_autonomy_policy(bad)
    assert "0.31" not in str(caught.value)
    assert "1 error" in str(caught.value)


def test_a_loader_refuses_malformed_yaml_and_a_non_mapping(tmp_path: Path) -> None:
    broken = tmp_path / "a.yaml"
    broken.write_text("confidence: [unclosed", encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_autonomy_policy(broken)
    scalar = tmp_path / "b.yaml"
    scalar.write_text("42\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="mapping"):
        load_autonomy_policy(scalar)


# --------------------------------------------------------------------------- #
# The environment may TIGHTEN the policy and never loosen it
# --------------------------------------------------------------------------- #
def test_the_environment_can_only_tighten_the_retry_budget() -> None:
    policy = load_autonomy_policy()
    base = effective_retry(policy, settings())
    assert base.max_attempts == policy.retry.max_attempts
    assert base.cooldown_s == policy.retry.cooldown_s
    tighter = effective_retry(
        policy, settings(autonomy_publish_max_attempts=1, autonomy_publish_cooldown_s=86400.0)
    )
    assert tighter.max_attempts == 1 and tighter.cooldown_s == 86400.0
    # Settings allow a zero cool-down and ten attempts; the policy floor wins.
    looser = effective_retry(
        policy, settings(autonomy_publish_max_attempts=10, autonomy_publish_cooldown_s=0.0)
    )
    assert looser.max_attempts == policy.retry.max_attempts
    assert looser.cooldown_s == policy.retry.cooldown_s >= COOLDOWN_FLOOR_S


# --------------------------------------------------------------------------- #
# No safety threshold is a number only a code change can alter
# --------------------------------------------------------------------------- #
ALLOWED_LITERALS: Final = frozenset({0, 1, -1, 200, 203, 204})
"""0, 1 and -1 are arithmetic. 200, 203 and 204 are the HTTP statuses of a page that resolves
(a FACT about the web, in ``blocks.py``), not a threshold anyone should tune."""


@pytest.mark.parametrize(
    "module", ["engine", "risk", "calibration", "budget", "facts", "blocks", "reasons"]
)
def test_no_decision_module_holds_a_numeric_threshold(module: str) -> None:
    """Every confidence floor, retry value, risk threshold and redirect limit comes from the
    versioned policy. A bare number in these modules is how a safety threshold ends up chosen
    to fit a demo, so this fails on the first one that appears."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    found = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
            and id(node) not in documentation
            and node.value not in ALLOWED_LITERALS
        }
    )
    # reasons.py's one number is the 500-character bound of link_plan_transitions.note.
    allowed = [500] if module == "reasons" else []
    assert found == allowed, f"{module}.py holds numeric literal(s) {found}"
