"""Stages 16-17: learning, the proposal path, and the governance proof.

Nothing here promotes anything. The demo shows the whole path to the door -- evaluation,
reflection, a proposal, an offline evaluation -- and then the three independent locks on the
door: demonstration mode (service), frozen paths (service) and the database guards (raw SQL).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ken_contracts.learning import (
    AdvanceProposalRequest,
    PromotionRefusal,
    PromotionRefusedError,
    ProposalStatus,
    SkillSource,
    SkillStatus,
    TargetKind,
    is_frozen_path,
)
from ken_core.config import REPO_ROOT
from ken_core.db.models.learning import Skill
from ken_core.providers.model.mock import MockModelProvider
from ken_services.demo.context import Ctx, fetchall, fetchone, need, scalar
from ken_services.demo.synthetic import (
    REVIEW_WINDOW,
    SYNTHETIC_CLASS,
    seed_review_history,
)
from ken_services.learning.datasets import available_suites
from ken_services.learning.evaluate import run_evaluation
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.promote import DatabasePromotionService
from ken_services.learning.proposals import CandidateRefusedError, policy_change
from ken_services.learning.protocol import EvaluationRequest, ReflectionRequest
from ken_services.learning.reflect import _base_documents, run_reflection

__all__ = ["stage_governance", "stage_learning"]

EVAL_LINE: Final = re.compile(
    r"^\s+(HARD-FAIL\s+)?(\S+)\s+(\d+)/(\d+)\s+pass_rate=(\S+)\s+must_not=(\d+)\s*$"
)


async def _proposals(ctx: Ctx) -> int:
    return int(await scalar(ctx, "SELECT count(*) FROM learning_proposals") or 0)


async def _run_eval_runner(ctx: Ctx) -> tuple[int, list[str]]:
    """``evals/runner.py --suites all`` exactly as ``make eval`` runs it, as a subprocess, with
    the database pinned to the demo database by environment (never the developer's)."""
    ctx.config.workdir.mkdir(parents=True, exist_ok=True)
    report = ctx.config.workdir / "eval-all.json"
    env = {
        **os.environ,
        "KEN_DATABASE_URL": ctx.config.app_url,
        "KEN_MODEL_PROVIDER": "mock",
        "KEN_LOG_LEVEL": "WARNING",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(REPO_ROOT / "evals" / "runner.py"),
        "--suites",
        "all",
        "--report",
        str(report),
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    return int(process.returncode or 0), lines


async def stage_learning(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(16, "Learning: eval suites, reflection on real and on SYNTHETIC history, a proposal")
    policy = load_configured_learning_policy(ctx.settings)
    floor = policy.gates.min_sample_count

    # ---- the eval suites, via the real runner
    out.note("`evals/runner.py --suites all` (mock provider; recorded datasets; read-only report)")
    code, lines = await _run_eval_runner(ctx)
    suites: list[tuple[str, str, str, str]] = []
    gate = "unknown"
    reasons: list[str] = []
    for line in lines:
        matched = EVAL_LINE.match(line)
        if matched:
            suites.append(
                (
                    matched.group(2),
                    f"{matched.group(3)}/{matched.group(4)}",
                    matched.group(5),
                    matched.group(6),
                )
            )
        elif line.startswith("gate:"):
            gate = line.split(":", 1)[1].strip()
        elif line.strip().startswith("- "):
            reasons.append(line.strip())
    out.table(["suite", "passed/cases", "pass_rate", "must_not violations"], suites)
    out.kv("runner exit code", code)
    out.kv("gate", gate)
    for reason in reasons[:6]:
        out.note(f"  {reason}")
    if gate == "inconclusive":
        out.note(
            f"The gate is INCONCLUSIVE: some decision classes have fewer than {floor} samples, so "
            "the"
        )
        out.note(
            "suites cannot certify a policy change. That is the designed behaviour, not a failure."
        )
    all_suites = len(available_suites())

    # ---- reflection on the real demo history
    before = await _proposals(ctx)
    real = await run_reflection(
        sessionmaker=ctx.sessionmaker,
        settings=ctx.settings,
        policy=policy,
        request=ReflectionRequest(use_model=False, triggered_by="demo"),
        model_provider=None,
    )
    after = await _proposals(ctx)
    reviews = int(await scalar(ctx, "SELECT count(*) FROM review_actions") or 0)
    out.line()
    out.note("Reflection on the REAL demo history (autonomous publishing; no human review actions)")
    out.kv("review actions on record", reviews)
    out.kv(
        "counters",
        ", ".join(
            f"{k}={v}"
            for k, v in sorted(real.counters.items())
            if k
            in (
                "decision_classes",
                "samples_considered",
                "classes_below_sample_floor",
                "options_enumerated",
                "model_calls",
                "proposals_created",
            )
        ),
    )
    out.kv("proposals written (before -> after)", f"{before} -> {after}")
    out.note(
        f"INCONCLUSIVE: no human reviewed anything (autonomous publishing has no review step), "
        f"so no decision class reaches the {floor}-sample floor and reflection proposes nothing."
    )
    real_inconclusive = (
        real.proposal_ids == ()
        and after == before
        and real.counters.get("proposals_created", 0) == 0
    )

    # ---- reflection on SYNTHETIC history
    out.line()
    out.banner(
        [
            "SYNTHETIC REVIEW HISTORY - not evidence about any real site or any real reviewer",
            "34 reviewed plans of one decision class, dated June 2026, written through the review",
            "tables so that reflection clears the 30-sample floor and a proposal can be shown.",
        ]
    )
    history = await seed_review_history(ctx)
    out.kv(
        "synthetic plans / approved / rejected",
        f"{history['plans']} / {history['approved']} / {history['rejected']}",
    )
    synthetic = await run_reflection(
        sessionmaker=ctx.sessionmaker,
        settings=ctx.settings,
        policy=policy,
        request=ReflectionRequest(
            window=REVIEW_WINDOW,
            target_kinds=(TargetKind.RANKING,),
            use_model=False,
            triggered_by="demo",
        ),
        model_provider=None,
    )
    out.kv(
        "counters",
        ", ".join(
            f"{k}={v}"
            for k, v in sorted(synthetic.counters.items())
            if k
            in (
                "decision_classes",
                "samples_considered",
                "classes_below_sample_floor",
                "options_enumerated",
                "model_calls",
                "fallbacks",
                "proposals_created",
            )
        ),
    )
    proposal_id = synthetic.proposal_ids[0] if synthetic.proposal_ids else None
    proposal = None
    if proposal_id is not None:
        proposal = await fetchone(
            ctx,
            "SELECT id, kind, status, target_kind, proposed_by, fallback_used, title, body "
            "FROM learning_proposals WHERE id = :p",
            p=proposal_id,
        )
    if proposal is not None:
        paths = [c["path"] for c in proposal.body["changes"]]
        out.kv(
            "proposal",
            f"{proposal.kind}/{proposal.target_kind} status={proposal.status} "
            f"proposed_by={proposal.proposed_by}",
        )
        out.kv("proposal title", proposal.title)
        out.kv(
            "proposed changes",
            ", ".join(
                f"{c['path']}: {c['from_value']} -> {c['to_value']}"
                for c in proposal.body["changes"]
            ),
        )
        out.kv("chosen by", "deterministic rule (option 0): the mock provides no model choice")
        out.note(
            "change paths are learnable and none is frozen: "
            f"{not any(is_frozen_path(p) for p in paths)}"
        )

    # ---- offline evaluation for the proposal
    evaluation_id: uuid.UUID | None = None
    gate_result = "not run"
    if proposal_id is not None:
        outcome = await run_evaluation(
            sessionmaker=ctx.sessionmaker,
            settings=ctx.settings,
            policy=policy,
            request=EvaluationRequest(
                suites=available_suites(),
                proposal_id=proposal_id,
                provider_name="mock",
                seed=0,
                triggered_by="demo",
            ),
            model_provider=MockModelProvider(ctx.settings),
            now=datetime.now(UTC),
        )
        evaluation_id = outcome.evaluation_id
        gate_result = outcome.gate_result.value
        out.line()
        out.kv("offline evaluation bound to the proposal", f"gate={gate_result}")
        stored = await fetchone(
            ctx,
            "SELECT runner_version, mode, gate_result FROM policy_evaluations WHERE id = :e",
            e=evaluation_id,
        )
        stored = need(stored)
        out.kv(
            "policy_evaluations row",
            f"mode={stored.mode} runner={stored.runner_version} gate={stored.gate_result}",
        )
    ctx.state["proposal_id"] = proposal_id
    ctx.state["evaluation_id"] = evaluation_id
    ctx.state["eval_ok"] = code == 0 and len(suites) >= 1 and len(suites) == all_suites
    ctx.state["real_inconclusive"] = real_inconclusive
    ctx.state["synthetic_proposal"] = proposal is not None and proposal.status == "proposed"
    ctx.state["eval_gate"] = gate
    ctx.state["suites_run"] = len(suites)
    ctx.state["proposal_gate"] = gate_result
    ctx.out.proves(
        "Learning",
        f"{len(suites)} eval suites; reflection inconclusive on real history; proposal from "
        "synthetic history",
    )


async def _refused(ctx: Ctx, sql: str, **params: Any) -> tuple[bool, str]:
    """Run one raw statement the database must refuse. Returns (refused, 'sqlstate: message')."""
    try:
        async with ctx.sessionmaker() as session, session.begin():
            await session.execute(text(sql), params)
    except DBAPIError as exc:
        original = exc.orig
        state = getattr(original, "sqlstate", None) or getattr(
            getattr(original, "__cause__", None), "sqlstate", None
        )
        message = str(original).splitlines()[0] if str(original) else type(original).__name__
        message = message.split("DETAIL")[0].strip()
        return True, f"{state}: {message[:150]}"
    return False, "NOT refused"


async def stage_governance(ctx: Ctx) -> None:
    out = ctx.out
    out.stage(17, "Governance: promotion is a human act; every lock is shown holding")
    proposal_id: uuid.UUID | None = ctx.state.get("proposal_id")
    evaluation_id: uuid.UUID | None = ctx.state.get("evaluation_id")
    policy = load_configured_learning_policy(ctx.settings)
    service = DatabasePromotionService(
        sessionmaker=ctx.sessionmaker, settings=ctx.settings, policy=policy
    )

    # 1. demonstration mode (service)
    refused_code: str | None = None
    if proposal_id is not None and evaluation_id is not None:
        out.note(
            f"learning_demonstration_mode = {ctx.settings.learning_demonstration_mode} (the "
            "shipped default)"
        )
        try:
            await service.advance(
                principal=ctx.principal,
                proposal_id=proposal_id,
                request=AdvanceProposalRequest(
                    operation_id=str(uuid.uuid4()),
                    to_status=ProposalStatus.PROMOTED,
                    evaluation_run_id=evaluation_id,
                ),
            )
        except PromotionRefusedError as exc:
            refused_code = exc.refusal.value
        out.kv(
            "promotion attempt by an operator_admin", f"REFUSED with typed error {refused_code!r}"
        )
    status = await scalar(ctx, "SELECT status FROM learning_proposals WHERE id = :p", p=proposal_id)
    out.kv("proposal status after the attempt", status)

    # 2. frozen paths (service)
    documents, _ = _base_documents(ctx.settings)
    frozen: list[str] = []
    before = await _proposals(ctx)
    for path, kind in (
        ("recommendation.placement", TargetKind.RECOMMENDATION),
        ("recommendation.selection.max_links_per_block", TargetKind.RECOMMENDATION),
    ):
        try:
            policy_change(
                path=path,
                base_document=documents[kind],
                to_value=99,
                target_kind=kind,
                policy=policy,
            )
        except CandidateRefusedError as exc:
            frozen.append(f"{path} -> {exc.refusal.value}")
    after = await _proposals(ctx)
    out.kv("proposals touching frozen paths", "; ".join(frozen) or "NOT refused")
    out.kv("proposal rows before -> after", f"{before} -> {after}")

    # 3. raw SQL (database)
    out.line()
    out.note("Raw SQL against the database, with no promotion service and no promoter setting:")
    skill_id = uuid.uuid4()
    async with ctx.sessionmaker() as session, session.begin():
        session.add(
            Skill(
                id=skill_id,
                name=f"demo_governance_probe_{skill_id.hex[:10]}",
                version=1,
                description=(
                    "SYNTHETIC demo probe: a DRAFT skill that exists only to be refused promotion"
                ),
                instructions="none",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                suites=["protected-content"],
                model_role="reasoning",
                source=SkillSource.HUMAN.value,
                status=SkillStatus.DRAFT.value,
                created_at=datetime.now(UTC),
            )
        )
    raw: list[tuple[str, bool, str]] = []
    if proposal_id is not None:
        ok1, msg1 = await _refused(
            ctx, "UPDATE learning_proposals SET status = 'promoted' WHERE id = :p", p=proposal_id
        )
        raw.append(("UPDATE learning_proposals SET status = 'promoted'", ok1, msg1))
        ok2, msg2 = await _refused(
            ctx,
            "INSERT INTO learning_proposal_transitions (proposal_id, from_status, to_status, "
            "actor) "
            "VALUES (:p, 'proposed', 'promoted', 'user:demo-autonomy-service')",
            p=proposal_id,
        )
        raw.append(("INSERT ... learning_proposal_transitions to 'promoted'", ok2, msg2))
    ok3, msg3 = await _refused(
        ctx, "UPDATE skills SET status = 'promoted' WHERE id = :s", s=skill_id
    )
    raw.append(("UPDATE skills SET status = 'promoted'", ok3, msg3))
    for statement, refused, message in raw:
        out.note(f"  {statement}")
        out.note(f"      -> {'REFUSED' if refused else 'ACCEPTED (!)'} by PostgreSQL ({message})")
    promoted_proposals = int(
        await scalar(ctx, "SELECT count(*) FROM learning_proposals WHERE status = 'promoted'") or 0
    )
    promoted_skills = int(
        await scalar(ctx, "SELECT count(*) FROM skills WHERE status = 'promoted'") or 0
    )
    promoted_transitions = int(
        await scalar(
            ctx, "SELECT count(*) FROM learning_proposal_transitions WHERE to_status = 'promoted'"
        )
        or 0
    )
    out.kv(
        "promoted proposals / skills / transitions",
        f"{promoted_proposals} / {promoted_skills} / {promoted_transitions}",
    )

    out.require(
        "8",
        bool(ctx.state.get("eval_ok"))
        and bool(ctx.state.get("real_inconclusive"))
        and bool(ctx.state.get("synthetic_proposal"))
        and ctx.state.get("proposal_gate") in ("pass", "inconclusive", "fail")
        and refused_code == PromotionRefusal.DEMONSTRATION_MODE.value
        and len(frozen) == 2
        and after == before
        and promoted_proposals == 0
        and promoted_skills == 0,
        f"{ctx.state.get('suites_run')} suites run, gate={ctx.state.get('eval_gate')}; "
        "real-history reflection "
        f"inconclusive (0 proposals); synthetic history -> 1 proposal, offline eval gate="
        f"{ctx.state.get('proposal_gate')}; promotion refused: {refused_code}; 2 frozen-path "
        "changes refused",
    )
    out.require(
        "9",
        len(raw) >= 3 and all(r[1] for r in raw) and promoted_transitions == 0,
        f"{sum(1 for r in raw if r[1])}/{len(raw)} raw promotion statements refused by PostgreSQL; "
        "0 promoted rows exist",
    )
    out.proves(
        "Governance", "demonstration mode, frozen paths and database guards each refuse a promotion"
    )
    _ = (json, Path, fetchall, SYNTHETIC_CLASS)
