"""Fault injection for the demo. Clearly NOT part of the product.

The verifier below is the same instrument ``tests/integration/autonomy_support.FailingVerifier``
uses: it reports every verification check as FAILED while leaving the CMS page untouched. That
is the only way to reach "verification failed AND rolling back is safe" -- every other fault
(an editor removing the link, a template duplicating it) also moves the CMS page version, and
the CMS then correctly refuses to restore a snapshot of a page that has moved (it would destroy
somebody's edit). The deterministic mutation itself is never replaced.
"""

from __future__ import annotations

from typing import Any

from ken_contracts.deployment import (
    VerificationCheck,
    VerificationCheckName,
    VerificationOutcome,
)

__all__ = ["FaultedVerifier"]


class FaultedVerifier:
    """A link verifier that deterministically FAILS (fault injection, demo only)."""

    def __init__(self, *, detail: str = "link_missing") -> None:
        self.detail = detail
        self.calls = 0

    def verify_links(
        self, html: str, insertions: Any, *, target_reachable: Any = None
    ) -> list[VerificationCheck]:
        del html, target_reachable
        self.calls += 1
        return [
            VerificationCheck(
                plan_id=item.plan_id,
                check=name,
                outcome=VerificationOutcome.FAILED,
                detail=self.detail,
            )
            for item in insertions
            for name in (
                VerificationCheckName.LINK_PRESENT_ONCE,
                VerificationCheckName.HREF_MATCHES,
            )
        ]
