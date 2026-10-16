"""Eval dataset loading: files on disk -> validated :class:`~ken_contracts.learning.EvalCase`.

ADR-0016 section 7: **datasets are FILES, runs are ROWS.** A dataset lives at
``evals/datasets/<suite>/<dataset>.v<N>.jsonl`` beside a ``MANIFEST.yaml``
(:class:`~ken_contracts.learning.DatasetManifest`) that records the dataset version, a
per-file sha256, the case count, the provenance and the privacy flags. A fixture therefore
belongs to a reviewable commit and is reproducible from a checkout, while PostgreSQL stays the
system of record for what a run measured.

What this module enforces, every load:

* the manifest validates against the frozen contract;
* **every file's sha256 matches the manifest**, so a dataset edited without re-hashing fails
  the load instead of silently changing what a recorded ``evaluation_ref`` means;
* the manifest's ``case_count`` equals the number of cases actually parsed;
* every case validates against :class:`~ken_contracts.learning.EvalCase`, declares the suite
  it sits under, and has a ``case_id`` unique within the dataset;
* a case claiming production data without an ``approval_ref`` is refused -- by the contract
  (``EvalPrivacy``) and again here at the dataset level, because ``evals/README.md`` makes the
  approval reference the load-bearing part of that rule.

Loads are cached by (resolved directory, every file's ``st_mtime_ns``), the pattern every
other policy/dataset loader in the repository uses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from ken_contracts.learning import DatasetManifest, EvalCase, EvalSuite

__all__ = [
    "DATASETS_DIRNAME",
    "MANIFEST_FILENAME",
    "DatasetError",
    "SuiteDataset",
    "available_suites",
    "default_dataset_root",
    "load_suite_dataset",
    "load_suite_datasets",
    "manifest_shas",
    "sha256_file",
]

MANIFEST_FILENAME: Final = "MANIFEST.yaml"
DATASETS_DIRNAME: Final = "datasets"
_CACHE_SIZE: Final = 32


class DatasetError(ValueError):
    """A dataset is missing, malformed, unhashed or inconsistent with its manifest.

    The message names the suite, the file and the kind of problem -- never file contents, so
    a fixture that accidentally held something sensitive cannot leak through an error.
    """


def sha256_file(path: Path) -> str:
    """The sha256 hex digest of ``path``, read in binary (line endings included)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_dataset_root() -> Path:
    """``<repo>/evals/datasets``, derived from this file's location (no Settings needed)."""
    return Path(__file__).resolve().parents[3] / "evals" / DATASETS_DIRNAME


@dataclass(frozen=True, slots=True)
class SuiteDataset:
    """One suite's dataset: its manifest, its cases and the manifest's own sha256.

    ``manifest_sha256`` is what a run records in ``policy_evaluations.dataset_manifests``, so
    a promotion can re-check that the dataset has not moved underneath a passing evaluation.
    """

    suite: EvalSuite
    manifest: DatasetManifest
    manifest_sha256: str
    cases: tuple[EvalCase, ...]

    @property
    def dataset_version(self) -> str:
        return f"{self.manifest.dataset}.v{self.manifest.dataset_version}"


def _read_manifest(directory: Path, suite: EvalSuite) -> tuple[DatasetManifest, str]:
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        raise DatasetError(f"{suite.value}: no {MANIFEST_FILENAME} in {directory.name}")
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DatasetError(f"{suite.value}: {MANIFEST_FILENAME} is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise DatasetError(f"{suite.value}: {MANIFEST_FILENAME} must contain a mapping")
    try:
        manifest = DatasetManifest.model_validate(raw)
    except ValidationError as exc:
        raise DatasetError(
            f"{suite.value}: {MANIFEST_FILENAME} is invalid ({exc.error_count()} error(s))"
        ) from exc
    if manifest.suite is not suite:
        raise DatasetError(
            f"{suite.value}: {MANIFEST_FILENAME} declares suite {manifest.suite.value!r}"
        )
    return manifest, sha256_file(path)


def _parse_cases(path: Path, suite: EvalSuite) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{suite.value}: {path.name} line {number} is not JSON") from exc
        if not isinstance(payload, dict):
            raise DatasetError(f"{suite.value}: {path.name} line {number} is not a JSON object")
        try:
            case = EvalCase.model_validate(payload)
        except ValidationError as exc:
            raise DatasetError(
                f"{suite.value}: {path.name} line {number} is not a valid case "
                f"({exc.error_count()} error(s))"
            ) from exc
        if case.suite is not suite:
            raise DatasetError(
                f"{suite.value}: {path.name} line {number} declares suite {case.suite.value!r}"
            )
        cases.append(case)
    return cases


def _load(directory: Path, suite: EvalSuite) -> SuiteDataset:
    manifest, manifest_sha = _read_manifest(directory, suite)
    cases: list[EvalCase] = []
    for relative, expected in sorted(manifest.files.items()):
        path = directory / relative
        if ".." in Path(relative).parts or Path(relative).is_absolute():
            raise DatasetError(f"{suite.value}: manifest entry {relative!r} escapes the dataset")
        if not path.is_file():
            raise DatasetError(f"{suite.value}: manifest names a missing file {relative!r}")
        actual = sha256_file(path)
        if actual != expected:
            raise DatasetError(
                f"{suite.value}: {relative} does not match its manifest sha256 "
                "(re-hash the dataset and bump dataset_version)"
            )
        cases.extend(_parse_cases(path, suite))
    if not cases:
        raise DatasetError(f"{suite.value}: the dataset declares no cases")
    if len(cases) != manifest.case_count:
        raise DatasetError(
            f"{suite.value}: manifest case_count {manifest.case_count} != {len(cases)} parsed"
        )
    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        duplicate = sorted({value for value in ids if ids.count(value) > 1})
        raise DatasetError(f"{suite.value}: duplicate case_id {', '.join(duplicate)}")
    for case in cases:
        if case.privacy.contains_production_data and not case.privacy.approval_ref:
            raise DatasetError(f"{suite.value}: {case.case_id} claims production data unapproved")
        if case.privacy.contains_production_data and not manifest.contains_production_data:
            raise DatasetError(
                f"{suite.value}: {case.case_id} holds production data the manifest does not declare"
            )
    return SuiteDataset(
        suite=suite, manifest=manifest, manifest_sha256=manifest_sha, cases=tuple(cases)
    )


@lru_cache(maxsize=_CACHE_SIZE)
def _cached(
    resolved: str, suite: EvalSuite, _fingerprint: tuple[tuple[str, int], ...]
) -> SuiteDataset:
    return _load(Path(resolved), suite)


def _fingerprint_of(directory: Path) -> tuple[tuple[str, int], ...]:
    """(name, mtime_ns) of every file in the dataset directory, sorted."""
    return tuple(
        sorted(
            (path.name, path.stat().st_mtime_ns) for path in directory.iterdir() if path.is_file()
        )
    )


def load_suite_dataset(suite: EvalSuite, *, root: Path | str | None = None) -> SuiteDataset:
    """Load and verify one suite's dataset (cached by directory contents).

    Raises:
        DatasetError: the dataset is missing, malformed, or inconsistent with its manifest --
            including a file whose sha256 has changed.
    """
    base = Path(root) if root is not None else default_dataset_root()
    directory = (base / suite.value).resolve()
    if not directory.is_dir():
        raise DatasetError(f"{suite.value}: no dataset directory under {base}")
    return _cached(str(directory), suite, _fingerprint_of(directory))


def load_suite_datasets(
    suites: Iterable[EvalSuite], *, root: Path | str | None = None
) -> dict[EvalSuite, SuiteDataset]:
    """Every requested suite's dataset, in the requested order."""
    return {suite: load_suite_dataset(suite, root=root) for suite in suites}


def manifest_shas(datasets: Mapping[EvalSuite, SuiteDataset]) -> dict[str, str]:
    """``policy_evaluations.dataset_manifests``: ``"<suite>:<dataset>.v<N>"`` -> manifest sha256."""
    return {
        f"{suite.value}:{dataset.dataset_version}": dataset.manifest_sha256
        for suite, dataset in sorted(datasets.items(), key=lambda item: item[0].value)
    }


def available_suites(*, root: Path | str | None = None) -> tuple[EvalSuite, ...]:
    """Suites that have a dataset directory with a manifest, in enum order."""
    base = Path(root) if root is not None else default_dataset_root()
    return tuple(suite for suite in EvalSuite if (base / suite.value / MANIFEST_FILENAME).is_file())


def _clear_caches() -> None:
    """Test hook: forget every cached dataset."""
    _cached.cache_clear()
