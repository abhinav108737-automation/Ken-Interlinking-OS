#!/usr/bin/env python
"""The offline eval runner (ADR-0016 section 7, TRD section 19). Report only; promotes nothing.

    .venv/bin/python evals/runner.py --suites all
    .venv/bin/python evals/runner.py --suites protected-content,stale-page-version --no-db
    make eval SUITES=all
    make eval SUITES=destination-selection PROPOSAL=<uuid>

This file is a thin entry point. The runner itself lives in
``services/ken_services/learning/evaluate.py`` (``run_evaluation``, the frozen
``RunEvaluation``), because ``make eval`` resolves ``ken_services.learning.cli`` and a job run
needs the service package; keeping the logic there means the suites, the scorers, the metrics
and the gate are covered by mypy and by the unit tests rather than living in a script.

What a run does, and does not do:

* loads each suite's versioned JSONL dataset and VERIFIES every file's sha256 against its
  ``MANIFEST.yaml`` -- a dataset edited without re-hashing fails the run;
* scores every case by driving the production decision function for its suite;
* computes per-suite pass rates, ``must_not`` violations at zero tolerance, precision@1 /
  recall@3 / Kendall tau against recorded human preferences, critic precision and recall, and
  reliability bins per decision class (REPORTED, never gating in Phase 8);
* evaluates the gate from ``policies/learning/learning.v1.yaml``, where a hard-fail suite
  cannot be waived and a decision class below the sample floor makes the result
  ``inconclusive`` rather than ``pass``;
* writes ONE report: a JSON file under ``evals/results/`` always, plus one append-only
  ``policy_evaluations`` row when the run names a proposal or a skill. It never activates,
  promotes or reconfigures anything (``evals/README.md`` section 6, "read-only results").

Determinism: the default provider is ``mock``, so the same datasets and seed give a
byte-identical report and CI can gate on it. ``--provider ollama`` needs
``KEN_RUN_LIVE_OLLAMA=1``, talks only to the local daemon, and its gate result is forced to
``inconclusive`` -- a live model may inform, never gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _root in ("contracts/python", "services", "agents"):
    _path = str(REPO_ROOT / _root)
    if _path not in sys.path:
        sys.path.insert(0, _path)


def main() -> int:
    from ken_services.learning.evaluate import eval_main

    return eval_main()


if __name__ == "__main__":
    raise SystemExit(main())
