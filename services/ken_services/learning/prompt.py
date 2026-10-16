"""The reflection prompt: ENUM CODES, INTEGERS AND FLOATS ONLY (ADR-0016 15.7.6, binding).

No reviewer note, no anchor string, no page title, no URL, no identifier. The reason is a blast
radius, not squeamishness: an ``edited`` anchor is "copied verbatim from the block text"
(``ken_contracts.recommendations``), so anchor text IS page content -- and reflection is
CORPUS-LEVEL, so one poisoned anchor would steer EVERY proposal, which is strictly worse than
the per-page agent prompt's exposure.

Enforcement is by SHAPE, not by the author remembering:
:func:`~ken_services.learning.protocol.code_only_violations` accepts a string only if it
matches ``CODE_TOKEN_PATTERN`` -- no capital letter, no space, no slash, no colon -- so a title,
an anchor phrase, a URL and a note are all excluded before the renderer runs.
:func:`reflection_messages` REFUSES to build messages for a payload with any violation, and
:mod:`ken_services.learning.reflect` treats that refusal as a fallback: the deterministic rule
picks option 0 and the proposal records it. A prompt is never "nearly clean".

Two further narrowings this module takes deliberately, beyond 15.7.6:

* **The payload carries EDITORIAL numbers only.** No ``link_totals``, no
  ``positive_outcome_rate``, no confounder flags. A behavioural signal may BLOCK a gate and may
  never pass one (ADR-0016 16.7); letting a spoofable first-party count steer which option a
  model prefers would be passing one by the back door. Behavioural numbers stay in the evidence,
  where the gate reads them through :class:`~ken_services.learning.protocol.BlockOnly`.
* **No identifier of any kind.** Not a plan id, not a page id, not a proposal id. The model is
  given a table keyed by canonical decision class and a list of indices; that is the whole
  world it can see, so there is nothing for it to address or exfiltrate.

If structured free text is ever genuinely needed here, the answer is the ``DATA`` +
opaque-ref envelope in ``agents/ken_agents/prompts.py`` -- never loosening this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ken_contracts.learning import (
    MAX_OPTIONS_PER_PROPOSAL,
    MAX_RATIONALE_CHARS,
    LearningPolicy,
    ProposalOption,
    TargetKind,
)
from ken_contracts.model import Message
from ken_services.learning.aggregate import AggregateReport
from ken_services.learning.protocol import CODE_TOKEN_PATTERN, code_only_violations

__all__ = [
    "KEEP_CURRENT",
    "RATE_DIGITS",
    "REDACTED",
    "REFLECTION_SYSTEM_PROMPT",
    "PromptPayloadError",
    "payload_is_code_only",
    "reflection_messages",
    "reflection_payload",
]

KEEP_CURRENT: Final = "keep_current"
"""The label of the no-change option, which is ALWAYS the LAST one.

It lives here rather than beside the enumerator because the prompt names it, and the
dependency runs reflect -> prompt. Option 0 is the DETERMINISTIC rule's own preferred
candidate, so a disabled or failing model falls back to deterministic code rather than to
"do nothing" -- and a model that reads the table as inconclusive can still choose this one,
in which case no proposal is written at all."""

RATE_DIGITS: Final = 4
"""Rates are rounded before rendering, so the same aggregate gives a byte-identical prompt."""

REFLECTION_SYSTEM_PROMPT: Final = (
    "You help tune the internal-linking policy of a market-research website. DATA is a table "
    "of counts and rates that deterministic code computed from a database: it holds no website "
    "content, no human prose and no identifiers. Your ONLY job is to choose one of the numbered "
    "options that have already been prepared, and to explain the choice. You may not propose a "
    "value, name a configuration field, or invent an option. A human reviews every option you "
    "choose and nothing you say changes the website. Answer with one JSON object matching the "
    "requested schema and nothing else."
)


_CODE_TOKEN: Final = re.compile(CODE_TOKEN_PATTERN)
_INDEX: Final = re.compile(r"\[\d+\]")
REDACTED: Final = "<redacted>"
"""What a path segment that is not itself a code token is replaced with."""


def _safe_path(violation: str) -> str:
    """One ``code_only_violations`` entry, truncated at its first non-code path segment.

    ``code_only_violations`` reports a bad mapping KEY by putting it IN the JSON path, so a
    poisoned reason code would otherwise travel from the payload into this exception's message
    and from there into a log line. The guarantee here is exactly the module's own rule: every
    segment that survives is a code token, and the first segment that is not becomes
    :data:`REDACTED` -- which also ends the path, so nothing deeper can leak either. The result
    (``$.decision_classes.<redacted>: key is not a code token``) still locates the problem,
    while the text itself stays in the database, where it belongs.

    The reason is split off with ``rpartition``: a ``code_only_violations`` reason never
    contains ``": "``, but an injected key easily can.
    """
    path, separator, reason = violation.rpartition(": ")
    if not separator:  # pragma: no cover - every violation carries a reason today
        path, reason = violation, ""
    segments: list[str] = []
    for raw in path.split("."):
        probe = _INDEX.sub("", raw).strip("'\"")
        if probe in ("", "$") or _CODE_TOKEN.fullmatch(probe) is not None:
            segments.append(raw)
            continue
        segments.append(REDACTED)
        break
    return ".".join(segments) + (f": {reason}" if reason else "")


class PromptPayloadError(ValueError):
    """A payload that is not code-only.

    The message names the offending JSON path and nothing else: every segment of it is a code
    token, and the first one that is not is redacted. Repeating the offending value or key would
    put the very text this module exists to exclude into a log line.
    """

    def __init__(self, violations: Sequence[str]) -> None:
        safe = tuple(_safe_path(violation) for violation in violations)
        super().__init__(
            "a reflection prompt carries enum codes, integers and floats only; offending "
            "paths: " + ", ".join(safe[:10])
        )
        self.violations = safe


def _rate(value: float | None) -> float | None:
    return None if value is None else round(value, RATE_DIGITS)


def reflection_payload(
    *,
    report: AggregateReport,
    target_kind: TargetKind,
    options: Sequence[ProposalOption],
    policy: LearningPolicy,
) -> dict[str, Any]:
    """The JSON value rendered as ``DATA``. Pure: the same report gives the same payload.

    Every leaf is an ``int``, a ``float``, a ``bool``, ``None`` or a lowercase code. Keys are
    canonical decision classes (``same_market|target_topic|semantic``) and field names that are
    themselves codes, so :func:`code_only_violations` accepts the whole structure.
    """
    classes: dict[str, Any] = {}
    for row in report.rows:
        observation = row.observation
        classes[observation.decision_class] = {
            "sample_count": observation.sample_count,
            "approvals": observation.approvals,
            "edits": observation.edits,
            "rejections": observation.rejections,
            "rollbacks": observation.rollbacks,
            "approval_rate": _rate(observation.approval_rate),
            "edit_rate": _rate(observation.edit_rate),
            "reject_rate": _rate(observation.reject_rate),
            "rollback_rate": _rate(observation.rollback_rate),
            "anchor_edit_char_delta": observation.anchor_edit_char_delta,
            "reasons": dict(sorted(observation.reason_histogram.items())),
            "sufficient": observation.sufficient,
        }
    return {
        "target_kind": target_kind.value,
        "window_days": (report.window.end - report.window.start).days + 1,
        "min_sample_count": policy.gates.min_sample_count,
        "totals": {
            "decision_classes": len(report.rows),
            "sample_count": report.sample_count_total,
            "classes_below_sample_floor": report.classes_below_floor,
        },
        "decision_classes": classes,
        "options": [
            {
                "index": option.index,
                "label": option.label,
                "change_count": len(option.changes),
                "change_paths": sorted(change.path for change in option.changes),
            }
            for option in options
        ],
    }


def reflection_messages(
    *,
    report: AggregateReport,
    target_kind: TargetKind,
    options: Sequence[ProposalOption],
    policy: LearningPolicy,
) -> list[Message]:
    """The REASONING-role messages, or a refusal.

    Raises:
        PromptPayloadError: the payload is not code-only (15.7.6). The caller records a
            fallback and uses the deterministic choice; it never sends a "nearly clean" prompt.
        ValueError: no option was offered, or more than ``MAX_OPTIONS_PER_PROPOSAL``.
    """
    if not options:
        raise ValueError("a choice needs at least one option")
    if len(options) > MAX_OPTIONS_PER_PROPOSAL:
        raise ValueError(f"at most {MAX_OPTIONS_PER_PROPOSAL} options may be offered")
    payload = reflection_payload(
        report=report, target_kind=target_kind, options=options, policy=policy
    )
    violations = code_only_violations(payload)
    if violations:
        raise PromptPayloadError(violations)
    last = len(options) - 1
    task = (
        f"Choose exactly one option by index. There are {len(options)} options, indexed 0 to "
        f"{last}. They are ordered by how strongly the counts point at them, so index 0 is "
        "already the option deterministic code prefers. The LAST index "
        f"({last}, labelled {KEEP_CURRENT}) means change nothing; choose it whenever the "
        "counts do not support a change -- a decision class whose sufficient flag is false has "
        "too few samples to support any change at all. Schema: "
        '{"title": "<= 200 chars", "rationale": "<= '
        f'{MAX_RATIONALE_CHARS} chars", "choice_index": <int>, "option_count": {len(options)}'
        "}. Set option_count to exactly "
        f"{len(options)}."
        f"\n\nDATA:\n{json.dumps(payload, sort_keys=True, ensure_ascii=False)}"
    )
    return [
        Message(role="system", content=REFLECTION_SYSTEM_PROMPT),
        Message(role="user", content=task),
    ]


def payload_is_code_only(payload: Mapping[str, Any]) -> bool:
    """Convenience predicate for callers and tests; the renderer enforces it itself."""
    return not code_only_violations(payload)
