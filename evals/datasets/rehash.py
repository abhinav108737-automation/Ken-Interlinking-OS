#!/usr/bin/env python
"""Re-hash the eval dataset manifests after editing a case file.

    .venv/bin/python evals/datasets/rehash.py           # check only; exit 1 if stale
    .venv/bin/python evals/datasets/rehash.py --write    # rewrite the sha256 and case_count

The runner verifies every file's sha256 against its ``MANIFEST.yaml`` and REFUSES a dataset
that has drifted, so that a recorded ``evaluation_ref`` always names a dataset whose bytes are
known. Editing a case therefore means re-hashing, and re-hashing by hand is how a mistake gets
in. This tool does the mechanical part and nothing else:

* it rewrites only ``files: <name>: <sha256>`` and ``case_count``;
* it NEVER touches ``dataset_version``, the provenance or the privacy flags -- those are
  editorial statements about the dataset, and a change of meaning deserves a version bump the
  author makes deliberately;
* with no ``--write`` it only reports, so it can run in CI as a staleness check.

If you changed what a case MEANS rather than fixing a typo, bump ``dataset_version`` yourself:
the version is what a past evaluation row points at.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

DATASETS = Path(__file__).resolve().parent
MANIFEST = "MANIFEST.yaml"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def case_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def rehash(directory: Path, *, write: bool) -> list[str]:
    """Problems found in one dataset directory (empty = already correct)."""
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        return [f"{directory.name}: no {MANIFEST}"]
    text = manifest_path.read_text(encoding="utf-8")
    problems: list[str] = []
    total = 0
    for name, recorded in re.findall(r"^  ([^\s:]+): ([0-9a-f]{64})$", text, flags=re.MULTILINE):
        case_file = directory / name
        if not case_file.is_file():
            problems.append(f"{directory.name}: manifest names a missing file {name}")
            continue
        actual = sha256_file(case_file)
        total += case_count(case_file)
        if actual != recorded:
            problems.append(f"{directory.name}: {name} sha256 is stale")
            text = text.replace(f"  {name}: {recorded}", f"  {name}: {actual}")
    recorded_count = re.search(r"^case_count: (\d+)$", text, flags=re.MULTILINE)
    if recorded_count is None:
        problems.append(f"{directory.name}: manifest has no case_count")
    elif int(recorded_count.group(1)) != total:
        problems.append(f"{directory.name}: case_count {recorded_count.group(1)} != {total} lines")
        text = re.sub(
            r"^case_count: \d+$", f"case_count: {total}", text, count=1, flags=re.MULTILINE
        )
    if problems and write:
        manifest_path.write_text(text, encoding="utf-8")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Rewrite the manifests in place.")
    args = parser.parse_args(argv)
    problems: list[str] = []
    for directory in sorted(
        path
        for path in DATASETS.iterdir()
        # A suite directory is named after its suite; `_`/`.` prefixes are tooling
        # (`__pycache__`, editor state) and are not datasets.
        if path.is_dir() and not path.name.startswith(("_", "."))
    ):
        problems.extend(rehash(directory, write=args.write))
    for problem in problems:
        print(("fixed: " if args.write else "stale: ") + problem)
    if not problems:
        print("every dataset manifest matches its case files")
        return 0
    return 0 if args.write else 1


if __name__ == "__main__":
    raise SystemExit(main())
