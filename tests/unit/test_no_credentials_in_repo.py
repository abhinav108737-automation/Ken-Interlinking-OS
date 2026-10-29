"""No credential may be tracked by git (CLAUDE.md constraint 20: never persist secrets in source).

A secret reaching GitHub is not recoverable by deleting it later: the history keeps it, forks and
clones keep it, and it must then be revoked. So the only defence that counts is the one that
runs BEFORE the commit is shared. This test is that defence for CI and for ``make test``.

It scans every file git TRACKS for the shapes real credentials take, and it fails the build on a
match. It reports the FILE and the KIND of credential, and never the matching text, so a failure
cannot itself leak the thing it found.

What it does NOT do, honestly: it cannot see a secret in a file that is untracked or ignored
(those never reach GitHub, which is the point of ``.gitignore``), and it cannot recognise a
credential that has no distinctive shape, such as a bare password. It is a net, not a proof.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]

# Built from parts so that THIS file does not contain a literal match for its own rules.
_PEM: Final = "-----" + "BEGIN"
_RULES: Final[dict[str, re.Pattern[str]]] = {
    "a PEM private key": re.compile(_PEM + r" ([A-Z]+ )?PRIVATE KEY" + "-----"),
    "a Google service-account key file": re.compile(r'"type"\s*:\s*"service' + r'_account"'),
    "a Google private_key_id": re.compile(r'"private' + r'_key_id"\s*:\s*"[0-9a-f]{20,}"'),
    "a Google OAuth access token": re.compile(r"\bya" + r"29\.[0-9A-Za-z_\-]{30,}"),
    "a Google API key": re.compile(r"\bAI" + r"za[0-9A-Za-z_\-]{35}\b"),
    "a GitHub token": re.compile(r"\bgh[pousr]" + r"_[0-9A-Za-z]{36,}\b"),
    "an AWS access key id": re.compile(r"\bAK" + r"IA[0-9A-Z]{16}\b"),
    "a Slack token": re.compile(r"\bxox[baprs]" + r"-[0-9A-Za-z\-]{10,}"),
}

# Files that legitimately contain a FAKE value in the shape of a real one, so the code that
# handles such values can be tested. Each entry is a reason, not a convenience: keep it short.
_ALLOWED_FAKES: Final[dict[str, str]] = {
    "tests/unit/test_analytics_google.py": "a fake access token used to prove it is never logged",
    "tests/unit/test_no_credentials_in_repo.py": "this file defines the rules",
}

_BINARY_SUFFIXES: Final = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".gz", ".lock"}
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 - fixed argv, no shell; git comes from PATH
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout, so there is nothing tracked to scan")
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def test_no_tracked_file_contains_a_credential() -> None:
    findings: list[str] = []
    for name in _tracked_files():
        if name in _ALLOWED_FAKES or Path(name).suffix.lower() in _BINARY_SUFFIXES:
            continue
        path = REPO / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(
            f"{name}: looks like {kind}" for kind, rule in _RULES.items() if rule.search(text)
        )
    assert not findings, (
        "credential-shaped content is tracked by git (values deliberately not shown):\n  "
        + "\n  ".join(findings)
        + "\nRemove it, ROTATE the credential, and do not push. Deleting a committed secret does "
        "not remove it from history."
    )


def test_the_env_files_that_hold_real_values_are_ignored() -> None:
    """``.env`` is where a developer's real settings live; it must never be trackable."""
    result = subprocess.run(
        ["git", "check-ignore", ".env", "keys/anything.json"],  # noqa: S607 - fixed argv, no shell
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        pytest.skip("git check-ignore is unavailable")
    ignored = set(result.stdout.split())
    assert {".env", "keys/anything.json"} <= ignored, f"not ignored: {ignored}"


@pytest.mark.parametrize("kind", sorted(_RULES))
def test_each_rule_actually_matches_the_shape_it_claims(kind: str) -> None:
    """A scanner that never fires is worse than none, because it reads as safety."""
    samples: dict[str, str] = {
        "a PEM private key": _PEM + " PRIVATE KEY" + "-----\nMIIE",
        "a Google service-account key file": '{"type": "service' + '_account"}',
        "a Google private_key_id": '"private' + '_key_id": "' + "a1" * 20 + '"',
        "a Google OAuth access token": "ya" + "29." + "A" * 40,
        "a Google API key": "AI" + "za" + "B" * 35,
        "a GitHub token": "gh" + "p_" + "C" * 36,
        "an AWS access key id": "AK" + "IA" + "D" * 16,
        "a Slack token": "xox" + "b-" + "1234567890-abcdef",
    }
    assert _RULES[kind].search(samples[kind]), f"the rule for {kind} does not match its own shape"
