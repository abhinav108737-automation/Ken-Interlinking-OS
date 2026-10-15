"""``HtmlLinkVerifier`` (ADR-0014 sections 1.7, 6 and 8)."""

from __future__ import annotations

from uuid import UUID

import pytest

from ken_contracts.deployment import VerificationCheck
from ken_contracts.deployment import VerificationCheckName as Name
from ken_contracts.deployment import VerificationOutcome as Outcome
from ken_services.deployment.mutation import link_start_tag
from ken_services.deployment.protocol import LinkInsertion
from ken_services.deployment.verification import DETAILS
from tests.unit.deployment._support import (
    EXCLUDED,
    HREF,
    MUTATOR,
    OTHER_HREF,
    VERIFIER,
    details,
    full_document,
    insertion,
    outcomes,
    plan,
)

PAGE = (
    "<main><article><h1>India EV Charging</h1>"
    "<p>Demand for battery storage is rising across India.</p>"
    "<p>Rooftop solar and grid-scale storage are covered elsewhere.</p></article></main>"
)
ITEM = insertion(PAGE, "battery storage")
DEPLOYED = MUTATOR.insert_links(PAGE, [ITEM], excluded_ancestors=EXCLUDED).html
TAG = link_start_tag(HREF, ITEM.plan_id)
ELEMENT_CHECKS = (
    Name.HREF_MATCHES,
    Name.ANCHOR_MATCHES,
    Name.IN_EXPECTED_BLOCK,
    Name.NOT_HIDDEN,
    Name.NOT_NESTED,
)


def verify(
    html: str, *items: LinkInsertion, reachable: dict[UUID, bool | None] | None = None
) -> list[VerificationCheck]:
    return VERIFIER.verify_links(html, list(items or (ITEM,)), target_reachable=reachable)


def failed(checks: list[VerificationCheck]) -> set[Name]:
    return {c.check for c in checks if c.outcome is Outcome.FAILED}


def test_one_check_per_plan_and_name_in_enum_order() -> None:
    checks = verify(DEPLOYED)
    assert [c.check for c in checks] == list(Name)
    assert all(c.plan_id == ITEM.plan_id for c in checks)
    assert all(c.detail is None or c.detail in DETAILS for c in checks)


def test_present_once_passes_everything_and_skips_the_target() -> None:
    checks = verify(DEPLOYED)
    result = outcomes(checks)
    assert all(result[name] is Outcome.PASSED for name in list(Name)[:-1])
    assert result[Name.TARGET_REACHABLE] is Outcome.SKIPPED
    assert details(checks)[Name.TARGET_REACHABLE] == "not_checked"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, Outcome.PASSED), (False, Outcome.FAILED), (None, Outcome.SKIPPED)],
)
def test_target_reachable_comes_from_the_map(value: bool | None, expected: Outcome) -> None:
    checks = verify(DEPLOYED, reachable={ITEM.plan_id: value})
    assert outcomes(checks)[Name.TARGET_REACHABLE] is expected


def test_missing_plan_in_the_map_is_skipped() -> None:
    assert outcomes(verify(DEPLOYED, reachable={}))[Name.TARGET_REACHABLE] is Outcome.SKIPPED


def test_missing_link_fails_presence_and_skips_the_element_checks() -> None:
    checks = verify(PAGE)
    result = outcomes(checks)
    assert result[Name.LINK_PRESENT_ONCE] is Outcome.FAILED
    assert details(checks)[Name.LINK_PRESENT_ONCE] == "link_missing"
    assert all(result[name] is Outcome.SKIPPED for name in ELEMENT_CHECKS)


def test_link_present_twice_fails_presence() -> None:
    twice = DEPLOYED.replace(
        "Rooftop solar", f'<a href="{HREF}">battery storage</a> and rooftop solar'
    )
    checks = verify(twice)
    assert failed(checks) == {Name.LINK_PRESENT_ONCE}
    assert details(checks)[Name.LINK_PRESENT_ONCE] == "link_duplicated"


def test_wrong_href_fails() -> None:
    changed = DEPLOYED.replace(f'href="{HREF}"', f'href="{OTHER_HREF}"')
    checks = verify(changed)
    assert failed(checks) == {Name.HREF_MATCHES}


def test_equivalent_href_spelling_passes() -> None:
    respelled = DEPLOYED.replace(
        f'href="{HREF}"',
        'href="HTTPS://Research.Example.com:443/industry-reports/india-ev-charging-market"',
    )
    assert failed(verify(respelled)) == set()


def test_wrong_anchor_fails_anchor_and_block() -> None:
    changed = DEPLOYED.replace("battery storage</a>", "battery storage systems</a>")
    checks = verify(changed)
    assert failed(checks) == {Name.ANCHOR_MATCHES, Name.IN_EXPECTED_BLOCK}
    assert details(checks)[Name.IN_EXPECTED_BLOCK] == "block_changed"


def test_link_moved_to_another_block_fails_the_block_check() -> None:
    moved = PAGE.replace("Demand for battery storage is rising", "Demand is rising").replace(
        "grid-scale storage", f"grid-scale storage and {TAG}battery storage</a>"
    )
    checks = verify(moved)
    assert failed(checks) == {Name.IN_EXPECTED_BLOCK}
    assert details(checks)[Name.IN_EXPECTED_BLOCK] == "block_changed"


def test_link_moved_within_the_block_fails_with_span_moved() -> None:
    page = "<main><p>battery storage then battery storage again.</p></main>"
    first = insertion(page, "battery storage", occurrence=0)
    second_position = insertion(page, "battery storage", occurrence=1)
    deployed = MUTATOR.insert_links(page, [second_position], excluded_ancestors=EXCLUDED).html
    checks = verify(deployed, first)
    assert failed(checks) == {Name.IN_EXPECTED_BLOCK}
    assert details(checks)[Name.IN_EXPECTED_BLOCK] == "span_moved"


@pytest.mark.parametrize(
    "wrapper",
    [
        '<div style="display: NONE">{}</div>',
        '<div style="visibility:hidden">{}</div>',
        '<div style="color:red;display:none!important">{}</div>',
    ],
)
def test_hidden_via_inline_style_fails(wrapper: str) -> None:
    start = DEPLOYED.index("<p>Demand")
    end = DEPLOYED.index("</p>", start) + 4
    hidden = DEPLOYED[:start] + wrapper.format(DEPLOYED[start:end]) + DEPLOYED[end:]
    assert failed(verify(hidden)) == {Name.NOT_HIDDEN}


def test_hidden_attribute_fails_hidden_and_block() -> None:
    hidden = DEPLOYED.replace("<p>Demand", "<p hidden>Demand")
    checks = verify(hidden)
    assert failed(checks) == {Name.NOT_HIDDEN, Name.IN_EXPECTED_BLOCK}
    assert details(checks)[Name.IN_EXPECTED_BLOCK] == "not_in_content"


def test_hidden_link_itself_fails() -> None:
    hidden = DEPLOYED.replace(TAG, TAG.replace("<a ", '<a aria-hidden="true" '))
    assert Name.NOT_HIDDEN in failed(verify(hidden))


def test_nested_link_fails() -> None:
    nested = DEPLOYED.replace(TAG, f'<a href="{OTHER_HREF}">{TAG}').replace(
        "battery storage</a>", "battery storage</a></a>"
    )
    assert Name.NOT_NESTED in failed(verify(nested))


def test_link_containing_another_link_fails() -> None:
    inner = DEPLOYED.replace("battery storage</a>", f'battery <a href="{OTHER_HREF}">x</a></a>')
    checks = verify(inner)
    assert Name.NOT_NESTED in failed(checks)


def test_unclosed_link_before_ours_fails_not_nested() -> None:
    unclosed = DEPLOYED.replace("<h1>India", f'<h1><a href="{OTHER_HREF}"/>India')
    assert failed(verify(unclosed)) == {Name.NOT_NESTED}


def test_data_attribute_stripped_still_verifies() -> None:
    stripped = DEPLOYED.replace(f' data-ken-link-id="{ITEM.plan_id}"', "")
    assert failed(verify(stripped)) == set()


def test_a_link_with_another_plans_id_is_not_ours() -> None:
    other = DEPLOYED.replace(
        "Rooftop solar",
        f"{link_start_tag(HREF, plan(99))}battery storage</a> and rooftop solar",
    )
    assert failed(verify(other)) == set()
    stripped = other.replace(f' data-ken-link-id="{plan(99)}"', "")
    assert failed(verify(stripped)) == {Name.LINK_PRESENT_ONCE}


def test_reserialised_link_still_verifies() -> None:
    reserialised = DEPLOYED.replace(
        TAG, f"<A data-ken-link-id='{ITEM.plan_id}' rel=noopener HREF='{HREF}'>"
    ).replace("battery storage</a>", "battery storage</A>")
    assert failed(verify(reserialised)) == set()


def test_served_full_document_verifies_a_fragment_plan() -> None:
    assert failed(verify(full_document(DEPLOYED))) == set()


def test_multiple_plans_are_verified_independently() -> None:
    second = insertion(PAGE, "rooftop solar", plan_id=2, href=OTHER_HREF)
    only_first = verify(DEPLOYED, ITEM, second)
    assert len(only_first) == 2 * len(Name)
    assert outcomes(only_first, ITEM.plan_id)[Name.LINK_PRESENT_ONCE] is Outcome.PASSED
    assert outcomes(only_first, second.plan_id)[Name.LINK_PRESENT_ONCE] is Outcome.FAILED


@pytest.mark.parametrize(
    "garbage",
    ["", "<", "<a href=", "<<<>>>&&&;;", "\x00\x01<p>\ud7ff", "<a><a><a>" * 300, "<!--" * 50],
)
def test_garbage_never_raises(garbage: str) -> None:
    checks = verify(garbage)
    assert len(checks) == len(Name)
    assert outcomes(checks)[Name.LINK_PRESENT_ONCE] is Outcome.FAILED


# --------------------------------------------------------------------------- #
# Unsound documents (QA L2: a browser/html.parser differential) never pass
# --------------------------------------------------------------------------- #
DOUBLE_ESCAPED = (
    '<main><p>x</p><script>var a="<!--<script>";</script><p>EV charging market</p></script></main>'
)


def test_a_double_escaping_script_document_never_passes() -> None:
    """The html a pre-fix mutator produced: in a browser our <a> is script TEXT."""
    item = insertion(DOUBLE_ESCAPED, "EV charging")
    anchor = "EV charging"
    position = DOUBLE_ESCAPED.index(anchor)
    tag = link_start_tag(item.href, item.plan_id)
    html = f"{DOUBLE_ESCAPED[:position]}{tag}{anchor}</a>{DOUBLE_ESCAPED[position + len(anchor) :]}"
    checks = verify(html, item, reachable={item.plan_id: True})
    assert [c.check for c in checks] == list(Name)
    assert not any(c.outcome is Outcome.PASSED for c in checks if c.check in ELEMENT_CHECKS)
    result = outcomes(checks)
    assert result[Name.LINK_PRESENT_ONCE] is Outcome.FAILED
    assert all(result[name] is Outcome.FAILED for name in ELEMENT_CHECKS)
    assert {c.detail for c in checks if c.check is not Name.TARGET_REACHABLE} == {
        "unsound_document"
    }
    assert result[Name.TARGET_REACHABLE] is Outcome.PASSED  # a fact about the target only
    assert all(c.detail is None or c.detail in DETAILS for c in checks)


def test_a_script_with_a_comment_opener_alone_still_verifies() -> None:
    html = '<main><p>x</p><script>var a="<!--";</script><p>EV charging market</p></main>'
    item = insertion(html, "EV charging")
    deployed = MUTATOR.insert_links(html, [item], excluded_ancestors=EXCLUDED)
    assert deployed.ok
    result = outcomes(verify(deployed.html, item))
    assert all(result[name] is Outcome.PASSED for name in list(Name)[:-1])
