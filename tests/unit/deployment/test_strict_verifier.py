"""The STRICT verification profile's mutation-isolation check.

It must catch what the standard identity rule cannot see -- an unintended SECOND link to the
destination with different anchor text -- and must not change anything else, because
``AUTO_PUBLISH_WITH_EXTRA_VERIFICATION`` differs from ``AUTO_PUBLISH`` in the verification
profile and in nothing else.
"""

from __future__ import annotations

import uuid

from ken_contracts.deployment import (
    VerificationCheck,
    VerificationCheckName,
    VerificationOutcome,
)
from ken_services.deployment.protocol import LinkInsertion
from ken_services.deployment.strict import (
    DUPLICATE_DETAIL,
    StrictLinkVerifier,
    strict_verifier,
)
from ken_services.deployment.verification import HtmlLinkVerifier

PLAN = uuid.UUID("44444444-4444-4444-8444-444444444444")
HREF = "https://site.test/reports/batteries"
ANCHOR = "lithium-ion batteries"
_Name = VerificationCheckName
_Outcome = VerificationOutcome


def _insertion() -> LinkInsertion:
    from ken_services.inventory.hashing import sha256_hex

    text = f"The market for {ANCHOR} is growing quickly across the country."
    start = text.index(ANCHOR)
    return LinkInsertion(
        plan_id=PLAN,
        href=HREF,
        anchor_text=ANCHOR,
        context_hash=sha256_hex(text),
        block_id_hint="b0",
        occurrence_index=0,
        start=start,
        end=start + len(ANCHOR),
    )


def _html(extra: str = "") -> str:
    return (
        "<main><p>The market for "
        f'<a href="{HREF}" data-ken-link-id="{PLAN}">{ANCHOR}</a>'
        " is growing quickly across the country.</p>"
        f"{extra}</main>"
    )


def _outcomes(checks: list[VerificationCheck]) -> dict[str, VerificationOutcome]:
    return {VerificationCheckName(c.check).value: VerificationOutcome(c.outcome) for c in checks}


def test_both_profiles_pass_a_clean_page() -> None:
    insertions = [_insertion()]
    html = _html()
    standard = _outcomes(HtmlLinkVerifier().verify_links(html, insertions))
    strict = _outcomes(StrictLinkVerifier(HtmlLinkVerifier()).verify_links(html, insertions))
    assert standard == strict
    assert standard[_Name.LINK_PRESENT_ONCE.value] is _Outcome.PASSED


def test_strict_catches_an_unintended_second_link_to_the_destination() -> None:
    """A template or an editor added another link to the SAME destination with different
    anchor text. The standard identity rule (our id, or href AND anchor) does not see it."""
    insertions = [_insertion()]
    html = _html(extra=f'<p>See also our <a href="{HREF}">battery coverage</a>.</p>')

    standard = _outcomes(HtmlLinkVerifier().verify_links(html, insertions))
    assert standard[_Name.LINK_PRESENT_ONCE.value] is _Outcome.PASSED

    checks = StrictLinkVerifier(HtmlLinkVerifier()).verify_links(html, insertions)
    strict = _outcomes(checks)
    assert strict[_Name.LINK_PRESENT_ONCE.value] is _Outcome.FAILED
    detail = next(
        c.detail for c in checks if VerificationCheckName(c.check) is _Name.LINK_PRESENT_ONCE
    )
    assert detail == DUPLICATE_DETAIL


def test_strict_leaves_a_missing_link_failing_exactly_as_standard_does() -> None:
    insertions = [_insertion()]
    html = "<main><p>Nothing here at all.</p></main>"
    standard = _outcomes(HtmlLinkVerifier().verify_links(html, insertions))
    strict = _outcomes(StrictLinkVerifier(HtmlLinkVerifier()).verify_links(html, insertions))
    assert standard[_Name.LINK_PRESENT_ONCE.value] is _Outcome.FAILED
    assert strict == standard


def test_strict_never_turns_a_failure_into_a_pass() -> None:
    """The wrapper may only tighten. Across a handful of documents, no check that the
    standard profile failed is passed by the strict one."""
    insertions = [_insertion()]
    for html in (
        _html(),
        "<main><p>Nothing here at all.</p></main>",
        _html(extra=f'<p><a href="{HREF}">more</a></p>'),
        f'<main><p hidden>The market for <a href="{HREF}">{ANCHOR}</a> is growing.</p></main>',
    ):
        standard = _outcomes(HtmlLinkVerifier().verify_links(html, insertions))
        strict = _outcomes(StrictLinkVerifier(HtmlLinkVerifier()).verify_links(html, insertions))
        for name, outcome in standard.items():
            if outcome is _Outcome.FAILED:
                assert strict[name] is _Outcome.FAILED, (name, html)


def test_wrapping_is_idempotent() -> None:
    inner = HtmlLinkVerifier()
    once = strict_verifier(inner)
    assert strict_verifier(once) is once


def test_the_wrapper_is_total_on_a_hostile_document() -> None:
    insertions = [_insertion()]
    for html in ("", "<main", "<script>var a = '</script>'</script>", "\x00"):
        checks = StrictLinkVerifier(HtmlLinkVerifier()).verify_links(html, insertions)
        assert checks, html
