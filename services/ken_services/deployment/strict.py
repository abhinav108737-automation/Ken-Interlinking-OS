"""The STRICT verification profile's mutation-isolation check (ADR-0018 s4, directive s15).

:class:`StrictLinkVerifier` wraps the ordinary
:class:`~ken_services.deployment.protocol.LinkVerifier` and tightens exactly two things.
Everything else -- which plans may be deployed, every deterministic safety rule, what a
failure does -- is identical, because
:attr:`~ken_contracts.autonomy.AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION` is
allowed to differ from :attr:`~ken_contracts.autonomy.AutonomyDecision.AUTO_PUBLISH` in the
verification profile and in nothing else.

**1. A wider duplicate check.** The standard ``link_present_once`` counts links that are
*recognisably ours*: our ``data-ken-link-id``, or an un-tagged link whose href AND anchor text
both match the plan. That is the right identity rule for deciding whether to splice. It also
means an unintended SECOND link to the same destination, written with different anchor text
by a CMS template, an editor or a buggy second apply, is invisible to it. The strict profile
counts every ``<a>`` in the content root whose href is equivalent to the plan's; more than one
is ``link_duplicated``, so "no duplicate unintended link appeared" is actually checked.

**2. The expected block is required, not merely not-contradicted.** The standard profile
reports ``in_expected_block`` as ``skipped`` when there is no candidate at all (the plan
already fails ``link_present_once``). The strict profile additionally refuses to let an
``in_expected_block`` that came back ``skipped`` sit beside a PASSING ``link_present_once``:
that combination can only arise from a document the mapper could not place our link in, and
"we could not tell where it is" is not a pass when the link was published without a human.

Total, like the verifier it wraps: any exception falls back to the inner result rather than
failing a publish on a verification bug. ``map_document`` soundness is already handled inside
the inner verifier (an unsound document passes nothing).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from ken_contracts.deployment import (
    VerificationCheck,
    VerificationCheckName,
    VerificationOutcome,
)
from ken_services.deployment.href_rules import hrefs_equivalent
from ken_services.deployment.protocol import LinkInsertion, LinkVerifier
from ken_services.deployment.source_map import map_document

__all__ = ["DUPLICATE_DETAIL", "UNPLACED_DETAIL", "StrictLinkVerifier", "strict_verifier"]

DUPLICATE_DETAIL: Final = "link_duplicated"
UNPLACED_DETAIL: Final = "not_in_content"

_Name = VerificationCheckName
_Outcome = VerificationOutcome


def _destination_links(html: str, insertions: Sequence[LinkInsertion]) -> dict[UUID, int]:
    """plan -> how many content-root links point at that plan's destination."""
    doc = map_document(html)
    counts: dict[UUID, int] = {}
    for item in insertions:
        counts[item.plan_id] = sum(
            1
            for link in doc.links
            if link.in_root and link.href and hrefs_equivalent(link.href, item.href)
        )
    return counts


class StrictLinkVerifier:
    """A :class:`LinkVerifier` that applies the two tightenings in the module docstring."""

    def __init__(self, inner: LinkVerifier) -> None:
        self._inner = inner

    def verify_links(
        self,
        html: str,
        insertions: Sequence[LinkInsertion],
        *,
        target_reachable: dict[UUID, bool | None] | None = None,
    ) -> list[VerificationCheck]:
        checks = self._inner.verify_links(html, insertions, target_reachable=target_reachable)
        try:
            counts = _destination_links(html, insertions)
        except Exception:  # pragma: no cover - totality guard
            return checks
        placed: dict[UUID, VerificationOutcome] = {
            c.plan_id: VerificationOutcome(c.outcome)
            for c in checks
            if VerificationCheckName(c.check) is _Name.IN_EXPECTED_BLOCK
        }
        out: list[VerificationCheck] = []
        for check in checks:
            name = VerificationCheckName(check.check)
            outcome = VerificationOutcome(check.outcome)
            detail = check.detail
            if name is _Name.LINK_PRESENT_ONCE and outcome is _Outcome.PASSED:
                if counts.get(check.plan_id, 0) > 1:
                    outcome, detail = _Outcome.FAILED, DUPLICATE_DETAIL
                elif placed.get(check.plan_id) is _Outcome.SKIPPED:
                    outcome, detail = _Outcome.FAILED, UNPLACED_DETAIL
            out.append(
                VerificationCheck(plan_id=check.plan_id, check=name, outcome=outcome, detail=detail)
            )
        return out


def strict_verifier(inner: LinkVerifier) -> LinkVerifier:
    """Wrap ``inner`` once; wrapping a wrapper again would be a no-op but is avoided."""
    if isinstance(inner, StrictLinkVerifier):
        return inner
    return StrictLinkVerifier(inner)
