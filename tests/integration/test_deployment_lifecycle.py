"""After the apply (ADR-0014 s8): crash consistency and reconciliation, verification outcomes
(passed / failed / inconclusive; re-verify), rollback (whole, per plan, after unrelated later
edits, conflicts, idempotency, crash), VALIDATED ownership against the engine and the review
service, pair occupancy, and the READ ONLY reader.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ken_contracts.cms import LinkChangeRequest
from ken_contracts.deployment import (
    CMSUnavailableDeploymentError,
    DeploymentIssueCode,
    DeploymentNotFoundError,
    DeploymentRefusedError,
    DeploymentStatus,
    LinkFailedPayload,
    LinkRolledBackPayload,
    RollbackReason,
    VerificationCheckName,
    VerificationOutcome,
)
from ken_contracts.events import EventName
from ken_contracts.intelligence import ProtectionSource
from ken_contracts.recommendations import LinkPlanStatus, PlanNotFoundError
from ken_contracts.review import FeedbackReason, PlanNotReviewableError, RejectRequest
from ken_core.db.repositories.intelligence import insert_protection
from ken_core.db.session import create_readonly_sessionmaker
from ken_services.deployment.errors import OperationReusedDeploymentError
from ken_services.deployment.http_probe import FetchOutcome, FetchResult
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.reader import SqlDeploymentReader
from ken_services.deployment.service import DatabaseDeploymentService, deploy_lock_key
from ken_services.deployment.verification import HtmlLinkVerifier
from tests.integration.deployment_support import (
    ScriptedProbe,
    World,
    count,
    deploy_request,
    events,
    plan_status,
    rollback_request,
    statuses,
    world,
)
from tests.integration.recommendation_support import (
    CHARGING,
    EV,
    EV_MARKET_PARAS,
    LI,
    SETTINGS,
    SITE,
    SM,
    _p,
    analyse,
    import_records,
    recommend,
    record,
    rows,
)
from tests.integration.review_support import review_service

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
D = DeploymentStatus
C = DeploymentIssueCode


@pytest.fixture
async def w(sessionmaker: SM, tmp_path: Path) -> World:
    return await world(sessionmaker, tmp_path)


async def _approved(w: World) -> list[uuid.UUID]:
    return await w.approve(w.plans[(EV, CHARGING)], w.plans[(EV, LI)])


async def _deployed(w: World, **service: Any) -> tuple[Any, list[uuid.UUID]]:
    ids = await _approved(w)
    detail = await w.service(**service).deploy(request=deploy_request(ids), principal=w.operator)
    return detail, ids


async def _body(w: World, path: str = EV) -> str:
    return (await w.mock.get_page(page_id=w.page_ids()[SITE + path])).body_html


async def _deployment_status(sessionmaker: SM, deployment_id: uuid.UUID) -> D:
    found = await rows(
        sessionmaker, "SELECT status FROM deployments WHERE id = :d", d=deployment_id
    )
    return D(found[0].status)


def _probe(w: World, **kw: Any) -> ScriptedProbe:
    return ScriptedProbe(cms=w.cms, page_ids=w.page_ids(), **kw)


def _codes(error: Any) -> set[tuple[str, Any]]:
    return {(DeploymentIssueCode(i.code).value, i.plan_id) for i in error.issues}


# --------------------------------------------------------------------------- #
# Crash consistency and reconciliation
# --------------------------------------------------------------------------- #
async def test_cms_timeout_before_the_cms_leaves_applying_then_reconcile_applies(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    original = await _body(w)
    w.cms.fail_before = 1  # tx1 committed; the CMS call timed out and applied nothing
    request = deploy_request(ids)
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=request, principal=w.operator)
    found = await rows(sessionmaker, "SELECT id, status, attempts FROM deployments")
    assert [(r.status, r.attempts) for r in found] == [("applying", 2)]
    assert await statuses(sessionmaker, ids) == [S.VALIDATED, S.VALIDATED]
    assert await _body(w) == original

    summary = await w.service().reconcile(older_than_s=0)

    assert (summary.examined, summary.applied) == (1, 1)
    assert await _deployment_status(sessionmaker, found[0].id) is D.VERIFIED
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]
    # The STORED request was resent byte for byte.
    assert w.cms.applied[0].model_dump_json() == w.cms.applied[1].model_dump_json()


async def test_cms_committed_but_the_answer_lost_then_reconcile_replays(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    w.cms.fail_after = 1  # the CMS applied; the app never learned it
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert [r.status for r in await rows(sessionmaker, "SELECT status FROM deployments")] == [
        "applying"
    ]
    body = await _body(w)
    assert all(body.count(f'data-ken-link-id="{p}"') == 1 for p in ids)  # applied ONCE

    summary = await w.service().reconcile(older_than_s=0)

    assert summary.applied == 1
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]
    assert await _body(w) == body  # the replay applied nothing twice
    replays = [e for e in w.mock.audit_log if e.outcome == "idempotent_replay"]
    assert len(replays) == 1


async def test_app_transaction_2_failing_after_the_cms_applied_is_reconciled(
    sessionmaker: SM, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _approved(w)
    service = w.service()
    original = service._record_applied

    async def _crash(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("the app process died before transaction 2")

    monkeypatch.setattr(service, "_record_applied", _crash)
    with pytest.raises(RuntimeError):
        await service.deploy(request=deploy_request(ids), principal=w.operator)
    monkeypatch.setattr(service, "_record_applied", original)
    assert await statuses(sessionmaker, ids) == [S.VALIDATED, S.VALIDATED]
    assert [r.status for r in await rows(sessionmaker, "SELECT status FROM deployments")] == [
        "applying"
    ]
    # The source lock was released even though the deploy crashed.
    summary = await w.service().reconcile(older_than_s=0)
    assert summary.applied == 1
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]


async def test_a_client_retry_of_an_applying_operation_resends_the_stored_request(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    request = deploy_request(ids)
    w.cms.fail_after = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=request, principal=w.operator)
    detail = await w.service().deploy(request=request, principal=w.operator)
    assert detail.idempotent_replay and detail.status is D.VERIFIED
    assert len(w.cms.applied) == 2
    assert w.cms.applied[0] == w.cms.applied[1]


async def test_reconcile_replays_even_after_the_page_moved(sessionmaker: SM, w: World) -> None:
    """Provider contract: replaying an applied operation returns the stored result even if
    the page has moved since (an unrelated later edit between the CMS commit and the
    reconciliation)."""
    ids = await _approved(w)
    w.cms.fail_after = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    page_id = w.page_ids()[SITE + EV]
    edited = (await _body(w)).replace("</main>", "<p>Later editorial addition.</p></main>")
    w.mock.edit_page(page_id=page_id, body_html=edited)
    summary = await w.service().reconcile(older_than_s=0)
    assert summary.applied == 1
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]


async def test_reconcile_skips_young_rows_and_rows_whose_source_is_locked(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    w.cms.fail_before = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert (await w.service().reconcile(older_than_s=3600)).examined == 0
    source = w.plans[(EV, LI)].source_page_id
    async with sessionmaker() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": deploy_lock_key(source)})
        summary = await w.service().reconcile(older_than_s=0)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": deploy_lock_key(source)})
        await holder.commit()
    assert (summary.examined, summary.skipped_locked, summary.applied) == (1, 1, 0)
    assert await statuses(sessionmaker, ids) == [S.VALIDATED, S.VALIDATED]


async def test_reconcile_of_a_conflicting_stored_request_expires(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    w.cms.fail_before = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    page_id = w.page_ids()[SITE + EV]
    edited = (await _body(w)).replace("</main>", "<p>An editor moved the page.</p></main>")
    w.mock.edit_page(page_id=page_id, body_html=edited)  # nothing was applied: a real conflict
    summary = await w.service().reconcile(older_than_s=0)
    assert summary.conflict == 1
    assert await statuses(sessionmaker, ids) == [S.EXPIRED, S.EXPIRED]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
async def test_http_verification_checks_the_published_page_and_targets(
    sessionmaker: SM, w: World
) -> None:
    probe = _probe(w)
    detail, _ids = await _deployed(w, probe=probe)
    assert detail.status is D.VERIFIED
    run = detail.verifications[-1]
    assert run.http_checked and run.passed
    reach = [c for c in run.checks if c.check is VerificationCheckName.TARGET_REACHABLE]
    assert {c.outcome for c in reach} == {VerificationOutcome.PASSED}
    assert probe.base + EV in probe.calls  # the published source page was fetched


async def test_a_broken_target_is_refused_at_deploy(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    probe = _probe(w)
    probe.targets[probe.base + LI] = FetchResult(FetchOutcome.FAILED, status=404, detail="http_404")
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service(probe=probe).deploy(request=deploy_request(ids), principal=w.operator)
    assert (C.BROKEN_TARGET.value, w.plans[(EV, LI)].id) in _codes(info.value)
    # A redirecting target is a failure too (no off-host redirect is ever followed).
    probe.targets[probe.base + LI] = FetchResult(FetchOutcome.FAILED, status=301, detail="redirect")
    with pytest.raises(DeploymentRefusedError):
        await w.service(probe=probe).deploy(request=deploy_request(ids), principal=w.operator)
    assert await count(sessionmaker, "deployments") == 0


async def test_a_deterministic_verification_failure_fails_the_plan(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    probe = _probe(w)
    real = probe.fetch

    async def _fetch(url: str) -> FetchResult:
        # Reachable at deploy time; gone (404) when verification runs after the apply.
        if url == probe.base + LI and w.cms.applied:
            return FetchResult(FetchOutcome.FAILED, status=404, detail="http_404")
        return await real(url)

    probe.fetch = _fetch  # type: ignore[method-assign]
    detail = await w.service(probe=probe).deploy(request=deploy_request(ids), principal=w.operator)
    assert detail.status is D.VERIFICATION_FAILED
    assert await plan_status(sessionmaker, li.id) is S.FAILED
    assert await plan_status(sessionmaker, w.plans[(EV, CHARGING)].id) is S.VERIFIED
    failed = [
        LinkFailedPayload.model_validate(e.payload)
        for e in await events(sessionmaker, "link.failed")
    ]
    assert [(f.plan_id, f.stage, f.codes) for f in failed] == [
        (li.id, "verify", [VerificationCheckName.TARGET_REACHABLE.value])
    ]


async def test_inconclusive_verification_never_fails_a_plan(sessionmaker: SM, w: World) -> None:
    probe = _probe(w)
    # The PUBLISHED source page times out (targets answer 200, so the deploy may proceed).
    probe.targets[probe.base + EV] = FetchResult(FetchOutcome.INCONCLUSIVE, detail="timeout")
    detail, ids = await _deployed(w, probe=probe, deploy_verify_attempts=3)
    assert detail.status is D.APPLIED
    assert len(detail.verifications) == 3  # the bounded retry at deploy time
    assert all(r.inconclusive and not r.passed for r in detail.verifications)
    assert await statuses(sessionmaker, ids) == [S.DEPLOYED, S.DEPLOYED]
    assert await events(sessionmaker, "link.failed") == []
    # The CDN catches up: verify on demand.
    del probe.targets[probe.base + EV]
    again = await w.service(probe=probe).verify(deployment_id=detail.id, principal=w.operator)
    assert again.status is D.VERIFIED and again.verifications[-1].passed
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]


async def test_publish_lag_is_inconclusive(sessionmaker: SM, w: World) -> None:
    probe = _probe(w, lag=True)
    probe._lagged[w.page_ids()[SITE + EV]] = await _body(w)  # the CDN still serves the old page
    detail, ids = await _deployed(w, probe=probe, deploy_verify_attempts=1)
    assert detail.status is D.APPLIED
    run = detail.verifications[-1]
    assert run.inconclusive
    assert {c.detail for c in run.checks if c.outcome is VerificationOutcome.INCONCLUSIVE} == {
        "publish_lag"
    }
    assert await statuses(sessionmaker, ids) == [S.DEPLOYED, S.DEPLOYED]


async def test_reverifying_a_verified_deployment_makes_no_plan_transition(
    sessionmaker: SM, w: World
) -> None:
    detail, ids = await _deployed(w)
    page_id = w.page_ids()[SITE + EV]
    # An editor removes our link text's paragraph entirely.
    body = await _body(w)
    start = body.index("<p>Vehicle makers")
    w.mock.edit_page(
        page_id=page_id, body_html=body[:start] + body[body.index("</p>", start) + 4 :]
    )
    transitions = await count(sessionmaker, "link_plan_transitions")

    again = await w.service().verify(deployment_id=detail.id, principal=w.operator)

    assert again.status is D.VERIFICATION_FAILED and not again.verifications[-1].passed
    assert len(again.verifications) == 2
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]  # never verified->failed
    assert await count(sessionmaker, "link_plan_transitions") == transitions
    # The editor restores it: re-verify moves the DEPLOYMENT back to verified.
    w.mock.edit_page(page_id=page_id, body_html=body)
    assert (await w.service().verify(deployment_id=detail.id, principal=w.operator)).status is (
        D.VERIFIED
    )


async def test_verify_refuses_unfinished_or_unapplied_deployments(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    w.cms.fail_before = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    applying = (await rows(sessionmaker, "SELECT id FROM deployments"))[0].id
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().verify(deployment_id=applying, principal=w.operator)
    assert _codes(info.value) == {(C.DEPLOYMENT_IN_PROGRESS.value, None)}
    with pytest.raises(DeploymentNotFoundError):
        await w.service().verify(deployment_id=uuid.uuid4(), principal=w.operator)


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
async def test_rollback_restores_the_page_and_is_idempotent(sessionmaker: SM, w: World) -> None:
    original = await _body(w)
    detail, ids = await _deployed(w)
    request = rollback_request(reason=RollbackReason.WRONG_DESTINATION, note="oops")

    rolled = await w.service().rollback(
        deployment_id=detail.id, request=request, principal=w.operator
    )

    assert rolled.status is D.ROLLED_BACK and rolled.rolled_back_at is not None
    assert rolled.rollback_reason is RollbackReason.WRONG_DESTINATION
    assert rolled.rollback_note == "oops"
    assert await _body(w) == original
    assert await statuses(sessionmaker, ids) == [S.ROLLED_BACK, S.ROLLED_BACK]
    found = await events(sessionmaker, EventName.LINK_ROLLED_BACK.value)
    assert len(found) == 2
    for event in found:
        payload = LinkRolledBackPayload.model_validate(event.payload)
        assert payload.reason is RollbackReason.WRONG_DESTINATION and payload.actor == "user:otto"
        assert event.correlation_id == request.operation_id
        assert payload.restored_version == rolled.new_version or payload.restored_version
    # Replays and second rollbacks change nothing.
    before = await count(sessionmaker, "link_plan_transitions")
    replay = await w.service().rollback(
        deployment_id=detail.id, request=request, principal=w.operator
    )
    assert replay.idempotent_replay and replay.status is D.ROLLED_BACK
    second = await w.service().rollback(
        deployment_id=detail.id, request=rollback_request(), principal=w.operator
    )
    assert second.status is D.ROLLED_BACK
    assert await count(sessionmaker, "link_plan_transitions") == before
    assert await _body(w) == original
    with pytest.raises(OperationReusedDeploymentError):
        await w.service().rollback(
            deployment_id=detail.id,
            request=rollback_request(op=request.operation_id, note="another"),
            principal=w.operator,
        )


async def test_rollback_after_an_unrelated_later_edit_keeps_the_edit(
    sessionmaker: SM, w: World
) -> None:
    detail, ids = await _deployed(w)
    page_id = w.page_ids()[SITE + EV]
    edited = (await _body(w)).replace("</main>", "<p>An unrelated editorial update.</p></main>")
    w.mock.edit_page(page_id=page_id, body_html=edited)

    rolled = await w.service().rollback(
        deployment_id=detail.id, request=rollback_request(), principal=w.operator
    )

    body = await _body(w)
    assert rolled.status is D.ROLLED_BACK
    assert "An unrelated editorial update." in body and "data-ken-link-id" not in body
    assert await statuses(sessionmaker, ids) == [S.ROLLED_BACK, S.ROLLED_BACK]


async def test_rollback_after_our_own_later_deployment_keeps_it(sessionmaker: SM, w: World) -> None:
    charging, li = w.plans[(EV, CHARGING)], w.plans[(EV, LI)]
    await w.approve(charging, li)
    first = await w.service().deploy(request=deploy_request([charging.id]), principal=w.operator)
    second = await w.service().deploy(request=deploy_request([li.id]), principal=w.operator)
    assert first.status is second.status is D.VERIFIED

    await w.service().rollback(
        deployment_id=first.id, request=rollback_request(), principal=w.operator
    )

    body = await _body(w)
    assert f'data-ken-link-id="{charging.id}"' not in body
    assert f'data-ken-link-id="{li.id}"' in body
    assert await statuses(sessionmaker, [charging.id, li.id]) == [S.ROLLED_BACK, S.VERIFIED]


async def test_a_changed_link_is_a_rollback_conflict(sessionmaker: SM, w: World) -> None:
    detail, ids = await _deployed(w)
    page_id = w.page_ids()[SITE + EV]
    body = await _body(w)
    w.mock.edit_page(page_id=page_id, body_html=body.replace(SITE + LI, SITE + "/elsewhere"))
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().rollback(
            deployment_id=detail.id, request=rollback_request(), principal=w.operator
        )
    assert {code for code, _plan in _codes(info.value)} == {C.ROLLBACK_CONFLICT.value}
    assert await _deployment_status(sessionmaker, detail.id) is D.VERIFIED
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]
    assert await events(sessionmaker, EventName.LINK_ROLLED_BACK.value) == []


class _ThreadRecorder:
    """Wraps the real mutator and verifier, recording the thread of every call."""

    def __init__(self) -> None:
        self.mutator = HtmlMutator()
        self.verifier = HtmlLinkVerifier()
        self.threads: list[tuple[str, int]] = []

    def insert_links(self, *args: Any, **kwargs: Any) -> Any:
        self.threads.append(("insert", threading.get_ident()))
        return self.mutator.insert_links(*args, **kwargs)

    def remove_links(self, *args: Any, **kwargs: Any) -> Any:
        self.threads.append(("remove", threading.get_ident()))
        return self.mutator.remove_links(*args, **kwargs)

    def verify_links(self, *args: Any, **kwargs: Any) -> Any:
        self.threads.append(("verify", threading.get_ident()))
        return self.verifier.verify_links(*args, **kwargs)


async def test_the_mutator_and_verifier_run_off_the_event_loop(sessionmaker: SM, w: World) -> None:
    """QA L4: the synchronous HTML work never blocks the API worker's event loop."""
    ids = await _approved(w)
    recorder = _ThreadRecorder()
    service = DatabaseDeploymentService(
        sessionmaker=w.sessionmaker,
        settings=SETTINGS.model_copy(update={"deploy_verify_retry_delay_s": 0.0}),
        cms=w.cms,
        mutator=recorder,
        verifier=recorder,
    )
    detail = await service.deploy(request=deploy_request(ids), principal=w.operator)
    assert detail.status is D.VERIFIED
    kinds = {kind for kind, _thread in recorder.threads}
    assert {"insert", "verify"} <= kinds
    assert threading.get_ident() not in {thread for _kind, thread in recorder.threads}


async def test_a_refused_rollback_replays_its_refusal(sessionmaker: SM, w: World) -> None:
    """QA L1: a replay of a REFUSED rollback operation returns the same refusal (as a deploy
    replay returns its stored outcome) -- even after the page is fixed -- instead of running
    again; a NEW operation id rolls back normally."""
    detail, ids = await _deployed(w)
    page_id = w.page_ids()[SITE + EV]
    body = await _body(w)
    w.mock.edit_page(page_id=page_id, body_html=body.replace(SITE + LI, SITE + "/elsewhere"))
    request = rollback_request()
    with pytest.raises(DeploymentRefusedError) as first:
        await w.service().rollback(deployment_id=detail.id, request=request, principal=w.operator)
    refusal = _codes(first.value)
    assert {code for code, _plan in refusal} == {C.ROLLBACK_CONFLICT.value}
    recorded = await rows(
        sessionmaker,
        "SELECT stage, codes FROM deployment_items WHERE operation_id = :o",
        o=request.operation_id,
    )
    assert recorded and all(r.stage == "rollback_conflict" for r in recorded)
    assert all(r.codes == [C.ROLLBACK_CONFLICT.value] for r in recorded)
    slot = await rows(
        sessionmaker, "SELECT rollback_operation_id FROM deployments WHERE id = :d", d=detail.id
    )
    assert slot[0].rollback_operation_id is None  # freed for a later, different rollback

    w.mock.edit_page(page_id=page_id, body_html=body)  # an editor restores our link
    transitions = await count(sessionmaker, "link_plan_transitions")
    with pytest.raises(DeploymentRefusedError) as replay:
        await w.service().rollback(deployment_id=detail.id, request=request, principal=w.operator)
    assert _codes(replay.value) == refusal
    assert await count(sessionmaker, "link_plan_transitions") == transitions
    assert await _body(w) == body
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]
    with pytest.raises(OperationReusedDeploymentError):  # other plans under the same id
        await w.service().rollback(
            deployment_id=detail.id,
            request=rollback_request(op=request.operation_id, plan_ids=[ids[0]]),
            principal=w.operator,
        )
    with pytest.raises(OperationReusedDeploymentError):  # never a deploy id either
        await w.service().deploy(
            request=deploy_request(ids, op=request.operation_id), principal=w.operator
        )

    rolled = await w.service().rollback(
        deployment_id=detail.id, request=rollback_request(), principal=w.operator
    )
    assert rolled.status is D.ROLLED_BACK and not rolled.idempotent_replay
    assert await statuses(sessionmaker, ids) == [S.ROLLED_BACK, S.ROLLED_BACK]


async def test_nothing_to_roll_back_for_a_failed_or_conflicting_deployment(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    page = await w.mock.get_page(page_id=w.page_ids()[SITE + EV])

    async def _protect(_request: LinkChangeRequest) -> None:
        w.mock._pages[page.page_id] = page.model_copy(update={"protected": True})

    w.cms.before_apply = _protect
    failed = await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert failed.status is D.FAILED
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().rollback(
            deployment_id=failed.id, request=rollback_request(), principal=w.operator
        )
    assert _codes(info.value) == {(C.NOTHING_TO_ROLL_BACK.value, None)}
    assert await _deployment_status(sessionmaker, failed.id) is D.FAILED


async def test_per_plan_rollback_then_the_rest(sessionmaker: SM, w: World) -> None:
    original = await _body(w)
    detail, ids = await _deployed(w)
    charging, li = ids

    one = await w.service().rollback(
        deployment_id=detail.id,
        request=rollback_request(plan_ids=[li], reason=RollbackReason.EDITORIAL_REVERSAL),
        principal=w.operator,
    )

    assert one.status is D.VERIFIED  # links remain live: back to the prior status
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.ROLLED_BACK]
    body = await _body(w)
    assert f'data-ken-link-id="{li}"' not in body and f'data-ken-link-id="{charging}"' in body
    # The REMOVE request is stored for reconciliation.
    stored = await rows(sessionmaker, "SELECT rollback_request_json FROM deployments")
    remove = LinkChangeRequest.model_validate_json(stored[0].rollback_request_json)
    assert [(c.change_id, c.operation.value) for c in remove.changes] == [(li, "remove")]
    # A plan outside the deployment is refused.
    with pytest.raises(DeploymentRefusedError):
        await w.service().rollback(
            deployment_id=detail.id,
            request=rollback_request(plan_ids=[uuid.uuid4()]),
            principal=w.operator,
        )

    rest = await w.service().rollback(
        deployment_id=detail.id, request=rollback_request(), principal=w.operator
    )
    assert rest.status is D.ROLLED_BACK
    assert await statuses(sessionmaker, ids) == [S.ROLLED_BACK, S.ROLLED_BACK]
    assert await _body(w) == original


async def test_a_rollback_whose_answer_is_lost_is_reconciled(sessionmaker: SM, w: World) -> None:
    original = await _body(w)
    detail, ids = await _deployed(w)
    w.cms.rollback_fail_after = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().rollback(
            deployment_id=detail.id, request=rollback_request(), principal=w.operator
        )
    assert await _deployment_status(sessionmaker, detail.id) is D.ROLLING_BACK
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]
    with pytest.raises(DeploymentRefusedError):  # verify waits for the rollback
        await w.service().verify(deployment_id=detail.id, principal=w.operator)

    summary = await w.service().reconcile(older_than_s=0)

    assert summary.rolled_back == 1
    assert await _deployment_status(sessionmaker, detail.id) is D.ROLLED_BACK
    assert await statuses(sessionmaker, ids) == [S.ROLLED_BACK, S.ROLLED_BACK]
    assert await _body(w) == original


async def test_only_an_operator_may_roll_back(w: World) -> None:
    detail, _ids = await _deployed(w)
    from ken_services.deployment.errors import DeploymentPermissionError

    with pytest.raises(DeploymentPermissionError):
        await w.service().rollback(
            deployment_id=detail.id, request=rollback_request(), principal=w.reviewer
        )


# --------------------------------------------------------------------------- #
# VALIDATED belongs to the deployment service
# --------------------------------------------------------------------------- #
async def test_the_engine_and_review_skip_validated_plans_while_a_deploy_is_in_flight(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _slow(_request: LinkChangeRequest) -> None:
        entered.set()
        await gate.wait()

    w.cms.before_apply = _slow
    deploy = asyncio.create_task(
        w.service().deploy(request=deploy_request(ids), principal=w.operator)
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert await statuses(sessionmaker, ids) == [S.VALIDATED, S.VALIDATED]

    # The target becomes protected: the engine would expire an OPEN plan as invalid...
    async with sessionmaker() as session, session.begin():
        await insert_protection(
            session,
            page_id=li.target_page_id,
            source=ProtectionSource.MANUAL.value,
            rule_id=None,
            reason="legal hold",
            created_by="user:test",
            created_at=datetime.now(UTC),
        )
    detail = await recommend(sessionmaker, source_page_ids=[li.source_page_id])
    assert detail.counters["errors"] == 0
    assert await plan_status(sessionmaker, li.id) is S.VALIDATED  # ...but not a VALIDATED one
    # ...and the review service refuses to touch it.
    with pytest.raises(PlanNotReviewableError):
        await review_service(sessionmaker).reject(
            plan_id=li.id,
            request=RejectRequest(
                operation_id=uuid.uuid4(),
                expected_status=S.VALIDATED,
                reasons=[FeedbackReason.IRRELEVANT_DESTINATION],
            ),
            principal=w.reviewer,
        )

    gate.set()
    finished = await deploy
    assert finished.status is D.VERIFIED
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]


# --------------------------------------------------------------------------- #
# Pair occupancy (ADR-0014 s8, amends ADR-0011)
# --------------------------------------------------------------------------- #
async def _pair_plans(sessionmaker: SM, source: uuid.UUID, target: uuid.UUID) -> list[Any]:
    return await rows(
        sessionmaker,
        "SELECT id, status FROM link_plans WHERE source_page_id = :s AND target_page_id = :t "
        "ORDER BY created_at",
        s=source,
        t=target,
    )


async def test_a_deployed_link_occupies_its_pair_regardless_of_the_source_version(
    sessionmaker: SM, w: World, tmp_path: Path
) -> None:
    detail, _ids = await _deployed(w)
    li = w.plans[(EV, LI)]
    # The source is re-read WITHOUT our link (a new version; e.g. a stale export or CDN lag).
    paragraphs = [*EV_MARKET_PARAS, _p("A new closing paragraph about something else entirely.")]
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2.jsonl"
    )
    assert (await analyse(sessionmaker)).counters["errors"] == 0
    run = await recommend(sessionmaker, source_page_ids=[li.source_page_id])
    assert run.counters["errors"] == 0
    assert run.counters["rejected_duplicate_recommendation"] >= 2
    assert [
        p.status for p in await _pair_plans(sessionmaker, li.source_page_id, li.target_page_id)
    ] == ["verified"]

    # Rolled back: the pair is free again, and the engine proposes it anew.
    await w.service().rollback(
        deployment_id=detail.id, request=rollback_request(), principal=w.operator
    )
    await recommend(sessionmaker, source_page_ids=[li.source_page_id])
    found = await _pair_plans(sessionmaker, li.source_page_id, li.target_page_id)
    assert [p.status for p in found] == ["rolled_back", "proposed"]


async def test_a_plan_failed_after_an_applied_deployment_occupies_its_pair(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    probe = _probe(w)
    real = probe.fetch

    async def _fetch(url: str) -> FetchResult:
        if url == probe.base + LI and w.cms.applied:
            return FetchResult(FetchOutcome.FAILED, status=404, detail="http_404")
        return await real(url)

    probe.fetch = _fetch  # type: ignore[method-assign]
    await w.service(probe=probe).deploy(request=deploy_request(ids), principal=w.operator)
    assert await plan_status(sessionmaker, li.id) is S.FAILED
    await recommend(sessionmaker, source_page_ids=[li.source_page_id])
    assert [
        p.status for p in await _pair_plans(sessionmaker, li.source_page_id, li.target_page_id)
    ] == ["failed"]


async def test_a_plan_the_cms_refused_does_not_occupy_its_pair(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    page = await w.mock.get_page(page_id=w.page_ids()[SITE + EV])

    async def _protect(_request: LinkChangeRequest) -> None:
        w.mock._pages[page.page_id] = page.model_copy(update={"protected": True})

    w.cms.before_apply = _protect
    await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert await plan_status(sessionmaker, li.id) is S.FAILED
    await recommend(sessionmaker, source_page_ids=[li.source_page_id])
    assert [
        p.status for p in await _pair_plans(sessionmaker, li.source_page_id, li.target_page_id)
    ] == [
        "failed",
        "proposed",
    ]


# --------------------------------------------------------------------------- #
# The READ ONLY reader
# --------------------------------------------------------------------------- #
async def test_the_reader_lists_and_details_deployments(sessionmaker: SM, w: World) -> None:
    detail, ids = await _deployed(w)
    reader = SqlDeploymentReader(sessionmaker=create_readonly_sessionmaker(sessionmaker.kw["bind"]))
    listed = await reader.list_deployments(limit=10, offset=0)
    assert listed.page.total == 1 and [d.id for d in listed.items] == [detail.id]
    assert listed.items[0].source_url == SITE + EV
    assert (await reader.list_deployments(limit=10, offset=0, status=D.FAILED)).page.total == 0
    assert (
        await reader.list_deployments(limit=10, offset=0, source_page_id=uuid.uuid4())
    ).items == []
    got = await reader.get_deployment(deployment_id=detail.id)
    assert got.model_dump(exclude={"idempotent_replay"}) == detail.model_dump(
        exclude={"idempotent_replay"}
    )
    assert [d.id for d in await reader.deployments_for_plan(plan_id=ids[0])] == [detail.id]
    assert await reader.deployments_for_plan(plan_id=w.plans[(EV, LI)].id) != []
    other = next(p for (s, _t), p in w.plans.items() if s != EV)
    assert await reader.deployments_for_plan(plan_id=other.id) == []
    with pytest.raises(PlanNotFoundError):
        await reader.deployments_for_plan(plan_id=uuid.uuid4())
    with pytest.raises(DeploymentNotFoundError):
        await reader.get_deployment(deployment_id=uuid.uuid4())


async def test_a_reader_session_cannot_write(sessionmaker: SM) -> None:
    readonly = create_readonly_sessionmaker(sessionmaker.kw["bind"])
    async with readonly() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
        with pytest.raises(DBAPIError) as info:
            await session.execute(
                text(
                    "INSERT INTO ingestion_runs (id, source_type, source_name, status) "
                    "VALUES (gen_random_uuid(), 'file_export', 'x', 'running')"
                )
            )
        assert getattr(info.value.orig, "sqlstate", None) == "25006"
    # Every NEW transaction is read only too (after_begin runs per transaction).
    async with readonly() as session:
        await session.execute(text("SELECT 1"))
        await session.commit()
        with pytest.raises(DBAPIError):
            await session.execute(text("CREATE TEMP TABLE nope (x int)"))
    assert await count(sessionmaker, "ingestion_runs") == 0


async def test_old_applying_rows_are_found_by_age(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    w.cms.fail_before = 1
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    later = datetime.now(UTC) + timedelta(hours=2)
    summary = await w.service(clock=lambda: later).reconcile()  # default horizon (300 s)
    assert summary.applied == 1


async def test_a_per_plan_rollback_replays_and_operation_ids_never_cross(
    sessionmaker: SM, w: World
) -> None:
    detail, ids = await _deployed(w)
    request = rollback_request(plan_ids=[ids[1]])
    first = await w.service().rollback(
        deployment_id=detail.id, request=request, principal=w.operator
    )
    assert first.status is D.VERIFIED and not first.idempotent_replay
    transitions = await count(sessionmaker, "link_plan_transitions")
    replay = await w.service().rollback(
        deployment_id=detail.id, request=request, principal=w.operator
    )
    assert replay.idempotent_replay
    assert await count(sessionmaker, "link_plan_transitions") == transitions
    with pytest.raises(OperationReusedDeploymentError):  # same id, other plans
        await w.service().rollback(
            deployment_id=detail.id,
            request=rollback_request(op=request.operation_id, plan_ids=[ids[0]]),
            principal=w.operator,
        )
    with pytest.raises(OperationReusedDeploymentError):  # a deploy id is never a rollback id
        await w.service().rollback(
            deployment_id=detail.id,
            request=rollback_request(op=detail.operation_id),
            principal=w.operator,
        )
    with pytest.raises(OperationReusedDeploymentError):  # ...nor the reverse
        await w.service().deploy(
            request=deploy_request(ids, op=request.operation_id), principal=w.operator
        )


async def test_rollback_after_an_edit_to_the_same_paragraph_keeps_the_edit(
    sessionmaker: SM, w: World
) -> None:
    """The paragraph holding our link changed elsewhere (its text hash moved): the mutator
    still finds OUR intact link (id, href, anchor) and unwraps it."""
    detail, ids = await _deployed(w)
    page_id = w.page_ids()[SITE + EV]
    body = await _body(w)
    edited = body.replace("over the coming decade.", "over the coming decade, analysts say.")
    assert edited != body
    w.mock.edit_page(page_id=page_id, body_html=edited)

    rolled = await w.service().rollback(
        deployment_id=detail.id, request=rollback_request(), principal=w.operator
    )

    after = await _body(w)
    assert rolled.status is D.ROLLED_BACK
    assert "analysts say." in after and "data-ken-link-id" not in after
    assert await statuses(sessionmaker, ids) == [S.ROLLED_BACK, S.ROLLED_BACK]


async def test_an_edited_paragraph_without_our_link_id_is_a_rollback_conflict(
    sessionmaker: SM, w: World
) -> None:
    """Same paragraph edit, and a CMS sanitiser stripped data-*: nothing proves the remaining
    link to our target is ours unchanged, so it is never removed (rollback_conflict)."""
    detail, ids = await _deployed(w)
    page_id = w.page_ids()[SITE + EV]
    body = await _body(w)
    edited = body.replace("over the coming decade.", "over the coming decade, analysts say.")
    for plan_id in ids:
        edited = edited.replace(f' data-ken-link-id="{plan_id}"', "")
    w.mock.edit_page(page_id=page_id, body_html=edited)
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().rollback(
            deployment_id=detail.id, request=rollback_request(), principal=w.operator
        )
    assert {code for code, _plan in _codes(info.value)} == {C.ROLLBACK_CONFLICT.value}
    assert await _body(w) == edited
    assert await statuses(sessionmaker, ids) == [S.VERIFIED, S.VERIFIED]
