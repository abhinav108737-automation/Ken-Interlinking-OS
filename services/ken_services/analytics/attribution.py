"""The attribution chain and the live windows (ADR-0015 §3 as amended by §12.6.3).

The PLAN is the attribution root, and ``link_plans`` content is immutable, so nothing here is
stored: :func:`link_attribution` recomputes the chain with ONE query every time.

    plan -> recommendation run -> versions -> source/target (+ versions)
         -> agent run (verdict, model, graph version) -> approving human
         -> deployment (provider, verified_at) -> rollback

**The live window comes from ``link_plan_transitions`` only** (amendment 3): the first move to
``deployed`` opens it and the first move to ``rolled_back`` closes it. The
``deployment_items`` stage must NOT be used -- a CMS no-op or a replay records
``already_applied`` while the plan still becomes ``deployed``, and a per-plan REMOVE
deployment ends the window through the ``rolled_back`` transition, not through a stage.

A plan that never reached ``deployed`` was never on a page (superseded, rejected, expired,
never applied): its window has ``deployed_at = None``, its imported rows are
``outside_live_window`` and its first-party events are ``link_not_live``.

**Experiments are DERIVED, never stamped** (§12.6.2): Phase 7 has no experiment tables, so
the derived experiment of every plan is "none". Phase 8 adds the assignment table and this
module gains the join; no event row ever carries an experiment id.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.agents import CriticVerdict
from ken_contracts.analytics import LinkAttribution, LiveWindow
from ken_contracts.intelligence import RelationshipType
from ken_contracts.recommendations import LinkPlanStatus, PlanNotFoundError, ReasonCode
from ken_contracts.review import ReviewAction
from ken_core.db.repositories import analytics as repo

__all__ = ["ATTRIBUTION_SQL", "link_attribution", "live_windows"]

ATTRIBUTION_SQL: Final = """
SELECT p.id, p.status, p.anchor_text, p.anchor_source, p.relationship_type, p.reason_codes,
       p.supersedes, p.run_id,
       p.engine_version, p.policy_version, p.ranking_policy_version, p.analysis_version,
       p.calibration_version,
       p.source_page_id, p.source_url, p.source_page_version,
       p.target_page_id, p.target_url, p.target_page_version,
       t.deployed_at, t.rolled_back_at,
       a.run_id       AS agent_run_id,
       a.verdict      AS agent_verdict,
       a.model        AS agent_model,
       ar.graph_version AS agent_graph_version,
       rv.actor       AS approved_by,
       rv.created_at  AS approved_at,
       d.deployment_id, d.cms_provider, d.verified_at
  FROM link_plans p
  LEFT JOIN (
        SELECT plan_id,
               min(occurred_at) FILTER (WHERE to_status = 'deployed')    AS deployed_at,
               min(occurred_at) FILTER (WHERE to_status = 'rolled_back') AS rolled_back_at
          FROM link_plan_transitions
         WHERE to_status IN ('deployed', 'rolled_back')
         GROUP BY plan_id
       ) t ON t.plan_id = p.id
  LEFT JOIN LATERAL (
        SELECT arp.run_id, arp.verdict, arp.model
          FROM agent_run_plans arp
         WHERE arp.plan_id = p.id
         ORDER BY arp.id DESC
         LIMIT 1
       ) a ON true
  LEFT JOIN agent_runs ar ON ar.id = a.run_id
  LEFT JOIN LATERAL (
        SELECT ra.actor, ra.created_at
          FROM review_actions ra
         WHERE ra.plan_id = p.id AND ra.action = :approve
         ORDER BY ra.id
         LIMIT 1
       ) rv ON true
  LEFT JOIN LATERAL (
        SELECT dep.id AS deployment_id, dep.cms_provider, dep.verified_at
          FROM deployment_items di
          JOIN deployments dep ON dep.id = di.deployment_id
         WHERE di.plan_id = p.id AND di.stage IN ('applied', 'already_applied')
         ORDER BY di.id
         LIMIT 1
       ) d ON true
 WHERE p.id = :plan_id
"""
"""One query for the whole chain. The live window joins ``link_plan_transitions``, never a
``deployment_items`` stage (amendment 3); the deployment join only names WHICH deployment
applied the link and what it reported."""


async def live_windows(
    session: AsyncSession, plan_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, LiveWindow]:
    """One :class:`~ken_contracts.analytics.LiveWindow` per requested plan id.

    Frozen signature (``protocol.LiveWindows``). An unknown or never-deployed plan gets
    ``deployed_at = None``, so a caller can treat "no window" and "never live" alike.
    """
    wanted = list(dict.fromkeys(plan_ids))
    windows = {
        plan_id: LiveWindow(plan_id=plan_id, deployed_at=None, rolled_back_at=None)
        for plan_id in wanted
    }
    if not wanted:
        return windows
    for row in await repo.live_transition_moments(session, wanted):
        windows[row.plan_id] = LiveWindow(
            plan_id=row.plan_id,
            deployed_at=row.deployed_at,
            rolled_back_at=row.rolled_back_at if row.deployed_at is not None else None,
        )
    return windows


def _reason_codes(raw: Any) -> tuple[ReasonCode, ...]:
    codes: list[ReasonCode] = []
    for item in raw or []:
        try:
            codes.append(ReasonCode(item))
        except ValueError:  # a code retired since the plan was written
            continue
    return tuple(codes)


def _optional[T](factory: Callable[[Any], T], value: Any) -> T | None:
    if value is None:
        return None
    try:
        return factory(value)
    except ValueError:
        return None
    except TypeError:  # pragma: no cover - the column is text or NULL
        return None


async def link_attribution(session: AsyncSession, *, plan_id: uuid.UUID) -> LinkAttribution:
    """The full chain of one plan.

    Raises:
        PlanNotFoundError: no such plan.
    """
    found = await session.execute(
        text(ATTRIBUTION_SQL), {"plan_id": plan_id, "approve": ReviewAction.APPROVE.value}
    )
    row = found.mappings().first()
    if row is None:
        raise PlanNotFoundError
    return LinkAttribution(
        plan_id=row["id"],
        plan_status=LinkPlanStatus(row["status"]),
        anchor_text=row["anchor_text"],
        anchor_source=row["anchor_source"],
        relationship_type=_optional(RelationshipType, row["relationship_type"]),
        reason_codes=_reason_codes(row["reason_codes"]),
        supersedes=row["supersedes"],
        recommendation_run_id=row["run_id"],
        engine_version=row["engine_version"],
        policy_version=row["policy_version"],
        ranking_policy_version=row["ranking_policy_version"],
        analysis_version=row["analysis_version"],
        calibration_version=row["calibration_version"],
        source_page_id=row["source_page_id"],
        source_url=row["source_url"],
        source_page_version=row["source_page_version"],
        target_page_id=row["target_page_id"],
        target_url=row["target_url"],
        target_page_version=row["target_page_version"],
        agent_run_id=row["agent_run_id"],
        agent_verdict=_optional(CriticVerdict, row["agent_verdict"]),
        agent_model=row["agent_model"],
        agent_graph_version=row["agent_graph_version"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        deployment_id=row["deployment_id"],
        cms_provider=row["cms_provider"],
        deployed_at=row["deployed_at"],
        verified_at=row["verified_at"],
        rolled_back_at=row["rolled_back_at"] if row["deployed_at"] is not None else None,
    )
