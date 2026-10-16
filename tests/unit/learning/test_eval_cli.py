"""The ``make eval`` entry point and the manifest re-hash tool.

Covered here: suite parsing (including the deferred suite and ``all``), the usage refusals
that must not reach a database or a model, the opt-in gate on a live provider, the exit codes
``make eval`` depends on, and the fact that the committed manifests are not stale.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ken_contracts.learning import DEFERRED_SUITES, EvalSuite, GateResult
from ken_services.learning.evaluate import (
    LIVE_PROVIDER_ENV,
    _parse_suites,
    eval_main,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_all_resolves_to_every_populated_suite() -> None:
    suites = _parse_suites("all")
    assert set(suites) == set(EvalSuite) - DEFERRED_SUITES


def test_suites_parse_in_the_order_given() -> None:
    assert _parse_suites("critic, no-action") == (EvalSuite.CRITIC, EvalSuite.NO_ACTION)


def test_an_unknown_suite_name_is_refused_with_the_known_names() -> None:
    with pytest.raises(SystemExit, match="unknown suite"):
        _parse_suites("destinaton-selection")


def test_an_empty_suite_list_is_refused() -> None:
    with pytest.raises(SystemExit):
        _parse_suites(" , ")


def test_a_deferred_suite_is_refused_rather_than_silently_empty() -> None:
    """A run that quietly scored zero cases for editorial-agreement would report a suite as
    covered when no dataset exists (ADR-0016 15.3)."""
    exit_code = eval_main(["--suites", "editorial-agreement", "--no-db"])
    assert exit_code == 2


def test_naming_both_a_proposal_and_a_skill_is_refused() -> None:
    """A DB CHECK allows exactly one subject, so the CLI refuses the pair before any work."""
    exit_code = eval_main(
        [
            "--suites",
            "critic",
            "--no-db",
            "--proposal",
            "00000000-0000-4000-8000-000000000001",
            "--skill",
            "00000000-0000-4000-8000-000000000002",
        ]
    )
    assert exit_code == 2


def test_a_live_provider_needs_the_opt_in_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LIVE_PROVIDER_ENV, raising=False)
    assert eval_main(["--suites", "critic", "--no-db", "--provider", "ollama"]) == 2


def test_a_clean_run_exits_zero_and_writes_a_canonical_report(tmp_path: Path) -> None:
    """Exit 1 is reserved for a gate FAIL. An ``inconclusive`` gate -- which is what the
    sample floor produces on this corpus -- is not a CI failure: it means 'not promotable',
    which is already the standing position."""
    report = tmp_path / "report.json"
    exit_code = eval_main(
        ["--suites", "proposal-safety,reflection-grounding", "--no-db", "--report", str(report)]
    )
    assert exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["gate_result"] in {GateResult.INCONCLUSIVE.value, GateResult.PASS.value}
    assert payload["suites"] == ["proposal-safety", "reflection-grounding"]
    assert report.read_text(encoding="utf-8").endswith("\n")


def test_the_committed_manifests_are_not_stale() -> None:
    """The runner refuses a dataset whose sha256 has drifted, so a stale manifest in the
    repository would break `make eval` for everyone. This is the check, not a reminder."""
    spec = importlib.util.spec_from_file_location(
        "ken_eval_rehash", REPO_ROOT / "evals" / "datasets" / "rehash.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([]) == 0, "run: .venv/bin/python evals/datasets/rehash.py --write"


def test_the_learning_cli_dispatches_eval() -> None:
    from ken_services.learning import cli

    assert cli.main(["eval", "--suites", "editorial-agreement", "--no-db"]) == 2
