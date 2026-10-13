"""Deterministic text cleanup shared by model providers.

These helpers never *interpret* model output; they only remove known wrapper
noise (reasoning blocks, Markdown fences, surrounding prose) so that the
remaining text can be handed to Pydantic. Validation is always the caller's
job -- nothing here produces a value that bypasses a schema (TRD.md section 6).
"""

from __future__ import annotations

import json
import re

# A complete reasoning block. Non-greedy and DOTALL so several blocks spanning
# newlines are each removed without swallowing the content between them.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# An unterminated opening tag: everything after it is reasoning, not answer.
_THINK_OPEN_UNCLOSED = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
# A stray closing tag whose opening tag was injected by the chat template and
# therefore never appears in the generated text.
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)
_FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\n?(.*?)```", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove ``<think>`` reasoning blocks from ``text``.

    Handles three shapes: complete blocks, a dangling ``</think>`` (everything
    before it is reasoning), and an unclosed ``<think>`` (everything after it
    is reasoning). The result is whitespace-stripped.
    """
    cleaned = _THINK_BLOCK.sub("", text)
    closes = list(_THINK_CLOSE.finditer(cleaned))
    if closes:
        cleaned = cleaned[closes[-1].end() :]
    cleaned = _THINK_OPEN_UNCLOSED.sub("", cleaned)
    return cleaned.strip()


def extract_json_object(text: str) -> str | None:
    """Return the first syntactically complete JSON object found in ``text``.

    Looks inside Markdown code fences first, then scans the text for the first
    ``{`` that starts a decodable object. Returns the exact substring (not a
    re-serialisation), or ``None`` when no JSON object is present. This only
    checks JSON *syntax*; schema validation remains the caller's job.
    """
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    candidates.append(text)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            try:
                obj, end = decoder.raw_decode(candidate, start)
            except json.JSONDecodeError:
                start = candidate.find("{", start + 1)
                continue
            if isinstance(obj, dict):
                return candidate[start:end]
            start = candidate.find("{", end)
    return None


def clean_structured_output(raw: str) -> tuple[str, bool]:
    """Prepare raw model output for schema validation.

    Returns ``(candidate, repaired)``. ``repaired`` is True when cleanup changed
    the text beyond leading/trailing whitespace, i.e. when the model did not
    emit a bare JSON object.
    """
    without_think = strip_think(raw)
    extracted = extract_json_object(without_think)
    candidate = extracted if extracted is not None else without_think
    return candidate, candidate != raw.strip()
