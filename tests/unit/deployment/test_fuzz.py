"""Fuzz-style totality: random HTML never raises, and every outcome keeps the invariants.

Deterministic (seeded): the same strings every run. For each random document and each
candidate anchor of its blocks, ``insert_links`` either refuses (``html`` unchanged, no
``applied``) or applies, in which case ``remove_links`` restores the input byte for byte and
verification reports no failed check.
"""

from __future__ import annotations

import random

from ken_contracts.deployment import VerificationOutcome
from ken_services.deployment.source_map import map_document
from ken_services.inventory.normalize import extract_blocks
from tests.unit.deployment._support import (
    EXCLUDED,
    MUTATOR,
    VERIFIER,
    candidate_spans,
    insertion,
    removal,
)

SEED = 20261015
TOKENS = (
    "<p>",
    "</p>",
    "<div>",
    "</div>",
    "<main>",
    "</main>",
    "<article>",
    "<b>",
    "</b>",
    "<em>",
    "</em>",
    "<br>",
    "<br/>",
    '<a href="https://x.example/a">',
    "<a name=n/>",
    "</a>",
    "<td>",
    "<table>",
    "<li>",
    "<ul>",
    "<h2>",
    "</h2>",
    "<!-- c -->",
    "<!--",
    "-->",
    "<script>x<p>y</p></script>",
    "<style>p{}</style>",
    "<xmp>",
    '<span style="display:none">',
    "</span>",
    "<p hidden>",
    "&amp;",
    "&nbsp;",
    "&#169;",
    "&copy",
    "&bogus;",
    "&",
    "<",
    ">",
    '"',
    "'",
    "\n",
    "\r\n",
    "  ",
    "\t",
    "e\u0301",
    "\xe9",
    "\U0001f50b",
    "\x00",
    "<![CDATA[z]]>",
    "<!DOCTYPE html>",
    "<?pi?>",
    "</>",
    "</ x>",
    "<p class=a id='b' data-x=\"c\">",
)
WORDS = (
    "battery",
    "storage",
    "solar",
    "power",
    "India",
    "market",
    "grid-scale",
    "R&D",
    "caf\xe9",
)


def _random_html(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(1, 60)):
        if rng.random() < 0.55:
            parts.append(" ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 6))))
        else:
            parts.append(rng.choice(TOKENS))
        if rng.random() < 0.3:
            parts.append(" ")
    return "".join(parts)


def _random_garbage(rng: random.Random) -> str:
    alphabet = "<>/&;#=\"' \n\tabpx!-?\xe9\u0301\x00"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))


def test_random_html_never_raises_and_keeps_the_invariants() -> None:
    rng = random.Random(SEED)  # noqa: S311 - deterministic test data, not cryptography
    applied = refused = 0
    for _ in range(600):
        html = _random_html(rng) if rng.random() < 0.8 else _random_garbage(rng)
        doc = map_document(html)
        assert isinstance(doc.sound, bool)
        blocks = extract_blocks(html)
        for block in blocks:
            for phrase, occurrence in candidate_spans(block, limit=2):
                item = insertion(html, phrase, occurrence=occurrence, block=block)
                result = MUTATOR.insert_links(html, [item], excluded_ancestors=EXCLUDED)
                if not result.ok:
                    refused += 1
                    assert result.html == html and result.applied == ()
                    continue
                applied += 1
                undone = MUTATOR.remove_links(result.html, [removal(item)])
                assert undone.ok and undone.html == html, html
                checks = VERIFIER.verify_links(result.html, [item])
                assert not [c for c in checks if c.outcome is VerificationOutcome.FAILED], html
        # Verification and removal of plans that were never deployed never raise either.
        if blocks:
            phrase_occ = next(candidate_spans(blocks[0], limit=1), None)
            if phrase_occ is not None:
                item = insertion(html, phrase_occ[0], occurrence=phrase_occ[1], block=blocks[0])
                assert VERIFIER.verify_links(html, [item])
                assert MUTATOR.remove_links(html, [removal(item)]).html == html
    assert applied > 300 and refused > 100, (applied, refused)
