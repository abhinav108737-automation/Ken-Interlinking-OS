"""Helpers for the Phase 6 mutation/verification tests. Pure; no DB, no network."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Final

from ken_contracts.deployment import VerificationCheck, VerificationCheckName, VerificationOutcome
from ken_contracts.inventory import NormalizedPage
from ken_contracts.recommendations import PlacementPolicy
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.protocol import LinkInsertion, LinkRemoval
from ken_services.deployment.verification import HtmlLinkVerifier
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock, extract_blocks
from ken_services.recommendations.placement import phrase_occurrences
from tests.unit.intelligence_evals.demo_corpus import CORPUS_PATH
from tests.unit.intelligence_evals.demo_corpus import load_pages as _load_pages

EXCLUDED: Final = PlacementPolicy().excluded_ancestors
HREF: Final = "https://research.example.com/industry-reports/india-ev-charging-market"
OTHER_HREF: Final = "https://research.example.com/insights/battery-swapping"
MUTATOR: Final = HtmlMutator()
VERIFIER: Final = HtmlLinkVerifier()


def plan(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def block_for(html: str, anchor: str, *, contains: str | None = None) -> ContentBlock:
    for block in extract_blocks(html):
        if phrase_occurrences(anchor, block.text) and (contains is None or contains in block.text):
            return block
    raise AssertionError(f"no block holds {anchor!r}")


def insertion(
    html: str,
    anchor: str,
    *,
    occurrence: int = 0,
    href: str = HREF,
    plan_id: int = 1,
    contains: str | None = None,
    block: ContentBlock | None = None,
) -> LinkInsertion:
    """A plan for the ``occurrence``-th whole-phrase match of ``anchor`` (first block with it)."""
    target = block or block_for(html, anchor, contains=contains)
    start, end = phrase_occurrences(anchor, target.text)[occurrence]
    return LinkInsertion(
        plan_id=plan(plan_id),
        href=href,
        anchor_text=target.text[start:end],
        context_hash=sha256_hex(target.text),
        block_id_hint=target.block_id,
        occurrence_index=occurrence,
        start=start,
        end=end,
    )


def removal(item: LinkInsertion, **changes: object) -> LinkRemoval:
    out = LinkRemoval(
        plan_id=item.plan_id,
        expected_href=item.href,
        expected_anchor_text=item.anchor_text,
        context_hash=item.context_hash,
        block_id_hint=item.block_id_hint,
        start=item.start,
        end=item.end,
    )
    return replace(out, **changes)  # type: ignore[arg-type]


def codes(result: object) -> list[str]:
    issues = getattr(result, "issues", ())
    return [issue.code.value for issue in issues]


def outcomes(
    checks: list[VerificationCheck], plan_id: uuid.UUID | None = None
) -> dict[VerificationCheckName, VerificationOutcome]:
    return {c.check: c.outcome for c in checks if plan_id is None or c.plan_id == plan_id}


def details(checks: list[VerificationCheck]) -> dict[VerificationCheckName, str | None]:
    return {c.check: c.detail for c in checks}


# --------------------------------------------------------------------------- #
# Demo corpus: the stored body (normalised) and the export fragment of each page
# --------------------------------------------------------------------------- #
@cache
def demo_pages() -> tuple[NormalizedPage, ...]:
    return tuple(_load_pages())


@cache
def demo_fragments() -> dict[str, str]:
    """``url -> body_html`` exactly as the export file carries it (the CMS fragment)."""
    out: dict[str, str] = {}
    lines = Path(CORPUS_PATH).read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:
        record = json.loads(line)
        out[record["url"]] = record["body_html"]
    return out


BOILERPLATE_HEAD: Final = (
    '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
    '<title>Example Research</title><link rel="canonical" href="https://research.example.com/">'
    "<style>p{color:#222}</style><script>window.x = '<p>not content</p>';</script></head>\n"
    '<body>\n<header class="site"><a href="https://research.example.com/">Example Research</a>'
    '<nav><ul><li><a href="https://research.example.com/industry-reports">Industry reports</a>'
    '</li><li><a href="https://research.example.com/insights">Insights</a></li></ul></nav>'
    "</header>\n<!-- page body -->\n"
)
BOILERPLATE_TAIL: Final = (
    "\n<aside><p>Subscribe to our newsletter for battery storage updates.</p></aside>"
    '\n<footer><p>&copy; Example Research. <a href="https://research.example.com/privacy">'
    "Privacy</a></p></footer>\n</body></html>\n"
)


def full_document(fragment: str) -> str:
    """The fragment inside a synthetic rendered page with head, header, nav, aside, footer."""
    return BOILERPLATE_HEAD + fragment + BOILERPLATE_TAIL


def without_main(fragment: str) -> str:
    """The fragment with its ``<main>`` wrapper removed (an ``<article>`` remains)."""
    return fragment.replace("<main>", "", 1).replace("</main>", "", 1)


def bare(fragment: str) -> str:
    """Neither ``<main>`` nor ``<article>``: the content root becomes the document."""
    out = without_main(fragment)
    return out.replace("<article>", "", 1).replace("</article>", "", 1)


def candidate_spans(block: ContentBlock, limit: int = 3) -> Iterator[tuple[str, int]]:
    """Up to ``limit`` word-bigram anchors of a block, with their occurrence index."""
    words = block.text.split(" ")
    seen = 0
    step = max(1, (len(words) - 1) // limit) if len(words) > 1 else 1
    for i in range(0, max(0, len(words) - 1), step):
        phrase = f"{words[i]} {words[i + 1]}"
        occurrences = phrase_occurrences(phrase, block.text)
        position = sum(len(w) + 1 for w in words[:i])
        for index, (start, _end) in enumerate(occurrences):
            if start == position:
                yield phrase, index
                seen += 1
                break
        if seen >= limit:
            return
