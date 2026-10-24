"""The ``autonomous`` audit tag on deployments (ADR-0018).

``deployments.actor`` is the operator account an autonomous publish runs as, so the reader
derives the tag from ``publish_attempts`` (actor ``system:autonomy-engine``, ``deployment_id``
set) joined to the sanctioning ``autonomy_decisions`` row. RULE: the tag says how the
deployment was PUBLISHED. A later rollback of it (by a person or by the engine) never changes
that; ``autonomy_rolled_back`` is true only when the engine itself rolled it back, and a human
deployment is never autonomous, whatever its note says.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.autonomy import AutonomyDecision, AutonomyMode
from ken_contracts.deployment import DeploymentStatus, RollbackReason, RollbackRequest
from ken_services.deployment.reader import SqlDeploymentReader
from tests.integration.autonomy_support import Autonomy, FailingVerifier, autonomy
from tests.integration.deployment_support import deploy_request
from tests.integration.recommendation_support import CHARGING, EV, SM, rows

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


def _auto(a: Autonomy, **overrides: Any) -> Autonomy:
    return a.with_mode("autonomous", autonomy_autonomous_acknowledged=True, **overrides)


def _reader(sessionmaker: SM) -> SqlDeploymentReader:
    return SqlDeploymentReader(sessionmaker=sessionmaker)


async def _human_deployment(a: Autonomy, **kw: Any) -> uuid.UUID:
    human_plan = next(p for (s, _t), p in a.world.plans.items() if s != EV)
    [plan_id] = await a.world.approve(human_plan)
    detail = await a.world.service().deploy(
        request=deploy_request([plan_id], **kw), principal=a.world.operator
    )
    return detail.id


async def _autonomous_deployment(a: Autonomy, **kw: Any) -> uuid.UUID:
    result = await _auto(a).publisher(**kw).publish_plan(plan_id=a.world.plans[(EV, CHARGING)].id)
    assert result.deployment_id is not None
    return result.deployment_id


async def test_an_autonomous_deployment_is_tagged_in_detail_and_list(
    sessionmaker: SM, a: Autonomy
) -> None:
    deployment_id = await _autonomous_deployment(a)
    reader = _reader(sessionmaker)

    detail = await reader.get_deployment(deployment_id=deployment_id)
    assert detail.autonomous is True
    assert detail.autonomy_mode is AutonomyMode.AUTONOMOUS
    assert detail.autonomy_decision in {
        AutonomyDecision.AUTO_PUBLISH,
        AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
    }
    assert detail.autonomy_rolled_back is False
    # The decision id is the autonomy_decisions row that carries exactly that decision.
    found = await rows(
        sessionmaker,
        "SELECT id, decision FROM autonomy_decisions WHERE id = :i",
        i=detail.autonomy_decision_id,
    )
    assert len(found) == 1 and found[0].decision == detail.autonomy_decision.value
    assert detail.actor == a.world.operator.actor  # the actor alone could not tell

    listed = await reader.list_deployments(limit=10, offset=0)
    assert [d.autonomous for d in listed.items] == [True]
    for_plan = await reader.deployments_for_plan(plan_id=detail.plan_ids[0])
    assert [d.autonomous for d in for_plan] == [True]


async def test_a_human_deployment_is_not_tagged(sessionmaker: SM, a: Autonomy) -> None:
    deployment_id = await _human_deployment(a)
    detail = await _reader(sessionmaker).get_deployment(deployment_id=deployment_id)
    assert detail.autonomous is False
    assert detail.autonomy_decision_id is None
    assert detail.autonomy_decision is None
    assert detail.autonomy_mode is None
    assert detail.autonomy_rolled_back is False


async def test_a_human_cannot_forge_the_tag_with_a_note(sessionmaker: SM, a: Autonomy) -> None:
    """The note is free text a person types; the tag ignores it."""
    deployment_id = await _human_deployment(a, note="autonomous publish (auto_publish) decision#1")
    detail = await _reader(sessionmaker).get_deployment(deployment_id=deployment_id)
    assert detail.autonomous is False and detail.autonomy_decision_id is None
    notes = await rows(sessionmaker, "SELECT note FROM deployments WHERE id = :d", d=deployment_id)
    assert notes[0].note.startswith("autonomous publish")


async def test_a_human_rollback_does_not_make_a_human_deployment_autonomous_or_vice_versa(
    sessionmaker: SM, a: Autonomy
) -> None:
    """RULE: the tag is the publish origin. A person rolling back an autonomous deployment
    leaves it autonomous (it WAS published by the engine) but NOT ``autonomy_rolled_back``,
    and rolling back a human deployment never makes it autonomous."""
    auto_id = await _autonomous_deployment(a)
    human_id = await _human_deployment(a)
    for deployment_id in (auto_id, human_id):
        after = await a.world.service().rollback(
            deployment_id=deployment_id,
            request=RollbackRequest(
                operation_id=uuid.uuid4(), reason=RollbackReason.EDITORIAL_REVERSAL
            ),
            principal=a.world.operator,
        )
        assert after.status is DeploymentStatus.ROLLED_BACK

    reader = _reader(sessionmaker)
    auto = await reader.get_deployment(deployment_id=auto_id)
    human = await reader.get_deployment(deployment_id=human_id)
    assert auto.status is DeploymentStatus.ROLLED_BACK
    assert auto.autonomous is True and auto.autonomy_rolled_back is False
    assert human.status is DeploymentStatus.ROLLED_BACK
    assert human.autonomous is False and human.autonomy_rolled_back is False


async def test_an_automatic_rollback_by_the_engine_is_marked(sessionmaker: SM, a: Autonomy) -> None:
    deployment_id = await _autonomous_deployment(a, verifier=FailingVerifier())
    detail = await _reader(sessionmaker).get_deployment(deployment_id=deployment_id)
    assert detail.status is DeploymentStatus.ROLLED_BACK
    assert detail.autonomous is True and detail.autonomy_rolled_back is True
