"""Stages 9-15: the autonomy engine, mode by mode, to a verified (or rolled-back) live page.

Everything here goes through the production objects: the engine, the publisher, the deployment
service, the simulated CMS and its HTTP verification. The only demo-specific code is selection
(which plan plays which role, chosen deterministically) and the fault injection in
:mod:`ken_services.demo.faults`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ken_contracts.autonomy import AutonomyDecision, AutonomyMode, BlockCode
from ken_contracts.intelligence import ProtectionSource
from ken_core.db.models.intelligence import PageProtection
from ken_services.autonomy.calibration import decision_class_of
from ken_services.autonomy.compose import build_engine, build_publisher
from ken_services.autonomy.engine import CriticDeclinedError, DatabaseAutonomyEngine
from ken_services.autonomy.facts import plan_identity
from ken_services.autonomy.job import run_autonomy
from ken_services.autonomy.publish import AutonomousPublisher, PublishOutcome, PublishResult
from ken_services.demo.config import settings_for
from ken_services.demo.context import Ctx, fetchall, fetchone, need, scalar
from ken_services.demo.faults import FaultedVerifier
from ken_services.demo.pipeline import path_of
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.service import DatabaseDeploymentService
from ken_services.recommendations.pipeline import run_recommendations

__all__ = [
    "PlanInfo",
    "catalog",
    "stage_block",
    "stage_calibration",
    "stage_main_pass",
    "stage_modes",
    "stage_no_action",
    "stage_rollback",
]

D = AutonomyDecision


@dataclass(frozen=True, slots=True)
class PlanInfo:
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    source_url: str
    target_url: str
    status: str
    confidence: float
    anchor: str
    klass: str | None

    @property
    def label(self) -> str:
        return f"{path_of(self.source_url)} -> {path_of(self.target_url)}"


async def catalog(ctx: Ctx) -> list[PlanInfo]:
    """Every plan with its decision class, in a stable order (source URL, target URL)."""
    rows = await fetchall(
        ctx,
        "SELECT id, source_page_id, target_page_id, source_url, target_url, status, confidence, "
        "anchor_text FROM link_plans ORDER BY source_url, target_url, created_at, id",
    )
    out: list[PlanInfo] = []
    for r in rows:
        async with ctx.sessionmaker() as session:
            identity = await plan_identity(session, plan_id=r.id)
            await session.rollback()
        decision_class = decision_class_of(identity)
        out.append(
            PlanInfo(
                id=r.id,
                source_id=r.source_page_id,
                target_id=r.target_page_id,
                source_url=r.source_url,
                target_url=r.target_url,
                status=r.status,
                confidence=float(r.confidence),
                anchor=r.anchor_text,
                klass=None if decision_class is None else decision_class.canonical,
            )
        )
    return out


def _stack(
    ctx: Ctx, mode: str, *, verifier: Any = None
) -> tuple[Any, DatabaseAutonomyEngine, AutonomousPublisher]:
    settings = settings_for(ctx.config, mode=mode, verify_base_url=ctx.site_url)
    engine = build_engine(sessionmaker=ctx.sessionmaker, settings=settings)
    deployments = None
    if verifier is not None:
        deployments = DatabaseDeploymentService(
            sessionmaker=ctx.sessionmaker,
            settings=settings,
            cms=ctx.cms,
            mutator=HtmlMutator(),
            verifier=verifier,
        )
    publisher = build_publisher(
        sessionmaker=ctx.sessionmaker,
        settings=settings,
        principal=ctx.principal,
        cms=ctx.cms,
        engine=engine,
        deployments=deployments,
    )
    return settings, engine, publisher


async def _decisions(ctx: Ctx, plan_id: uuid.UUID) -> list[Any]:
    return await fetchall(
        ctx, "SELECT * FROM autonomy_decisions WHERE plan_id = :p ORDER BY id", p=plan_id
    )


def _fmt_decision(r: Any) -> str:
    return (
        f"#{r.id} mode={r.mode} decision={r.decision} would_have={r.would_have_decision} "
        f"risk={r.risk_tier} conf={r.confidence:.3f} "
        f"calib={'yes' if r.calibration_version else 'none'} "
        f"profile={r.verification_profile} critic={r.critic_outcome} blocks={list(r.blocks)}"
    )


async def _counts(ctx: Ctx) -> dict[str, int]:
    return {
        "deployments": int(await scalar(ctx, "SELECT count(*) FROM deployments") or 0),
        "publish_attempts": int(await scalar(ctx, "SELECT count(*) FROM publish_attempts") or 0),
        "cms_writes": ctx.cms.writes,
    }


async def _status(ctx: Ctx, plan_id: uuid.UUID) -> str:
    return str(await scalar(ctx, "SELECT status FROM link_plans WHERE id = :p", p=plan_id))


async def _cms_body(ctx: Ctx, url: str) -> str:
    page_id = await ctx.sim.resolve_page_id(url=url)
    page_id = need(page_id)
    return str((await ctx.sim.get_page(page_id=page_id)).body_html)


def _roles(ctx: Ctx, plans: list[PlanInfo]) -> dict[str, Any]:
    """Pick, deterministically, which plan plays which part in the demo."""
    accepted = [p for p in plans if p.status == "policy_checked" and p.confidence >= 0.55]
    by_class: dict[str, list[PlanInfo]] = {}
    for p in accepted:
        if p.klass is not None:
            by_class.setdefault(p.klass, []).append(p)
    calibrated_class = sorted(by_class, key=lambda k: (-len(by_class[k]), k))[0]
    in_class = {p.id for p in by_class[calibrated_class]}
    rest = [p for p in accepted if p.id not in in_class]
    guarded = [p for p in plans if p.status == "policy_checked" and p.id not in in_class][-3:]
    guarded_ids = {p.id for p in guarded}
    rest = [p for p in rest if p.id not in guarded_ids]
    inbound: dict[uuid.UUID, int] = {}
    for p in plans:
        if p.status == "policy_checked":
            inbound[p.target_id] = inbound.get(p.target_id, 0) + 1
    block = sorted(rest, key=lambda p: (inbound[p.target_id], p.source_url, p.target_url))[0]
    rollback = next(
        p
        for p in rest
        if p.id != block.id
        and block.target_id not in (p.source_id, p.target_id)
        and p.source_id != block.source_id
    )
    return {
        "class": calibrated_class,
        "class_plans": by_class[calibrated_class],
        "guarded": guarded,
        "block": block,
        "rollback": rollback,
    }


# --------------------------------------------------------------------------- #
# 9. Shadow, then guarded
# --------------------------------------------------------------------------- #
async def stage_modes(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(9, "Autonomy modes on the same plans: shadow (counterfactual) and guarded (deferral)")
    plans = await catalog(ctx)
    roles = _roles(ctx, plans)
    ctx.state["roles"] = roles
    ctx.state["catalog"] = plans
    out.note(
        f"{len(plans)} plans: {sum(p.status == 'policy_checked' for p in plans)} handed on by the "
        "critic, "
        f"{sum(p.status == 'proposed' for p in plans)} declined by it (uncertain)"
    )
    before = await _counts(ctx)
    statuses_before = await fetchall(
        ctx, "SELECT status, count(*) AS n FROM link_plans GROUP BY 1 ORDER BY 1"
    )

    # -- shadow: the whole decidable set
    settings, engine, _ = _stack(ctx, "shadow")
    detail = await run_autonomy(
        sessionmaker=ctx.sessionmaker,
        settings=settings,
        triggered_by="demo",
        mode=AutonomyMode.SHADOW,
        engine=engine,
    )
    out.line()
    out.note("SHADOW pass (mode=shadow; records a counterfactual, writes nothing else)")
    out.kv("job kind / status", f"{detail.kind.value} / {detail.status.value}")
    out.kv("counters", ", ".join(f"{k}={v}" for k, v in sorted(detail.counters.items()) if v))
    shadow = await fetchall(
        ctx,
        "SELECT would_have_decision AS w, decision AS d, count(*) AS n FROM autonomy_decisions "
        "WHERE mode = 'shadow' GROUP BY 1, 2 ORDER BY 1, 2",
    )
    out.table(
        ["recorded decision", "would_have_decision (counterfactual)", "plans"],
        [(r.d, r.w, r.n) for r in shadow],
    )
    after = await _counts(ctx)
    statuses_after = await fetchall(
        ctx, "SELECT status, count(*) AS n FROM link_plans GROUP BY 1 ORDER BY 1"
    )
    nothing = before == after and [tuple(r) for r in statuses_before] == [
        tuple(r) for r in statuses_after
    ]
    out.require(
        "6",
        nothing and all(r.d == "block" for r in shadow) and any(r.w is not None for r in shadow),
        "shadow recorded counterfactual would_have_decision rows; deployments, attempts, CMS "
        f"writes and plan statuses unchanged ({after})",
    )
    shadow_would = {r.w: r.n for r in shadow}
    ctx.state["shadow_would"] = shadow_would

    # -- guarded: a sample of plans, so the rest stay available to the autonomous pass
    out.line()
    out.note(
        "GUARDED pass (mode=guarded) on 3 of the same plans; deferred plans end 'blocked' by design"
    )
    _, _, guarded_publisher = _stack(ctx, "guarded")
    results: list[tuple[PlanInfo, PublishResult]] = []
    for plan in roles["guarded"]:
        results.append((plan, await guarded_publisher.publish_plan(plan_id=plan.id)))
    for plan, res in results:
        rows = [r for r in await _decisions(ctx, plan.id) if r.mode == "guarded"]
        note = await scalar(
            ctx,
            "SELECT note FROM link_plan_transitions WHERE plan_id = :p AND to_status = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            p=plan.id,
        )
        out.note(f"{plan.label}")
        out.note(
            f"    outcome={res.outcome.value} reasons={list(res.reasons)} plan "
            f"status={await _status(ctx, plan.id)}"
        )
        if rows:
            out.note(f"    decision row {_fmt_decision(rows[-1])}")
        out.note(f"    reason on the plan transition: {note!r}")
    after_guarded = await _counts(ctx)
    ok = (
        all(
            res.outcome is PublishOutcome.BLOCKED and res.reasons == ("policy_refused",)
            for _, res in results
        )
        and after_guarded == after
    )
    deferrals = []
    for plan, _res in results:
        deferrals += [r for r in await _decisions(ctx, plan.id) if r.mode == "guarded"]
    ok = (
        ok
        and bool(deferrals)
        and all(r.decision == "block" and list(r.blocks) == [] for r in deferrals)
    )
    notes_ok = True
    for plan, _res in results:
        text_note = await scalar(
            ctx,
            "SELECT note FROM link_plan_transitions WHERE plan_id = :p AND to_status = 'blocked' "
            "LIMIT 1",
            p=plan.id,
        )
        notes_ok = notes_ok and bool(text_note) and len(str(text_note)) > 20
    out.require(
        "6",
        ok and notes_ok,
        "guarded published nothing for an uncalibrated class; each deferral is a 'blocked' plan "
        "with "
        "an EMPTY block list and a readable reason; no deployment, attempt or CMS call",
    )
    out.proves(
        "Modes: shadow and guarded",
        "shadow records counterfactuals; guarded defers uncalibrated plans",
    )


# --------------------------------------------------------------------------- #
# 10. BLOCK
# --------------------------------------------------------------------------- #
async def stage_block(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(10, "BLOCK: a deterministic code stops a plan the critic accepted")
    plan: PlanInfo = ctx.state["roles"]["block"]
    verdict = await fetchone(
        ctx, "SELECT verdict, handed_off FROM agent_run_plans WHERE plan_id = :p", p=plan.id
    )
    out.note(f"plan: {plan.label}   anchor='{plan.anchor}'   raw confidence {plan.confidence:.3f}")
    verdict = need(verdict)
    out.note(f"stored critic review: verdict={verdict.verdict} handed_off={verdict.handed_off}")
    _, _, pub = _stack(ctx, "autonomous")
    _, engine_probe, _ = _stack(ctx, "autonomous")
    before = await _counts(ctx)
    source_before = await _cms_body(ctx, plan.source_url)
    out.note("Before protection, the same plan in autonomous mode would be decided:")
    pre = await engine_probe.evaluate(plan_id=plan.id, mode=AutonomyMode.AUTONOMOUS)
    out.note(f"    decision={pre.decision.value} blocks={[b.value for b in pre.blocks]}")
    async with ctx.sessionmaker() as session, session.begin():
        session.add(
            PageProtection(
                page_id=plan.target_id,
                source=ProtectionSource.MANUAL.value,
                rule_id=None,
                reason="demo: an editor protected this page after the critic accepted the plan",
                active=True,
                created_by=ctx.principal.actor,
                created_at=datetime.now(UTC),
            )
        )
    out.note(f"An editor then PROTECTS the destination {path_of(plan.target_url)}.")
    seen: list[str] = []
    for mode in ("shadow", "guarded", "autonomous"):
        _, engine, _ = _stack(ctx, mode)
        evaluation = await engine.evaluate(plan_id=plan.id, mode=AutonomyMode(mode))
        seen.append(
            f"{mode}: decision={evaluation.decision.value} "
            f"critic={evaluation.critic_outcome.value} "
            f"blocks={[b.value for b in evaluation.blocks]}"
        )
    for line in seen:
        out.note(f"    {line}")
    result = await pub.publish_plan(plan_id=plan.id)
    rows = await _decisions(ctx, plan.id)
    after = await _counts(ctx)
    out.note(
        f"autonomous publish_plan -> outcome={result.outcome.value} reasons={list(result.reasons)}"
    )
    out.note(f"    decision row {_fmt_decision(rows[-1])}")
    body_same = await _cms_body(ctx, plan.source_url) == source_before
    out.require(
        "1",
        result.outcome is PublishOutcome.BLOCKED
        and BlockCode.PROTECTED_PAGE.value in result.reasons
        and rows[-1].decision == "block"
        and rows[-1].critic_outcome == "accept"
        and before["cms_writes"] == after["cms_writes"]
        and before["deployments"] == after["deployments"]
        and before["publish_attempts"] == after["publish_attempts"]
        and body_same
        and all("decision=block" in s for s in seen),
        f"critic said accept, code protected_page said block in shadow, guarded AND autonomous; "
        f"CMS write calls {before['cms_writes']} -> {after['cms_writes']}, deployments "
        f"{before['deployments']} -> {after['deployments']}, source page unchanged={body_same}",
    )
    out.proves("BLOCK", "a deterministic code overrides a critic accept; the CMS is never called")


# --------------------------------------------------------------------------- #
# 11. NO_ACTION
# --------------------------------------------------------------------------- #
async def stage_no_action(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(
        11, "NO_ACTION: the critic declined, so there is no decision row and nothing publishes"
    )
    plans: list[PlanInfo] = [p for p in ctx.state["catalog"] if p.status == "proposed"]
    _, engine, pub = _stack(ctx, "autonomous")
    before = await _counts(ctx)
    ok = bool(plans)
    for plan in plans:
        review = await fetchone(
            ctx, "SELECT verdict, handed_off FROM agent_run_plans WHERE plan_id = :p", p=plan.id
        )
        try:
            await engine.evaluate(plan_id=plan.id, mode=AutonomyMode.AUTONOMOUS)
            raised = False
        except CriticDeclinedError:
            raised = True
        result = await pub.publish_plan(plan_id=plan.id)
        rows = await _decisions(ctx, plan.id)
        status = await _status(ctx, plan.id)
        review = need(review)
        out.note(
            f"{plan.label}: critic verdict={review.verdict} handed_off={review.handed_off}; "
            "engine raises "
            f"CriticDeclinedError={raised}; publish outcome={result.outcome.value}; decision "
            f"rows={len(rows)}; "
            f"plan status={status}"
        )
        ok = (
            ok
            and raised
            and result.outcome is PublishOutcome.DECLINED
            and not rows
            and status == "proposed"
        )
    after = await _counts(ctx)
    out.require(
        "3",
        ok and before == after,
        f"{len(plans)} plans declined by the critic: 0 decision rows, 0 deployments, 0 attempts, "
        f"CMS write calls unchanged ({after['cms_writes']})",
    )
    out.proves("NO_ACTION", "an uncertain critic review never becomes a decision or a publish")


# --------------------------------------------------------------------------- #
# 12. Rollback after a failed verification, and the retry budget
# --------------------------------------------------------------------------- #
async def stage_rollback(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(12, "Verification fails -> automatic rollback; the retry budget refuses the retry")
    plan: PlanInfo = ctx.state["roles"]["rollback"]
    out.note(f"plan: {plan.label}   anchor='{plan.anchor}'")
    out.note(
        "FAULT INJECTION (demo only): the link verifier is replaced by one that fails every check,"
    )
    out.note(
        "so verification fails after the CMS accepted the change; the CMS page itself is untouched."
    )
    verifier = FaultedVerifier()
    _, _, pub = _stack(ctx, "autonomous", verifier=verifier)
    original = await _cms_body(ctx, plan.source_url)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        http_before = (await client.get(ctx.site_url + path_of(plan.source_url))).text
    before = await _counts(ctx)
    result = await pub.publish_plan(plan_id=plan.id)
    after = await _counts(ctx)
    restored = await _cms_body(ctx, plan.source_url)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        http_after = (await client.get(ctx.site_url + path_of(plan.source_url))).text
    status = await _status(ctx, plan.id)
    out.kv("publish outcome / reasons", f"{result.outcome.value} / {list(result.reasons)}")
    out.kv("verifier calls (fault injected)", verifier.calls)
    out.kv("plan status", status)
    out.kv(
        "CMS write calls (apply, rollback)",
        f"{len(ctx.cms.applied)}, {len(ctx.cms.rolled_back)} (total {before['cms_writes']} -> "
        f"{after['cms_writes']})",
    )
    byte_identical = restored == original
    out.kv("CMS page byte-identical to its original", byte_identical)
    out.kv("served page byte-identical over HTTP", http_after == http_before)
    out.kv("link marker present after rollback", f'data-ken-link-id="{plan.id}"' in restored)
    events = await fetchall(
        ctx,
        "SELECT event_name AS name, count(*) AS n FROM events WHERE subject_id = :p "
        "AND event_name IN ('link.failed', 'link.rolled_back') GROUP BY 1 ORDER BY 1",
        p=str(plan.id),
    )
    out.kv("events for the plan", ", ".join(f"{e.name}={e.n}" for e in events))
    attempt = await fetchone(ctx, "SELECT * FROM publish_attempts WHERE plan_id = :p", p=plan.id)
    attempt = need(attempt)
    out.kv(
        "recorded attempt",
        f"attempt {attempt.attempt}/{attempt.max_attempts} succeeded={attempt.succeeded} "
        f"rolled_back={attempt.rolled_back} failure_code={attempt.failure_code} "
        f"cooldown_set={attempt.cooldown_until is not None}",
    )
    # The retry: the recommender proposes the pair again; the budget refuses to publish it.
    out.line()
    out.note(
        "Retry: the recommendation engine re-proposes the pair; the autonomy engine must refuse it."
    )
    await run_recommendations(
        sessionmaker=ctx.sessionmaker,
        settings=ctx.settings,
        triggered_by="demo",
        source_page_ids=[plan.source_id],
        model_provider=ctx.model,
        vector_store=ctx.store,
    )
    again = await fetchall(
        ctx,
        "SELECT id FROM link_plans WHERE source_page_id = :s AND target_page_id = :t "
        "AND status = 'proposed' ORDER BY created_at",
        s=plan.source_id,
        t=plan.target_id,
    )
    retry_note = "the pair was not re-proposed"
    retry_refused = False
    if again:
        _, _, retry_pub = _stack(ctx, "autonomous")
        retry = await retry_pub.publish_plan(plan_id=again[-1].id)
        attempts_now = await scalar(
            ctx,
            "SELECT count(*) FROM publish_attempts WHERE source_page_id = :s AND target_page_id = "
            ":t",
            s=plan.source_id,
            t=plan.target_id,
        )
        retry_refused = (
            retry.outcome is PublishOutcome.DEFERRED
            and "cooling_down" in retry.reasons
            and attempts_now == 1
        )
        retry_note = (
            f"outcome={retry.outcome.value} reasons={list(retry.reasons)}; "
            f"attempt rows for the pair={attempts_now}"
        )
        ctx.state["retry_plan"] = again[-1].id
    out.kv("retry of the same pair", retry_note)
    final = await _counts(ctx)
    out.require(
        "5",
        result.outcome is PublishOutcome.ROLLED_BACK
        and status == "rolled_back"
        and byte_identical
        and http_after == http_before
        and {e.name for e in events} == {"link.failed", "link.rolled_back"}
        and attempt.succeeded is False
        and attempt.rolled_back is True
        and attempt.cooldown_until is not None
        and retry_refused
        and final["cms_writes"] == after["cms_writes"],
        "verification failed -> rolled back automatically; page byte-identical; link.failed and "
        "link.rolled_back emitted; failed attempt with cool-down; immediate retry refused "
        "(cooling_down), no CMS call",
    )
    out.proves(
        "Rollback", "failed verification reverts the page byte-for-byte; the pair is on a cool-down"
    )


# --------------------------------------------------------------------------- #
# 13. The main autonomous pass
# --------------------------------------------------------------------------- #
async def stage_main_pass(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(13, "AUTONOMOUS pass: AUTO_PUBLISH_WITH_EXTRA_VERIFICATION for uncalibrated plans")
    out.note(
        "mode=autonomous, acknowledged. No class is calibrated, so every publish uses the STRICT"
    )
    out.note(
        "verification profile: a re-fetch, a second pass, and a count of every link to the "
        "destination."
    )
    held = {p.id for p in ctx.state["roles"]["class_plans"]}
    settings, engine, _ = _stack(ctx, "autonomous")
    ids = [i for i in await engine.decidable_plans() if i not in held]
    before = await _counts(ctx)
    detail = await run_autonomy(
        sessionmaker=ctx.sessionmaker,
        settings=settings,
        triggered_by="demo",
        mode=AutonomyMode.AUTONOMOUS,
        plan_ids=ids,
        principal=ctx.principal,
        cms=ctx.cms,
        engine=engine,
    )
    out.kv("job kind / status", f"{detail.kind.value} / {detail.status.value}")
    out.kv("plans considered", detail.counters.get("plans_considered", 0))
    out.kv(
        "outcomes",
        ", ".join(
            f"{k.removeprefix('outcome_')}={v}"
            for k, v in sorted(detail.counters.items())
            if k.startswith("outcome_") and v
        ),
    )
    published = await fetchall(
        ctx,
        "SELECT p.id, p.source_url, p.target_url, p.status, d.decision, d.verification_profile AS "
        "profile, "
        "d.calibration_version AS calib FROM link_plans p JOIN LATERAL (SELECT * FROM "
        "autonomy_decisions x "
        "WHERE x.plan_id = p.id ORDER BY x.id DESC LIMIT 1) d ON true "
        "WHERE p.status = 'verified' ORDER BY p.source_url, p.target_url",
    )
    out.kv("plans now 'verified' (live)", len(published))
    kinds = {(r.decision, r.profile, r.calib is None) for r in published}
    out.kv(
        "decision / profile / uncalibrated",
        ", ".join(f"{k[0]}/{k[1]}/{k[2]}" for k in sorted(kinds)),
    )
    sample = published[0]
    plan_id = sample.id
    rows = await _decisions(ctx, plan_id)
    attempt = await fetchone(ctx, "SELECT * FROM publish_attempts WHERE plan_id = :p", p=plan_id)
    deployment = await fetchone(
        ctx,
        "SELECT dep.* FROM deployments dep JOIN deployment_items i ON i.deployment_id = dep.id "
        "WHERE i.plan_id = :p",
        p=plan_id,
    )
    attempt = need(attempt)
    deployment = need(deployment)
    verifications = await fetchall(
        ctx,
        "SELECT passed, inconclusive, http_checked FROM deployment_verifications WHERE "
        "deployment_id = :d ORDER BY id",
        d=deployment.id,
    )
    body = await _cms_body(ctx, sample.source_url)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        served = (await client.get(ctx.site_url + path_of(sample.source_url))).text
    marker = f'data-ken-link-id="{plan_id}"'
    out.line()
    out.note(f"Evidence for {path_of(sample.source_url)} -> {path_of(sample.target_url)}")
    out.note(f"    decision row   {_fmt_decision(rows[-1])}")
    out.note(
        f"    publish attempt #{attempt.attempt}/{attempt.max_attempts} mode={attempt.mode} "
        f"decision={attempt.decision} "
        f"profile={attempt.verification_profile} succeeded={attempt.succeeded} "
        f"actor={attempt.actor}"
    )
    out.note(
        f"    deployment     status={deployment.status} "
        f"applied={deployment.applied_at is not None} "
        f"verified={deployment.verified_at is not None} note={deployment.note!r}"
    )
    out.note(
        "    verification   "
        + "; ".join(
            f"pass {i + 1}: passed={v.passed} http_checked={v.http_checked}"
            for i, v in enumerate(verifications)
        )
    )
    out.note(
        f"    live in the CMS page: {marker in body}; served over HTTP by the site: "
        f"{marker in served}"
    )
    after = await _counts(ctx)
    out.require(
        "2",
        len(published) >= 1
        and kinds == {("auto_publish_with_extra_verification", "strict", True)}
        and rows[-1].decision == "auto_publish_with_extra_verification"
        and attempt.succeeded is True
        and attempt.verification_profile == "strict"
        and deployment.status == "verified"
        and len(verifications) >= 2
        and all(v.passed and v.http_checked for v in verifications)
        and marker in body
        and marker in served
        and after["cms_writes"] > before["cms_writes"],
        f"{len(published)} uncalibrated plans published under the strict profile "
        f"({len(verifications)} verification "
        "passes with HTTP checks each); the link is in the CMS page and on the served page",
    )
    out.require(
        "6",
        detail.counters.get("outcome_published", 0) == len(published) >= 1,
        f"autonomous published {len(published)} plans with no human approval anywhere",
    )
    no_approval = await scalar(
        ctx,
        "SELECT count(*) FROM link_plan_transitions WHERE to_status IN ('awaiting_approval', "
        "'approved')",
    )
    out.kv("transitions through awaiting_approval / approved", no_approval)
    out.proves(
        "Autonomous publishing",
        f"{len(published)} links live, strict verification, no approval step",
    )
    ctx.state["published_main"] = len(published)
    ctx.state["sample_plan"] = plan_id


# --------------------------------------------------------------------------- #
# 14. Calibration contrast and plain AUTO_PUBLISH
# --------------------------------------------------------------------------- #
async def stage_calibration(ctx: Ctx) -> None:
    from ken_services.demo.synthetic import seed_calibration_fixture

    out = ctx.out
    out.stage(
        14, "Plain AUTO_PUBLISH needs a CALIBRATED class: before and after a synthetic calibration"
    )
    klass: str = ctx.state["roles"]["class"]
    members: list[PlanInfo] = ctx.state["roles"]["class_plans"]
    plan = members[0]
    out.note(f"decision class: {klass}   ({len(members)} plans)")
    out.note(f"plan: {plan.label}")
    _, engine, _ = _stack(ctx, "autonomous")
    pre = await engine.evaluate(plan_id=plan.id, mode=AutonomyMode.AUTONOMOUS)
    out.note(
        f"BEFORE seeding: decision={pre.decision.value} profile={pre.verification_profile.value} "
        f"calibration_version={pre.calibration_version} confidence={pre.confidence:.3f} "
        f"risk={pre.risk_tier.value}"
    )
    out.line()
    out.banner(
        [
            "SYNTHETIC CALIBRATION FIXTURE - not evidence about any real site",
            "A passing policy_evaluations row (runner_version 'demo-synthetic-calibration') "
            "written through the",
            "same tables the real learning pipeline writes. No review history exists; this stands "
            "in for one.",
        ]
    )
    seeded = await seed_calibration_fixture(ctx, decision_class=klass)
    out.kv("fixture evaluation id", seeded["evaluation_id"])
    out.kv(
        "bound to proposal (system-authored)",
        f"{seeded['proposal_id']} proposed_by={seeded['proposed_by']}",
    )
    out.kv(
        "samples / calibrated confidence / positive rate",
        f"{seeded['samples']} / {seeded['calibrated']} / {seeded['positive']}",
    )
    _, engine2, pub = _stack(ctx, "autonomous")
    post = await engine2.evaluate(plan_id=plan.id, mode=AutonomyMode.AUTONOMOUS)
    out.note(
        f"AFTER seeding : decision={post.decision.value} profile={post.verification_profile.value} "
        f"calibration_version={post.calibration_version} confidence={post.confidence:.3f} "
        f"risk={post.risk_tier.value}"
    )
    before_cms = ctx.cms.writes
    published = 0
    plain = 0
    for member in members:
        if member.confidence < 0.5:
            continue
        result = await pub.publish_plan(plan_id=member.id)
        rows = await _decisions(ctx, member.id)
        if result.outcome is PublishOutcome.PUBLISHED:
            published += 1
            if rows and rows[-1].decision == D.AUTO_PUBLISH.value:
                plain += 1
    deployment = await fetchone(
        ctx,
        "SELECT dep.status, dep.note FROM deployments dep JOIN deployment_items i ON "
        "i.deployment_id = dep.id "
        "WHERE i.plan_id = :p",
        p=plan.id,
    )
    attempt = await fetchone(ctx, "SELECT * FROM publish_attempts WHERE plan_id = :p", p=plan.id)
    out.kv("class plans published / as plain AUTO_PUBLISH", f"{published} / {plain}")
    deployment = need(deployment)
    attempt = need(attempt)
    out.kv("deployment status / profile", f"{deployment.status} / {attempt.verification_profile}")
    out.require(
        "4",
        pre.decision is D.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
        and pre.calibration_version is None
        and post.decision is D.AUTO_PUBLISH
        and post.calibration_version is not None
        and post.verification_profile.value == "standard"
        and plain >= 1
        and plain == published
        and attempt.verification_profile == "standard"
        and ctx.cms.writes > before_cms,
        f"same plan: extra verification before the SYNTHETIC calibration, plain AUTO_PUBLISH after "
        f"({plain} plans published under the standard profile)",
    )
    out.proves(
        "Calibration gate",
        "plain AUTO_PUBLISH is unreachable without a passing, sufficient evaluation",
    )
    _ = json
