"""The eval datasets themselves: loadable, hashed, honest about privacy, and real gates.

These tests are about the DATASETS, not the runner. They are what stops the commonest ways an
eval suite stops being a gate:

* a dataset edited without re-hashing (the loader must refuse it);
* a hard-fail suite with too few cases to mean anything;
* a suite with no CONTROL case, so that "refuse everything" would score 100%;
* a case quietly carrying production data without an approval reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_contracts.learning import (
    DEFERRED_SUITES,
    HARD_FAIL_SUITES,
    CaseOrigin,
    EvalSuite,
)
from ken_services.learning import datasets as ds

MIN_HARD_FAIL_CASES = 10
"""ADR-0016 §15.3 makes these gates; a hard-fail suite with fewer cases is not one."""

POPULATED = tuple(suite for suite in EvalSuite if suite not in DEFERRED_SUITES)

CONTROL_DECISIONS: dict[EvalSuite, frozenset[str]] = {
    EvalSuite.DESTINATION_SELECTION: frozenset({"select:india-ev-report"}),
    # ADR-0027 §3: the anchor carries the word "market". The pre-ADR-0027 control decision
    # was the bare "anchor:electric vehicle@b1", which the engine must now never produce.
    EvalSuite.ANCHOR_SELECTION: frozenset(
        {"anchor:India electric vehicle market@b1", "anchor:electric vehicle market@b1"}
    ),
    EvalSuite.RELATIONSHIP_DETECTION: frozenset({"relationship:same_market"}),
    EvalSuite.CRITIC: frozenset({"verdict:accept"}),
    EvalSuite.NO_ACTION: frozenset({"triage:optimize", "plans:2"}),
    EvalSuite.PROTECTED_CONTENT: frozenset({"allowed", "valid"}),
    EvalSuite.STALE_PAGE_VERSION: frozenset({"freshness:fresh", "mutation:applied", "cms:applied"}),
    EvalSuite.POLICY_REGRESSION: frozenset({"select:india-ev-report"}),
    EvalSuite.REFLECTION_GROUNDING: frozenset({"rederives"}),
    EvalSuite.PROPOSAL_SAFETY: frozenset({"accepted", "payload_accepted", "proposer_accepted"}),
    EvalSuite.ROLLBACK_CORRECTNESS: frozenset(
        {"verify_passed:kept", "rolled_back:original", "allowed"}
    ),
    EvalSuite.AUTONOMY_CLASSIFICATION: frozenset(
        {
            "[] shadow:block(would=auto_publish)/none,guarded:auto_publish/autonomy_decided,"
            "autonomous:auto_publish/autonomy_decided",
            "tier:low",
        }
    ),
}
"""Per suite, the decisions that mean 'the system DID the thing'. A suite whose cases all
expect a refusal is passed by code that refuses everything, which is the only way a
zero-tolerance gate can be gamed."""


@pytest.fixture(scope="module")
def loaded() -> dict[EvalSuite, ds.SuiteDataset]:
    return ds.load_suite_datasets(POPULATED)


def test_every_non_deferred_suite_has_a_dataset() -> None:
    assert set(ds.available_suites()) == set(POPULATED)


def test_the_deferred_suite_has_no_dataset() -> None:
    """editorial-agreement needs held-out human decisions that do not exist (ADR-0016 15.3).
    A stub dataset would be worse than none: it would make the suite look covered."""
    for suite in DEFERRED_SUITES:
        with pytest.raises(ds.DatasetError):
            ds.load_suite_dataset(suite)


@pytest.mark.parametrize("suite", POPULATED, ids=lambda suite: suite.value)
def test_dataset_loads_and_matches_its_manifest(
    suite: EvalSuite, loaded: dict[EvalSuite, ds.SuiteDataset]
) -> None:
    dataset = loaded[suite]
    assert dataset.manifest.case_count == len(dataset.cases)
    assert dataset.manifest.files
    assert len({case.case_id for case in dataset.cases}) == len(dataset.cases)


@pytest.mark.parametrize("suite", POPULATED, ids=lambda suite: suite.value)
def test_no_case_claims_production_data(
    suite: EvalSuite, loaded: dict[EvalSuite, ds.SuiteDataset]
) -> None:
    """evals/README.md: no real production data without a recorded approval_ref. Phase 8 has
    no such approval, so every case must be synthetic and every manifest must say so."""
    dataset = loaded[suite]
    assert dataset.manifest.contains_production_data is False
    assert dataset.manifest.approval_ref is None
    for case in dataset.cases:
        assert case.privacy.contains_production_data is False, case.case_id
        assert case.privacy.approval_ref is None, case.case_id
        assert case.provenance.origin is CaseOrigin.SYNTHETIC, case.case_id


@pytest.mark.parametrize("suite", sorted(HARD_FAIL_SUITES, key=str), ids=str)
def test_hard_fail_suites_have_enough_cases_to_be_a_gate(
    suite: EvalSuite, loaded: dict[EvalSuite, ds.SuiteDataset]
) -> None:
    assert len(loaded[suite].cases) >= MIN_HARD_FAIL_CASES


@pytest.mark.parametrize("suite", POPULATED, ids=lambda suite: suite.value)
def test_every_suite_has_a_control_case(
    suite: EvalSuite, loaded: dict[EvalSuite, ds.SuiteDataset]
) -> None:
    expected = {case.expected.decision for case in loaded[suite].cases}
    assert expected & CONTROL_DECISIONS[suite], (
        f"{suite.value} has no case asserting a positive outcome; code that refused "
        "everything would score 100% on it"
    )


@pytest.mark.parametrize("suite", POPULATED, ids=lambda suite: suite.value)
def test_cases_carry_a_rationale_and_a_description(
    suite: EvalSuite, loaded: dict[EvalSuite, ds.SuiteDataset]
) -> None:
    """A case nobody can read is a case nobody can judge when it fails."""
    for case in loaded[suite].cases:
        assert len(case.rationale) >= 40, case.case_id
        assert len(case.description) >= 20, case.case_id


def test_a_tampered_case_file_is_refused(tmp_path: Path) -> None:
    """The load-time sha256 check is what makes a recorded evaluation_ref mean something: a
    dataset edited after a passing run must not silently redefine what that run measured."""
    source = ds.default_dataset_root() / EvalSuite.PROPOSAL_SAFETY.value
    target = tmp_path / EvalSuite.PROPOSAL_SAFETY.value
    target.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file():
            (target / path.name).write_bytes(path.read_bytes())
    # Sanity: the untouched copy loads.
    ds._clear_caches()
    assert ds.load_suite_dataset(EvalSuite.PROPOSAL_SAFETY, root=tmp_path).cases

    case_file = next(path for path in target.iterdir() if path.suffix == ".jsonl")
    case_file.write_text(
        case_file.read_text(encoding="utf-8").replace("refused:frozen_path", "accepted", 1),
        encoding="utf-8",
    )
    ds._clear_caches()
    with pytest.raises(ds.DatasetError, match="manifest sha256"):
        ds.load_suite_dataset(EvalSuite.PROPOSAL_SAFETY, root=tmp_path)
    ds._clear_caches()


def test_manifest_shas_are_keyed_by_suite_and_dataset_version(
    loaded: dict[EvalSuite, ds.SuiteDataset],
) -> None:
    shas = ds.manifest_shas(loaded)
    assert shas["proposal-safety:core.v1"] == loaded[EvalSuite.PROPOSAL_SAFETY].manifest_sha256
    assert len(shas) == len(loaded)


def test_every_block_code_has_a_single_block_case(
    loaded: dict[EvalSuite, ds.SuiteDataset],
) -> None:
    """The directive's closed set of hard blocks: for each of the 15 BlockCode values there is
    a case whose ONLY fired code is that one and which expects BLOCK in every mode. If a code
    is added to the enum, this fails until a case exists for it."""
    from ken_contracts.autonomy import BlockCode

    covered: set[str] = set()
    for case in loaded[EvalSuite.AUTONOMY_CLASSIFICATION].cases:
        decision = case.expected.decision
        if (
            case.case_id.endswith("-blocks-in-every-mode")
            and decision.startswith("[")
            and "," not in decision.split("]", 1)[0]
        ):
            covered.add(decision[1:].split("]", 1)[0])
            assert (
                decision.split("] ", 1)[1]
                == "shadow:block(would=block),guarded:block,autonomous:block"
            )
    assert covered == {code.value for code in BlockCode}


def test_the_autonomy_cases_cover_all_three_modes(
    loaded: dict[EvalSuite, ds.SuiteDataset],
) -> None:
    text = " ".join(
        case.expected.decision for case in loaded[EvalSuite.AUTONOMY_CLASSIFICATION].cases
    )
    for mode in ("shadow:", "guarded:", "autonomous:"):
        assert mode in text, mode


def test_the_new_hard_fail_suites_are_unwaivable_and_gated_at_one() -> None:
    from ken_contracts.learning import HARD_FAIL_SUITES

    for suite in (EvalSuite.ROLLBACK_CORRECTNESS, EvalSuite.AUTONOMY_CLASSIFICATION):
        assert suite in HARD_FAIL_SUITES
