"""The deployment service on the ``*_test`` database (ADR-0014 s1 and s8): validate ->
dry run -> durable intent -> apply -> verify -> typed events; idempotency, the per-source
lock, CMS version conflicts and CMS refusals, deploy-time checks.

Plans are REAL engine plans approved by a reviewer; the CMS is the in-memory provider over the
inventory's own pages with the REAL deterministic HTML library on both sides.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from ken_contracts.cms import CMSPage, LinkChangeRequest, ValidationIssue
from ken_contracts.deployment import (
    DEPLOYMENT_ACTOR,
    CMSUnavailableDeploymentError,
    DeploymentIssueCode,
    DeploymentRefusedError,
    DeploymentStatus,
    LinkDeployedPayload,
    LinkFailedPayload,
    LinkVerifiedPayload,
)
from ken_contracts.events import EventName
from ken_contracts.recommendations import LinkPlanStatus, PlanNotFoundError
from ken_core.db.models.deployment import ItemStage
from ken_services.deployment.errors import (
    DeploymentPermissionError,
    OperationReusedDeploymentError,
)
from ken_services.deployment.service import deploy_fingerprint, deploy_lock_key
from ken_services.recommendations.policy import load_recommendation_policy
from tests.integration.deployment_support import (
    World,
    count,
    deploy_request,
    dry_request,
    events,
    plan_status,
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
    import_records,
    record,
    rows,
)
from tests.integration.review_support import principal

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus
D = DeploymentStatus
C = DeploymentIssueCode
TABLES = (
    "deployments",
    "deployment_items",
    "deployment_verifications",
    "events",
    "link_plan_transitions",
)


@pytest.fixture
async def w(sessionmaker: SM, tmp_path: Path) -> World:
    return await world(sessionmaker, tmp_path)


async def _approved(w: World) -> list[uuid.UUID]:
    """The EV page's two plans (charging infrastructure, lithium-ion batteries), approved."""
    plans = [w.plans[(EV, CHARGING)], w.plans[(EV, LI)]]
    return await w.approve(*plans)


async def _counts(sessionmaker: SM) -> dict[str, int]:
    return {t: await count(sessionmaker, t) for t in TABLES}


async def _cms_body(w: World, path: str = EV) -> CMSPage:
    return await w.mock.get_page(page_id=w.page_ids()[SITE + path])


def _codes(error: Any) -> set[tuple[str, uuid.UUID | None]]:
    return {(DeploymentIssueCode(i.code).value, i.plan_id) for i in error.issues}


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
async def test_a_dry_run_previews_writes_nothing_and_never_consumes_the_operation_id(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    before = await _counts(sessionmaker)
    page = await _cms_body(w)
    op = uuid.uuid4()

    result = await w.service().dry_run(request=dry_request(ids, op=op), principal=w.operator)

    assert result.valid and result.issues == []
    assert result.cms_page_id == page.page_id and result.cms_version == page.version
    assert {c.plan_id for c in result.changes} == set(ids)
    for change in result.changes:
        assert change.block_text[change.anchor_start : change.anchor_end] == change.anchor_text
        assert f'data-ken-link-id="{change.plan_id}"' in change.html_after
        assert "data-ken-link-id" not in change.html_before
        assert change.href.startswith(SITE + "/reports/")
    assert await _counts(sessionmaker) == before
    assert await statuses(sessionmaker, ids) == [S.APPROVED, S.APPROVED]
    assert (await _cms_body(w)) == page  # the CMS's own validate never mutates
    # The operation id was NOT consumed: deploying with it works.
    detail = await w.service().deploy(request=deploy_request(ids, op=op), principal=w.operator)
    assert detail.status is D.VERIFIED and not detail.idempotent_replay


async def test_a_dry_run_with_issues_is_valid_false(sessionmaker: SM, w: World) -> None:
    charging, li = w.plans[(EV, CHARGING)], w.plans[(EV, LI)]
    await w.approve(charging)  # li stays proposed
    result = await w.service().dry_run(
        request=dry_request([charging.id, li.id]), principal=w.operator
    )
    assert not result.valid
    assert (C.PLAN_NOT_APPROVED, li.id) in {(i.code, i.plan_id) for i in result.issues}
    assert result.source_page_id == charging.source_page_id


async def test_plans_of_two_sources_are_refused(sessionmaker: SM, w: World) -> None:
    other = next(p for (s, _t), p in w.plans.items() if s != EV)
    ids = await w.approve(w.plans[(EV, LI)], other)
    result = await w.service().dry_run(request=dry_request(ids), principal=w.operator)
    assert not result.valid and [i.code for i in result.issues] == [C.MIXED_SOURCES]
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert _codes(info.value) == {(C.MIXED_SOURCES.value, None)}
    assert await count(sessionmaker, "deployments") == 0


async def test_an_unknown_plan_is_404(w: World) -> None:
    with pytest.raises(PlanNotFoundError):
        await w.service().dry_run(request=dry_request([uuid.uuid4()]), principal=w.operator)
    with pytest.raises(PlanNotFoundError):
        await w.service().deploy(request=deploy_request([uuid.uuid4()]), principal=w.operator)


async def test_only_an_operator_may_deploy(w: World) -> None:
    ids = await _approved(w)
    for who in (w.reviewer, w.viewer):
        with pytest.raises(DeploymentPermissionError):
            await w.service().deploy(request=deploy_request(ids), principal=who)
        with pytest.raises(DeploymentPermissionError):
            await w.service().dry_run(request=dry_request(ids), principal=who)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_deploy_applies_verifies_and_records_everything(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    original = await _cms_body(w)
    op = uuid.uuid4()

    detail = await w.service().deploy(
        request=deploy_request(ids, op=op, note="ship"), principal=w.operator
    )

    assert detail.status is D.VERIFIED
    assert detail.operation_id == op and detail.plan_ids == ids
    assert detail.actor == "user:otto" and detail.cms_provider == "mock"
    assert detail.previous_version == original.version
    page = await _cms_body(w)
    assert detail.new_version == page.version != original.version
    assert detail.applied_at is not None and detail.verified_at is not None
    assert [i.plan_status for i in detail.items] == [S.VERIFIED, S.VERIFIED]
    assert len(detail.preview) == 2 and len(detail.verifications) == 1
    run = detail.verifications[0]
    assert run.passed and not run.inconclusive and not run.http_checked
    assert len(run.checks) == 2 * 7

    # The splice: exactly our two anchors, nothing else changed.
    for plan_id in ids:
        assert page.body_html.count(f'data-ken-link-id="{plan_id}"') == 1
    stripped = re.sub(r'<a href="[^"]*" data-ken-link-id="[^"]*">(.*?)</a>', r"\1", page.body_html)
    assert stripped == original.body_html

    # Plans: approved -> validated -> deployed -> verified, by the deployment actor.
    for plan_id in ids:
        moves = await rows(
            sessionmaker,
            "SELECT from_status, to_status, actor, note FROM link_plan_transitions "
            "WHERE plan_id = :p AND actor = :a ORDER BY id",
            p=plan_id,
            a=DEPLOYMENT_ACTOR,
        )
        assert [(m.from_status, m.to_status) for m in moves] == [
            ("approved", "validated"),
            ("validated", "deployed"),
            ("deployed", "verified"),
        ]
        assert all("user:otto" in m.note and str(op) in m.note for m in moves)

    # Items: requested, applied, verified (append-only stages).
    stages = await rows(
        sessionmaker,
        "SELECT plan_id, stage FROM deployment_items WHERE deployment_id = :d ORDER BY id",
        d=detail.id,
    )
    assert [s.stage for s in stages] == [ItemStage.REQUESTED] * 2 + [ItemStage.APPLIED] * 2 + [
        ItemStage.VERIFIED
    ] * 2

    # Typed events in the transaction of the status change, correlated by the operation id.
    deployed = await events(sessionmaker, EventName.LINK_DEPLOYED.value)
    verified = await events(sessionmaker, EventName.LINK_VERIFIED.value)
    assert len(deployed) == len(verified) == 2
    for event in deployed + verified:
        assert event.correlation_id == op and event.event_version == 1
        assert event.subject_type == "link_plan" and uuid.UUID(event.subject_id) in ids
    payloads = [LinkDeployedPayload.model_validate(e.payload) for e in deployed]
    assert {p.plan_id for p in payloads} == set(ids)
    for payload in payloads:
        assert payload.deployment_id == detail.id and payload.operation_id == op
        assert payload.previous_version == original.version
        assert payload.new_version == page.version
        assert payload.cms_page_id == page.page_id and payload.actor == "user:otto"
        assert payload.href.startswith(SITE)
    for event in verified:
        payload_v = LinkVerifiedPayload.model_validate(event.payload)
        assert payload_v.verification_run_id == run.id and not payload_v.http_checked

    # The stored request is the EXACT LinkChangeRequest the CMS received.
    stored = await rows(sessionmaker, "SELECT request_json, request_fingerprint FROM deployments")
    sent = w.cms.applied[-1]
    assert stored[0].request_json == sent.model_dump_json()
    assert LinkChangeRequest.model_validate_json(stored[0].request_json) == sent
    assert stored[0].request_fingerprint == deploy_fingerprint(
        request=deploy_request(ids, op=op, note="ship"), user_id=w.operator.user_id
    )


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
async def test_an_identical_replay_returns_the_stored_outcome(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    request = deploy_request(ids)
    first = await w.service().deploy(request=request, principal=w.operator)
    before = await _counts(sessionmaker)
    page = await _cms_body(w)

    again = await w.service().deploy(request=request, principal=w.operator)

    assert again.idempotent_replay and again.id == first.id and again.status is first.status
    assert await _counts(sessionmaker) == before
    assert await _cms_body(w) == page
    assert len(w.cms.applied) == 1
    # The plan order in the request does not change the fingerprint.
    reordered = deploy_request(list(reversed(ids)), op=request.operation_id)
    assert (await w.service().deploy(request=reordered, principal=w.operator)).idempotent_replay


async def test_a_reused_operation_id_with_another_request_is_409(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    op = uuid.uuid4()
    await w.service().deploy(request=deploy_request(ids, op=op), principal=w.operator)
    before = await _counts(sessionmaker)
    for changed in (
        deploy_request(ids, op=op, note="different"),
        deploy_request(ids[:1], op=op),
    ):
        with pytest.raises(OperationReusedDeploymentError) as info:
            await w.service().deploy(request=changed, principal=w.operator)
        assert info.value.issues == []
    # Another user replaying the same body is a different request too.
    other = await principal(sessionmaker, "olga", role=w.operator.role)
    with pytest.raises(OperationReusedDeploymentError):
        await w.service().deploy(request=deploy_request(ids, op=op), principal=other)
    assert await _counts(sessionmaker) == before


# --------------------------------------------------------------------------- #
# Refusals before anything is written
# --------------------------------------------------------------------------- #
async def test_a_refused_deploy_writes_nothing(sessionmaker: SM, w: World) -> None:
    charging, li = w.plans[(EV, CHARGING)], w.plans[(EV, LI)]
    await w.approve(charging)
    before = await _counts(sessionmaker)
    page = await _cms_body(w)
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().deploy(request=deploy_request([charging.id, li.id]), principal=w.operator)
    assert (C.PLAN_NOT_APPROVED.value, li.id) in _codes(info.value)
    assert await _counts(sessionmaker) == before
    assert await statuses(sessionmaker, [charging.id, li.id]) == [S.APPROVED, S.PROPOSED]
    assert await _cms_body(w) == page and w.cms.applied == []


async def test_a_concurrent_deployment_of_the_source_is_409_in_progress(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    source = w.plans[(EV, LI)].source_page_id
    async with sessionmaker() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": deploy_lock_key(source)})
        try:
            with pytest.raises(DeploymentRefusedError) as info:
                await w.service().deploy(request=deploy_request(ids), principal=w.operator)
            assert _codes(info.value) == {(C.DEPLOYMENT_IN_PROGRESS.value, None)}
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": deploy_lock_key(source)}
            )
            await holder.commit()
    assert await statuses(sessionmaker, ids) == [S.APPROVED, S.APPROVED]  # never expired
    assert await count(sessionmaker, "deployments") == 0
    # Retryable: once the lock is free the same request deploys.
    detail = await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert detail.status is D.VERIFIED


async def test_two_simultaneous_deploys_of_one_source_never_both_run(
    sessionmaker: SM, w: World
) -> None:
    ids = await _approved(w)
    gate = asyncio.Event()

    async def _slow(_request: LinkChangeRequest) -> None:
        await gate.wait()

    w.cms.before_apply = _slow
    first = asyncio.create_task(
        w.service().deploy(request=deploy_request(ids[:1]), principal=w.operator)
    )
    for _ in range(200):
        if w.cms.applied:
            break
        await asyncio.sleep(0.01)
    assert w.cms.applied  # the first is inside its CMS call, holding the source lock
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().deploy(request=deploy_request(ids[1:]), principal=w.operator)
    assert _codes(info.value) == {(C.DEPLOYMENT_IN_PROGRESS.value, None)}
    assert await plan_status(sessionmaker, ids[1]) is S.APPROVED
    gate.set()
    assert (await first).status is D.VERIFIED


# --------------------------------------------------------------------------- #
# CMS outcomes recorded in transaction 2
# --------------------------------------------------------------------------- #
async def test_a_cms_version_conflict_expires_the_plans(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    page = await _cms_body(w)
    edited = page.body_html.replace("</main>", "<p>An editor added this paragraph.</p></main>")

    async def _editor(_request: LinkChangeRequest) -> None:
        w.mock.edit_page(page_id=page.page_id, body_html=edited)  # between validate and apply

    w.cms.before_apply = _editor
    request = deploy_request(ids)
    detail = await w.service().deploy(request=request, principal=w.operator)

    assert detail.status is D.CONFLICT and detail.error_code == "cms_version_conflict"
    assert detail.applied_at is None and detail.verifications == []
    assert await statuses(sessionmaker, ids) == [S.EXPIRED, S.EXPIRED]
    assert [i.plan_status for i in detail.items] == [S.EXPIRED, S.EXPIRED]
    assert (await _cms_body(w)).body_html == edited  # nothing applied
    assert await events(sessionmaker, EventName.LINK_DEPLOYED.value) == []
    # The replay returns the stored conflict, with the same status code (no new CMS call).
    w.cms.before_apply = None
    again = await w.service().deploy(request=request, principal=w.operator)
    assert again.status is D.CONFLICT and again.idempotent_replay and again.id == detail.id
    assert len(w.cms.applied) == 1


async def test_a_cms_refusal_fails_the_plans(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    page = await _cms_body(w)

    async def _protect(_request: LinkChangeRequest) -> None:
        # The CMS protects the page after our validation (a terminal CMS refusal).
        w.mock._pages[page.page_id] = page.model_copy(update={"protected": True})

    w.cms.before_apply = _protect
    request = deploy_request(ids)
    detail = await w.service().deploy(request=request, principal=w.operator)

    assert detail.status is D.FAILED and detail.error_code == "cms_validation_failed"
    assert detail.applied_at is None
    assert await statuses(sessionmaker, ids) == [S.FAILED, S.FAILED]
    failed = await events(sessionmaker, EventName.LINK_FAILED.value)
    assert len(failed) == 2
    for event in failed:
        payload = LinkFailedPayload.model_validate(event.payload)
        assert payload.stage == "apply" and payload.codes == ["protected_page"]
        assert event.correlation_id == request.operation_id
    stages = await rows(sessionmaker, "SELECT stage, codes FROM deployment_items ORDER BY id")
    assert [s.stage for s in stages] == ["requested", "requested", "failed", "failed"]
    again = await w.service().deploy(request=request, principal=w.operator)
    assert again.status is D.FAILED and again.idempotent_replay


async def test_an_unreachable_cms_at_validation_writes_nothing(sessionmaker: SM, w: World) -> None:
    ids = await _approved(w)
    before = await _counts(sessionmaker)
    w.cms.unavailable = True
    with pytest.raises(CMSUnavailableDeploymentError) as info:
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert _codes(info.value) == {(C.CMS_UNAVAILABLE.value, None)}
    with pytest.raises(CMSUnavailableDeploymentError):
        await w.service().dry_run(request=dry_request(ids), principal=w.operator)
    assert await _counts(sessionmaker) == before
    assert await statuses(sessionmaker, ids) == [S.APPROVED, S.APPROVED]


# --------------------------------------------------------------------------- #
# Deploy-time checks (ADR-0014 s1.3, s3, s8)
# --------------------------------------------------------------------------- #
async def _refusal(w: World, ids: list[uuid.UUID], **service: Any) -> set[tuple[str, Any]]:
    result = await w.service(**service).dry_run(request=dry_request(ids), principal=w.operator)
    assert not result.valid
    return {(DeploymentIssueCode(i.code).value, i.plan_id) for i in result.issues}


async def test_a_stale_plan_is_expired_at_deploy(
    sessionmaker: SM, w: World, tmp_path: Path
) -> None:
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    paragraphs = list(EV_MARKET_PARAS)
    paragraphs[1] = _p("Vehicle makers buy imported batteries from several countries today.")
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2.jsonl"
    )
    with pytest.raises(DeploymentRefusedError) as info:
        await w.service().deploy(request=deploy_request(ids), principal=w.operator)
    assert (C.PLAN_STALE.value, li.id) in _codes(info.value)
    # The stale plan is expired (committed); the other stays approved; nothing was deployed.
    assert await plan_status(sessionmaker, li.id) is S.EXPIRED
    assert await plan_status(sessionmaker, w.plans[(EV, CHARGING)].id) is S.APPROVED
    assert await count(sessionmaker, "deployments") == 0


async def test_link_density_counts_the_cms_html_and_the_batch(w: World) -> None:
    ids = await _approved(w)
    policy = load_recommendation_policy(SETTINGS.recommendation_policy_path)
    tight = policy.model_copy(
        update={
            "selection": policy.selection.model_copy(
                update={"max_internal_links_per_100_words": 0.5}
            )
        }
    )
    service = w.service()
    service._policy = tight
    result = await service.dry_run(request=dry_request(ids), principal=w.operator)
    assert (C.DENSITY_EXCEEDED, None) in {(i.code, i.plan_id) for i in result.issues}


async def test_a_target_already_linked_in_the_cms_html_is_refused(w: World) -> None:
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    page = await _cms_body(w)
    href = SITE + LI
    w.mock.edit_page(
        page_id=page.page_id,
        body_html=page.body_html.replace(
            "</main>", f'<p>See <a href="{href}">the battery report</a>.</p></main>'
        ),
    )
    assert (C.ALREADY_LINKED.value, li.id) in await _refusal(w, ids)


async def test_an_editorial_link_at_the_span_is_already_linked_never_adopted(w: World) -> None:
    """Orchestrator rule (1): identity is (block, span, href), so an editor's link with OUR
    href around OUR anchor must be ``already_linked`` -- if it were adopted as
    ``already_applied``, a later rollback would unwrap the editor's link."""
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    page = await _cms_body(w)
    anchor = li.anchor_text
    assert page.body_html.count(anchor) >= 1
    body = page.body_html.replace(anchor, f'<a href="{SITE + LI}">{anchor}</a>', 1)
    w.mock.edit_page(page_id=page.page_id, body_html=body)
    issues = await _refusal(w, ids)
    assert (C.ALREADY_LINKED.value, li.id) in issues
    assert (C.ALREADY_APPLIED.value, li.id) not in issues


async def test_an_unmapped_or_missing_cms_page_is_refused(w: World) -> None:
    ids = await _approved(w)
    w.cms.resolve_override[SITE + EV] = None
    assert (C.CMS_PAGE_UNMAPPED.value, None) in await _refusal(w, ids)
    w.cms.resolve_override[SITE + EV] = "cms-no-such-page"
    assert await _refusal(w, ids) == {(C.CMS_PAGE_NOT_FOUND.value, None)}
    w.cms.resolve_override = {SITE + LI: None}
    assert (C.CMS_PAGE_UNMAPPED.value, w.plans[(EV, LI)].id) in await _refusal(w, ids)


async def test_a_protected_cms_page_is_refused(w: World) -> None:
    ids = await _approved(w)
    page = await _cms_body(w)
    w.mock._pages[page.page_id] = page.model_copy(update={"protected": True})
    assert (C.CMS_PAGE_PROTECTED.value, None) in await _refusal(w, ids)


async def test_an_href_off_the_allowed_hosts_is_refused(w: World) -> None:
    ids = await _approved(w)
    service = w.service()
    service._allowed_hosts = lambda source_url: {"elsewhere.test"}  # type: ignore[method-assign]
    result = await service.dry_run(request=dry_request(ids), principal=w.operator)
    assert {(i.code, i.plan_id) for i in result.issues} >= {(C.HREF_INVALID, p) for p in ids}


async def test_a_moved_anchor_is_refused_by_the_mutator(w: World) -> None:
    """The CMS HTML no longer has the planned block text (an unpublished CMS edit the
    inventory has not seen): the mutator's block_not_found, nothing applied."""
    ids = await _approved(w)
    li = w.plans[(EV, LI)]
    page = await _cms_body(w)
    w.mock.edit_page(
        page_id=page.page_id,
        body_html=page.body_html.replace("planned to cut that dependence", "planned to reduce it"),
    )
    assert (C.BLOCK_NOT_FOUND.value, li.id) in await _refusal(w, ids)


async def test_the_cms_own_validation_is_consulted(w: World) -> None:
    ids = await _approved(w)
    original = w.cms.validate_link_changes

    async def _refuse(*, request: LinkChangeRequest) -> Any:
        result = await original(request=request)
        issue = ValidationIssue(code="cms_rule", severity="error", message="no", change_id=ids[0])
        return result.model_copy(update={"valid": False, "issues": [issue]})

    w.cms.validate_link_changes = _refuse  # type: ignore[method-assign]
    issues = await _refusal(w, ids)
    assert issues == {(C.CMS_VALIDATION_FAILED.value, ids[0])}
