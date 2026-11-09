"""Recommendation gates: three new ``link_candidates.rejection`` codes.

Revision ID: 0011_recommendation_gates
Revises: 0010_qdrant_and_autonomy
Create Date: 2026-10-30

Phase 3 made geography and market structure HARD GATES instead of score contributions
(``ken_services.recommendations.gates``), and made the anchor the target's own market keyword
(``placement.market_keyword_anchors``). Three ``CandidateRejection`` members record the new
refusals, so the audit table and the ``rejected_<code>`` counters can explain them:

* ``geography_mismatch`` -- the target's primary geography is not the source's, does not
  contain it, is not global, and the source text does not name it;
* ``market_unrelated`` -- no declared taxonomy relation between the two markets;
* ``no_market_keyword_anchor`` -- the source text does not contain the target's market
  keyword verbatim.

Data only widens: ``ck_link_candidates_rejection_valid`` is replaced by the same CHECK with
three more allowed values. No column, index or trigger changes, and no stored row can become
invalid. Downgrade restores the 0004 CHECK exactly, and first fails loudly if any row already
carries one of the new codes (deleting audit rows is never this migration's decision).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0011_recommendation_gates"
down_revision: str | Sequence[str] | None = "0010_qdrant_and_autonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT: Final = "ck_link_candidates_rejection_valid"

REJECTIONS_0004: Final[tuple[str, ...]] = (
    "self_link",
    "source_protected",
    "target_protected",
    "experiment_locked",
    "source_ineligible",
    "target_ineligible",
    "already_linked",
    "duplicate_recommendation",
    "previously_rejected",
    "target_excluded",
    "page_type_incompatible",
    "below_min_relevance",
    "no_natural_anchor",
    "below_min_score",
    "link_density_exceeded",
    "per_source_limit",
    "diversity_limit",
)
"""Literal copy of ``ken_contracts.recommendations.CandidateRejection`` at revision 0004."""

ADDED_0011: Final[tuple[str, ...]] = (
    "geography_mismatch",
    "market_unrelated",
    "no_market_keyword_anchor",
)
REJECTIONS_0011: Final[tuple[str, ...]] = (*REJECTIONS_0004, *ADDED_0011)


def _check(values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"rejection IS NULL OR rejection IN ({allowed})"


def _replace(values: Sequence[str]) -> None:
    # op.f(): the name is already the final one, not a template for the naming convention.
    op.drop_constraint(op.f(CONSTRAINT), "link_candidates", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "link_candidates", sa.text(_check(values)))


def upgrade() -> None:
    _replace(REJECTIONS_0011)


def downgrade() -> None:
    allowed = ", ".join(f"'{value}'" for value in ADDED_0011)
    stale = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM link_candidates WHERE rejection IN ({allowed})")  # noqa: S608
    )
    count = stale.scalar_one()
    if count:
        raise RuntimeError(
            f"{count} link_candidates rows carry a gate rejection code; "
            "they would violate the 0004 CHECK (decide what to do with the audit rows first)"
        )
    _replace(REJECTIONS_0004)
