"""Output and acceptance bookkeeping for the demo.

Everything the demo prints goes through :class:`Console`, so the report body is one stream that
can be normalised and diffed between runs, and every acceptance check is recorded with its
evidence (a check that is never recorded is a check that failed).
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, TextIO

__all__ = ["CHECKS", "Check", "Console", "normalise"]

WIDTH: Final = 100

CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("1", "BLOCK: a deterministic code blocks, with zero CMS calls"),
    ("2", "AUTO_PUBLISH_WITH_EXTRA_VERIFICATION: strict publish and live link"),
    ("3", "NO_ACTION: a declined plan writes no decision row and publishes nothing"),
    ("4", "AUTO_PUBLISH (plain) only after a calibrated class (SYNTHETIC fixture)"),
    ("5", "Rollback after failed verification, byte-identical page, retry refused"),
    ("6", "Modes: shadow counterfactual, guarded deferral, autonomous publish"),
    ("7", "Analytics: collector filters, synthetic rows, no plan becomes measured"),
    ("8", "Learning: evals, inconclusive reflection, synthetic proposal, promotion refused"),
    ("9", "Governance: raw SQL promotion refused by the database"),
    ("P", "Pipeline integrity: every stage produced real, non-empty, consistent output"),
)


@dataclass(slots=True)
class Check:
    key: str
    title: str
    passed: bool | None = None
    evidence: list[str] = field(default_factory=list)


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PORT = re.compile(r"127\.0\.0\.1:\d{4,5}")
_ELAPSED = re.compile(r"elapsed \d+(?:\.\d+)?s")
_DEPLOYMENT = re.compile(r"\bsim-dep-[0-9a-f]+\b|\bdep-[0-9a-f]{8,}\b")
_HEX = re.compile(r"\b[0-9a-f]{12,64}\b")


def normalise(text: str) -> str:
    """The report with generated ids, timestamps, ports and durations replaced, so two runs
    can be diffed. Only these classes of value are rewritten; decisions, counts, scores and
    reasons are left untouched."""
    out = _UUID.sub("<id>", text)
    out = _TIMESTAMP.sub("<ts>", out)
    out = _DATE.sub("<date>", out)
    out = _PORT.sub("127.0.0.1:<port>", out)
    out = _DEPLOYMENT.sub("<deployment>", out)
    out = _HEX.sub("<hex>", out)
    return _ELAPSED.sub("elapsed <n>s", out)


class Console:
    """Line-oriented output plus the acceptance ledger."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self.checks: dict[str, Check] = {key: Check(key, title) for key, title in CHECKS}
        self.proved: list[tuple[str, str]] = []
        self.lines: list[str] = []
        self._started = time.monotonic()

    # ---------------------------------------------------------------- output
    def line(self, text: str = "") -> None:
        self.lines.append(text)
        print(text, file=self._stream, flush=True)

    def banner(self, lines: Sequence[str]) -> None:
        bar = "=" * WIDTH
        self.line(bar)
        for text in lines:
            self.line(f"  {text}")
        self.line(bar)

    def stage(self, number: int, title: str) -> None:
        self.line()
        self.line(f"--- Stage {number}: {title} " + "-" * max(0, WIDTH - 14 - len(title)))

    def note(self, text: str) -> None:
        self.line(f"  {text}")

    def kv(self, label: str, value: object) -> None:
        self.line(f"  {label:<44} {value}")

    def table(self, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
        cells = [[str(c) for c in row] for row in rows]
        widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(header)]
        self.line("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
        self.line("  " + "  ".join("-" * w for w in widths))
        for row in cells:
            self.line("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))

    # ------------------------------------------------------------ acceptance
    def evidence(self, key: str, text: str) -> None:
        """One printed line of evidence attached to acceptance check ``key``."""
        self.checks[key].evidence.append(text)
        self.line(f"  [check {key}] {text}")

    def require(self, key: str, condition: bool, text: str) -> None:
        """Record (and print) one assertion of check ``key``. Never raises: the run goes on,
        the check is marked failed, and the process exits non-zero at the end."""
        check = self.checks[key]
        check.passed = condition if check.passed is None else (check.passed and condition)
        mark = "PASS" if condition else "FAIL"
        check.evidence.append(f"{mark}: {text}")
        self.line(f"  [check {key}] {mark}: {text}")

    def proves(self, what: str, detail: str) -> None:
        self.proved.append((what, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks.values() if c.passed is not True]

    def elapsed(self) -> float:
        return time.monotonic() - self._started
