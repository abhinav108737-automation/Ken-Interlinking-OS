"""Agent critic benchmark over the demo corpus (Phase 4, ADR-0012). Report only, no floors.

    .venv/bin/python evals/agents/benchmark.py                    # mock: the rule baseline
    .venv/bin/python evals/agents/benchmark.py --provider ollama  # the local REASONING model

Runs the production flow on the TEST database only (the database name must end in ``_test``;
anything else is refused because the benchmark truncates tables)::

    migrate to head -> truncate -> ingest the demo JSONL -> run_analysis (MockModelProvider
    embeddings, so the plans are exactly the recorded fixture's) -> run_recommendations ->
    ONE agent batch over every source with unreviewed proposed plans

The agent policy is ``policies/agents/agents.v1.yaml`` as committed (planner off, critic on,
``max_plans_per_run`` 3), except that ``--provider mock`` DISABLES the critic model: the mock
cannot judge anything, so the mock run is the deterministic-rule baseline the model is
compared with. In ``ollama`` mode the critic is the provider's REASONING role (whatever
``KEN_MODEL_REASONING`` resolves to, default ``qwen3:8b-q4_K_M``; no ``.env`` is read) and the
only network target is ``http://127.0.0.1:11434``.

Reported: verdict distribution; fallback and schema-violation rates (per critic call and per
plan); critic latency; agreement with the deterministic rule; agreement with the Phase 3
GOOD / ACCEPTABLE / BAD judgments (``tests/fixtures/demo/recommendation_judgments.json``);
the hard-gate check of ``tests/unit/agent_evals/flow.py``. Writes
``evals/agents/results/<name>.json``. Offline eval (RUN_OFFLINE_EVAL, ALLOW): nothing is
proposed, promoted or learned.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
RESULTS: Final = Path(__file__).resolve().parent / "results"
LOCAL_OLLAMA: Final = "http://127.0.0.1:11434"
DEFAULT_TEST_DATABASE_URL: Final = "postgresql+asyncpg://ken:ken@localhost:5433/ken_interlink_test"
SITE: Final = "https://research.example.com"
HANDED: Final = frozenset({"accept", "uncertain"})


def _import(name: str) -> Any:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module(name)


def _short(value: str) -> str:
    return value.removeprefix(SITE)


def _migrate(database_url: str) -> None:
    """Alembic upgrade to head. Must run OUTSIDE an event loop (env.py uses asyncio.run)."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def _critic_provider(provider_name: str, database_url: str) -> Any:
    """The critic's provider: None for mock (rule baseline), else the LOCAL Ollama daemon."""
    if provider_name == "mock":
        return None
    from ken_core.config import Settings
    from ken_core.providers.model.factory import build_model_provider

    settings = Settings(
        _env_file=None,
        model_provider="ollama",
        ollama_base_url=LOCAL_OLLAMA,
        database_url=database_url,
    )
    host = urlsplit(settings.ollama_base_url).hostname
    if host != "127.0.0.1":
        raise SystemExit(f"refusing non-local Ollama base URL host {host!r}")
    return build_model_provider(settings)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _rate(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(part / whole, 4)


async def run(provider_name: str, database_url: str) -> dict[str, Any]:
    from ken_contracts.agents import AgentNode, FallbackReason
    from ken_core.db.engine import create_engine
    from ken_core.db.session import create_sessionmaker

    flow = _import("tests.unit.agent_evals.flow")
    cases = _import("tests.unit.agent_evals.cases")
    jm = _import("tests.unit.recommendation_evals.judgments")

    settings = flow.settings_for(database_url)  # refuses a non-_test database
    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    critic = _critic_provider(provider_name, database_url)
    model_name: str | None = None
    try:
        if critic is not None:
            caps = await critic.capabilities()
            model_name = next((r.model for r in caps.roles if r.role.value == "reasoning"), None)
        began = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="ken-agent-bench-") as scratch:
            corpus = flow.write_lines(Path(scratch), flow.corpus_lines(), "demo.jsonl")
            await flow.seed(sm, settings, corpus)
        setup_ms = round((time.perf_counter() - began) * 1000.0, 1)
        base = flow.base_policy(settings)
        policy = flow.agent_policy(
            base,
            cases.Policy(
                planner=False,
                critic=critic is not None,
                max_plans_per_run=base.max_plans_per_run,
            ),
        )
        tools = flow.backend(sm, settings)
        began = time.perf_counter()
        detail, results = await flow.run_batch(
            sm,
            settings,
            backend=tools,
            provider=critic,
            policy=policy,
            sources=None,
            triggered_by="eval:agents-benchmark",
        )
        batch_ms = round((time.perf_counter() - began) * 1000.0, 1)
        gates = await flow.gate_violations(
            sm, tools, results, expired_by_hook=frozenset(), expected_model=model_name
        )
        ids = await flow.page_ids(sm)
        by_id = {v: k for k, v in ids.items()}
        plans_by_id: dict[Any, Any] = {}
        for source in {by_id[r.source_page_id] for r in results}:
            for plan in await flow.source_plans(sm, source):
                plans_by_id[plan.id] = (source, plan)
    finally:
        if critic is not None:
            await critic.aclose()
        await engine.dispose()

    judged = jm.source_judgments()
    threshold = policy.critic.accept_min_confidence
    per_plan: list[dict[str, Any]] = []
    call_fallbacks: Counter[str] = Counter()
    latencies: list[float] = []
    outcomes: Counter[str] = Counter()
    for result in results:
        outcomes[result.outcome.value] += 1
        step = next((s for s in result.steps if s.node is AgentNode.CRITIQUE), None)
        if step is not None:
            latencies.append(float(step.duration_ms))
            call_fallbacks[step.fallback_reason.value if step.fallback_reason else "none"] += 1
            if model_name is None and step.model:
                model_name = step.model
        for review in result.state.critic_result if result.state else ():
            source, plan = plans_by_id[review.plan_id]
            verdict = review.verdict.value
            rule = cases.rule_verdict(plan.confidence, threshold).value
            label = judged[source].label(plan.target_url) if source in judged else "unjudged"
            per_plan.append(
                {
                    "source": _short(source),
                    "target": _short(plan.target_url),
                    "anchor": plan.anchor,
                    "confidence": round(plan.confidence, 4),
                    "label": label,
                    "rule": rule,
                    "verdict": verdict,
                    "reasons": [r.value for r in review.reasons],
                    "fallback": review.fallback_reason.value if review.fallback_reason else None,
                    "explanation": review.explanation,
                }
            )

    model_plans = [p for p in per_plan if p["fallback"] is None]
    judged_plans = [p for p in per_plan if p["label"] != "unjudged"]

    def agrees(p: dict[str, Any]) -> bool:
        return bool((p["verdict"] == "reject") == (p["label"] == "bad"))

    confusion: dict[str, Counter[str]] = {}
    for p in per_plan:
        confusion.setdefault(p["label"], Counter())[p["verdict"]] += 1
    calls = sum(call_fallbacks.values())
    bad_total = sum(p["label"] == "bad" for p in judged_plans)
    bad_rejected = sum(p["verdict"] == "reject" for p in judged_plans if p["label"] == "bad")
    model_problem = sum(
        n
        for k, n in call_fallbacks.items()
        if k not in ("none", FallbackReason.MODEL_DISABLED.value)
    )
    summary = {
        "sources": len(results),
        "plans_reviewed": len(per_plan),
        "plans_handed_off": detail.counters.get("plans_handed_off", 0),
        "outcomes": dict(outcomes),
        "verdicts": dict(Counter(p["verdict"] for p in per_plan)),
        "critique_steps": calls,
        "step_fallback_reasons": dict(call_fallbacks),
        "step_fallback_rate": _rate(model_problem, calls),
        "step_schema_violation_rate": _rate(call_fallbacks.get("schema_violation", 0), calls),
        "plan_fallback_reasons": dict(Counter(p["fallback"] or "model" for p in per_plan)),
        "plans_answered_by_model": len(model_plans),
        "critic_latency_ms": {
            "mean": round(statistics.fmean(latencies), 1) if latencies else None,
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "agreement_with_rule": _rate(
            sum(p["verdict"] == p["rule"] for p in per_plan), len(per_plan)
        ),
        "agreement_with_rule_model_answered": _rate(
            sum(p["verdict"] == p["rule"] for p in model_plans), len(model_plans)
        ),
        "agreement_with_judgments": _rate(sum(agrees(p) for p in judged_plans), len(judged_plans)),
        "good_or_acceptable_handed_off_rate": _rate(
            sum(p["verdict"] in HANDED for p in judged_plans if p["label"] != "bad"),
            sum(p["label"] != "bad" for p in judged_plans),
        ),
        "bad_rejected": f"{bad_rejected}/{bad_total}",
        "good_accepted_rate": _rate(
            sum(p["verdict"] == "accept" for p in judged_plans if p["label"] == "good"),
            sum(p["label"] == "good" for p in judged_plans),
        ),
        "confusion_label_x_verdict": {k: dict(v) for k, v in sorted(confusion.items())},
        "gate_violations": len(gates),
        "runs_failed": detail.counters.get("runs_failed", 0),
    }
    return {
        "benchmark": "ken-phase4-agent-critic",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": {"python": platform.python_version(), "machine": platform.machine()},
        "provider": provider_name,
        "critic": "deterministic rule (critic model disabled)"
        if provider_name == "mock"
        else "model",
        "reasoning_model": model_name if provider_name != "mock" else None,
        "agent_policy": {
            "version": policy.version,
            "accept_min_confidence": threshold,
            "max_plans_per_run": policy.max_plans_per_run,
            "critic_timeout_seconds": policy.critic.timeout_seconds,
            "planner_model": policy.planner.use_model,
            "critic_model": policy.critic.use_model,
        },
        "corpus": "tests/fixtures/demo/research_site.jsonl (MockModelProvider embeddings)",
        "timing_ms": {"setup": setup_ms, "agent_batch": batch_ms},
        "summary": summary,
        "gates": gates,
        "plans": per_plan,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4 agent critic benchmark")
    parser.add_argument("--provider", choices=("mock", "ollama"), default="mock")
    parser.add_argument("--output", type=Path, help="report path (default: results/<name>.json)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("KEN_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
        help="TEST database URL (the name must end in _test)",
    )
    args = parser.parse_args(argv)
    flow = _import("tests.unit.agent_evals.flow")
    flow.settings_for(args.database_url)  # refuse a non-test database before migrating
    _migrate(args.database_url)
    report = asyncio.run(run(args.provider, args.database_url))
    text = json.dumps(report, indent=2) + "\n"
    sys.stdout.write(json.dumps(report["summary"], indent=2) + "\n")
    if args.output is not None:
        output = args.output
    elif args.provider == "mock":
        output = RESULTS / "mock.json"
    else:
        output = RESULTS / f"ollama-{str(report['reasoning_model']).replace(':', '-')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    sys.stdout.write(f"wrote {output}\n")
    return 0 if not report["gates"] and report["summary"]["runs_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
