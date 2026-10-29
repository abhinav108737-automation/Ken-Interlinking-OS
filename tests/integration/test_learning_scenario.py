"""Replays ``evals/learning/lifecycle_scenario.json`` -- the golden lifecycle fixture -- against
the real services and pins the exact stage sequence and every refusal, in order (ADR-0016
section 11). A mismatch means a safety mechanism changed.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import text

from ken_contracts.learning import (
    AdvanceProposalRequest,
    PromotionRefusedError,
    ProposalStatus,
)
from ken_services.learning.lifecycle import advance_system_stage
from ken_services.learning.promote import DatabasePromotionService
from tests.integration.learning_lifecycle_support import (
    NOW,
    SM,
    insert_evaluation,
    insert_shadow_rows,
    learning_policy,
    operator,
    reviewer,
    seed_proposal,
    settings,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

FIXTURE: Final = (
    Path(__file__).resolve().parents[2] / "evals" / "learning" / "lifecycle_scenario.json"
)


async def _status(sm: SM, proposal_id: uuid.UUID) -> str:
    async with sm() as session:
        return str(
            await session.scalar(
                text("SELECT status FROM learning_proposals WHERE id = :id"), {"id": proposal_id}
            )
        )


async def test_the_lifecycle_scenario_matches_its_golden_fixture(sessionmaker: SM) -> None:
    scenario: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seeded = await seed_proposal(sessionmaker)
    people = {
        "operator_admin": await operator(sessionmaker),
        "reviewer": await reviewer(sessionmaker),
    }
    latest: uuid.UUID | None = None
    refusals: list[str] = []
    promotions_in_demonstration = 0

    for step in scenario["steps"]:
        if "shadow_rows" in step:
            await insert_shadow_rows(sessionmaker, seeded, **step["shadow_rows"])
        if "evaluation" in step:
            spec = step["evaluation"]
            latest = await insert_evaluation(
                sessionmaker,
                proposal_id=seeded.proposal_id,
                fingerprint=seeded.fingerprint if spec["fingerprint"] == "current" else "d" * 64,
                gate=spec["gate"],
                age=timedelta(days=spec.get("age_days", 0), hours=1),
            )
        target = ProposalStatus(step["request"]["to_status"])
        expect = step["expect"]
        refused: str | None = None
        try:
            if step["actor"] == "system":
                await advance_system_stage(
                    sessionmaker, proposal_id=seeded.proposal_id, to_status=target
                )
            else:
                service = DatabasePromotionService(
                    sessionmaker=sessionmaker,
                    settings=settings(learning_demonstration_mode=step["demonstration_mode"]),
                    policy=learning_policy(),
                    clock=lambda: NOW,
                )
                await service.advance(
                    principal=people[step["actor"]],
                    proposal_id=seeded.proposal_id,
                    request=AdvanceProposalRequest(
                        operation_id=str(uuid.uuid4()),
                        to_status=target,
                        evaluation_run_id=(
                            latest if step["request"].get("evaluation") else uuid.uuid4()
                        )
                        if target is ProposalStatus.PROMOTED
                        else None,
                    ),
                )
        except PromotionRefusedError as error:
            refused = error.refusal.value
        status = await _status(sessionmaker, seeded.proposal_id)
        if "refused" in expect:
            assert refused == expect["refused"], step["id"]
            assert status == expect["status_after"], step["id"]
            refusals.append(refused)
        else:
            assert refused is None, (step["id"], refused)
            assert status == expect["advanced_to"], step["id"]
        if step.get("demonstration_mode") is True and status == "promoted":
            promotions_in_demonstration += 1

    assert refusals == scenario["refusals"]
    assert promotions_in_demonstration == scenario["promotions_with_demonstration_mode_on"]
    async with sessionmaker() as session:
        sequence = [
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT to_status FROM learning_proposal_transitions "
                        "WHERE proposal_id = :id ORDER BY id"
                    ),
                    {"id": seeded.proposal_id},
                )
            ).all()
        ]
        events = await session.scalar(
            text("SELECT count(*) FROM events WHERE event_name = 'policy.promoted'")
        )
    assert ["proposed", *sequence] == scenario["stage_sequence"]
    assert events == 1
