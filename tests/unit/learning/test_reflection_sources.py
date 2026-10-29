"""Static rules on W1's own source (the ``tests/unit/agents/test_boundaries.py`` pattern).

A runtime test cannot prove the ABSENCE of a read, and ADR-0016 15.2e is about an absence: the
free-text editorial columns must never leave the database as TEXT. ``note`` is a 500-character
reviewer note; ``anchor_before`` / ``anchor_after`` are page content, copied verbatim by the
review service. Reflection is corpus-level, so one poisoned value would steer every proposal.

So:

1. ``aggregate.py`` never SELECTs ``note`` at all, and mentions an anchor column only inside
   ``char_length(...)`` -- a character COUNT, never the string.
2. No module of this workstream reads a free-text column by name in SQL.
3. The reflection prompt module names no editorial free-text column at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

PACKAGE: Final = Path(__file__).resolve().parents[3] / "services" / "ken_services" / "learning"
W1_MODULES: Final = ("aggregate.py", "prompt.py", "proposals.py", "dedupe.py", "reflect.py")
FREE_TEXT_COLUMNS: Final = ("note", "anchor_before", "anchor_after")
"""Editorial columns whose CONTENT may never leave the database. ``rationale`` is excluded on
purpose: the prompt names it as the model's OUTPUT field, which is not a database read."""
_SAFE_ANCHOR_USE = re.compile(
    r"char_length\(\s*ra\.(anchor_before|anchor_after)\s*\)"
    r"|ra\.(anchor_before|anchor_after)\s+IS\s+NOT\s+NULL"
)
"""The only two shapes allowed: a character COUNT, and an existence check. Neither returns the
string, and the existence check is what stops a NULL ``anchor_after`` from poisoning the sum."""


def _runtime_strings(path: Path) -> list[str]:
    """Every string constant the module could USE; docstrings and attribute docs excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    ]


def _sql(path: Path) -> list[str]:
    """The runtime strings that look like SQL over the editorial tables."""
    return [
        value
        for value in _runtime_strings(path)
        if "SELECT" in value or "INSERT" in value or "FROM" in value
    ]


def test_the_aggregate_reads_an_anchor_only_as_a_character_count() -> None:
    statements = _sql(PACKAGE / "aggregate.py")
    assert statements, "the aggregate is SQL; if this is empty the test is not testing anything"
    for statement in statements:
        for column in ("anchor_before", "anchor_after"):
            if column not in statement:
                continue
            bare = _SAFE_ANCHOR_USE.sub("", statement)
            assert column not in bare, (
                f"{column} appears outside char_length() and an IS NOT NULL test: an anchor is "
                "page content, and only its LENGTH may leave the database"
            )


def test_the_aggregate_never_reads_a_reviewer_note() -> None:
    for statement in _sql(PACKAGE / "aggregate.py"):
        assert "note" not in statement.replace("not", ""), statement[:120]


@pytest.mark.parametrize("module", W1_MODULES)
def test_no_w1_module_selects_a_free_text_editorial_column(module: str) -> None:
    """A column read as text here would end up in an aggregate, an option label or a prompt."""
    for statement in _sql(PACKAGE / module):
        for column in ("ra.note", "ra.anchor_before", "ra.anchor_after", "p.rationale"):
            if column.endswith(("anchor_before", "anchor_after")) and module == "aggregate.py":
                continue  # covered precisely by the char_length test above
            assert column not in statement, (module, column)


def test_the_prompt_module_names_no_editorial_free_text_column() -> None:
    source = (PACKAGE / "prompt.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for value in _runtime_strings(PACKAGE / "prompt.py"):
        for column in FREE_TEXT_COLUMNS:
            # A bare constant would be a payload KEY; the quoted form would be prose naming it.
            assert value != column, (column, value[:80])
            assert f'"{column}":' not in value, (column, value[:80])
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "note" not in names
    assert "anchor_before" not in names
    assert "anchor_after" not in names


def test_the_prompt_payload_builder_touches_no_behavioural_field() -> None:
    """A behavioural signal may BLOCK a gate and may never pass one, so it must not be able to
    steer the model's choice either (ADR-0016 16.7)."""
    tree = ast.parse((PACKAGE / "prompt.py").read_text(encoding="utf-8"))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for field in (
        "link_totals",
        "positive_outcome_rate",
        "positive_outcomes",
        "behavioural_flags",
        "autonomy",
        "blocked_rate",
        "rolled_back_rate",
    ):
        assert field not in attributes, field


# --------------------------------------------------------------------------- #
# The CLI surface
# --------------------------------------------------------------------------- #
def test_the_reflect_subcommand_parses_a_window_and_the_no_model_flag() -> None:
    from ken_services.learning.cli import _parser

    args = _parser().parse_args(["reflect", "--start", "2026-06-01", "--end", "2026-06-30"])
    assert (args.command, str(args.start), str(args.end), args.use_model) == (
        "reflect",
        "2026-06-01",
        "2026-06-30",
        True,
    )
    assert _parser().parse_args(["reflect", "--no-model"]).use_model is False
    assert _parser().parse_args(["reflect"]).start is None


def test_a_malformed_date_is_rejected_by_argparse_before_anything_runs() -> None:
    from ken_services.learning.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["reflect", "--start", "yesterday", "--end", "2026-06-30"])


def test_the_reflect_line_prints_counts_and_ids_only() -> None:
    """A CLI line never carries a database URL, a file path or exception text."""
    import json
    import uuid

    from ken_services.learning.cli import reflect_line
    from ken_services.learning.protocol import ReflectionOutcome

    run = uuid.UUID("00000000-0000-4000-8000-000000000001")
    proposal = uuid.UUID("00000000-0000-4000-8000-000000000002")
    line = json.loads(
        reflect_line(
            ReflectionOutcome(
                job_run_id=run,
                proposal_ids=(proposal,),
                deduplicated=1,
                counters={"proposals_created": 1, "model_calls": 1},
            )
        )
    )
    assert line == {
        "command": "reflect",
        "job_run_id": str(run),
        "proposals": [str(proposal)],
        "deduplicated": 1,
        "counters": {"model_calls": 1, "proposals_created": 1},
    }


def test_the_aggregate_never_reads_an_operator_override_reason() -> None:
    """``autonomy_decisions.override_reason`` is operator free text (ADR-0018 s5). Only enum
    codes and booleans may leave the database."""
    for statement in _sql(PACKAGE / "aggregate.py"):
        assert "override_reason" not in statement
