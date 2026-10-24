"""Recommendation-engine benchmark with channel and component ablations (Phase 3, ADR-0011).

    .venv/bin/python evals/recommendations/benchmark.py                    # mock (default)
    .venv/bin/python evals/recommendations/benchmark.py --provider ollama  # local nomic-embed-text

Runs the production flow on the TEST database only (the database name must end in
``_test``; anything else is refused because the benchmark truncates tables):

    migrate to head -> truncate -> ingest the demo JSONL -> run_analysis -> for each config:
        truncate the recommendation tables -> run_recommendations -> score the plans

Configurations: ``all`` (policy v1 as committed); ``no_<channel>`` for each retrieval channel
(a policy COPY with ``retrieval.channels_enabled`` minus that channel); ``graph_weight_0``
(a ranking COPY with the ``graph`` complementarity weight set to 0 and the other weights
renormalised to sum to 1). Policy copies live in memory only: no policy file is written,
proposed or promoted. Finally ``all`` is re-run after a FRESH re-ingestion to check that the
plans do not depend on the (random) page ids the database mints.

Prints one JSON report and writes it to ``evals/recommendations/results/<name>.json``
(``mock.json`` / ``ollama-nomic-embed-text.json``). Model access goes only through
``build_model_provider(Settings(...))``; in ``ollama`` mode the only network target is the
local daemon at ``http://127.0.0.1:11434``. Offline eval (RUN_OFFLINE_EVAL, ALLOW).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import os
import platform
import sys
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
CHANNELS: Final = ("semantic", "lexical", "taxonomy", "graph")


def _import(name: str) -> Any:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module(name)


def _settings(provider: str, database_url: str) -> Any:
    from ken_core.config import Settings

    flow = _import("tests.unit.recommendation_evals.db_flow")
    flow.require_test_database(database_url)
    # vector_store="memory": an offline benchmark must never write into the shared Qdrant index.
    overrides: dict[str, Any] = {
        "model_provider": provider,
        "database_url": database_url,
        "vector_store": "memory",
    }
    if provider == "ollama":
        overrides["ollama_base_url"] = LOCAL_OLLAMA
    else:
        overrides["similarity_min_score"] = flow.MOCK_SIMILARITY_MIN_SCORE
    settings = Settings(_env_file=None, **overrides)
    host = urlsplit(settings.ollama_base_url).hostname
    if provider == "ollama" and host != "127.0.0.1":
        raise SystemExit(f"refusing non-local Ollama base URL host {host!r}")
    return settings


def _migrate(database_url: str) -> None:
    """Alembic upgrade to head. Must run OUTSIDE an event loop (env.py uses asyncio.run)."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def _configs(policy: Any, ranking: Any) -> dict[str, tuple[Any, Any]]:
    from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy

    out: dict[str, tuple[Any, Any]] = {"all": (policy, ranking)}
    for channel in CHANNELS:
        data = policy.model_dump(mode="json")
        data["retrieval"]["channels_enabled"] = [c for c in CHANNELS if c != channel]
        data["status"] = f"ablation-no-{channel}"
        out[f"no_{channel}"] = (RecommendationPolicy.model_validate(data), ranking)
    for channel in CHANNELS:
        data = policy.model_dump(mode="json")
        data["retrieval"]["channels_enabled"] = [channel]
        data["status"] = f"ablation-only-{channel}"
        out[f"only_{channel}"] = (RecommendationPolicy.model_validate(data), ranking)
    weights = dict(ranking.weights)
    rest = math.fsum(w for k, w in weights.items() if k != "graph")
    renormalised = {k: (0.0 if k == "graph" else w / rest) for k, w in weights.items()}
    data = ranking.model_dump(mode="json")
    data.update(weights=renormalised, status="ablation-graph-weight-0")
    out["graph_weight_0"] = (policy, RankingPolicy.model_validate(data))
    return out


def _plan_keys(plans: list[Any]) -> set[tuple[str, str, str, int]]:
    return {(p.source_url, p.target_url, p.anchor_text, p.start) for p in plans}


def _labelled(keys: set[tuple[str, str, str, int]], jm: Any) -> list[str]:
    judged = jm.source_judgments()
    out = []
    for source, target, anchor, _start in sorted(keys):
        verdict = judged.get(source)
        label = verdict.label(target) if verdict else "unjudged"
        out.append(f"{_short(source)} -> {_short(target)} [{anchor!r}] {label}")
    return out


def _short(url: str) -> str:
    return url.removeprefix("https://research.example.com")


def _graph_values(plans: list[Any], weight: float) -> dict[str, int]:
    if weight <= 0:
        return {}
    values = Counter(round(p.components.get("graph", 0.0) / weight, 3) for p in plans)
    return {str(k): v for k, v in sorted(values.items())}


async def _evaluate(flow: Any, jm: Any, sessionmaker: Any, settings: Any, provider: Any,
                    policy: Any, ranking: Any, taxonomy: Any) -> dict[str, Any]:  # fmt: skip
    await flow.execute(sessionmaker, flow.TRUNCATE_RECOMMENDATIONS)
    began = time.perf_counter()
    run = await flow.recommend(sessionmaker, settings, provider, policy=policy, ranking=ranking)
    elapsed = round((time.perf_counter() - began) * 1000.0, 1)
    plans = await flow.load_plans(sessionmaker)
    pages = await flow.load_pages(sessionmaker)
    metrics = jm.quality_metrics(
        plans, policy=policy, taxonomy=taxonomy, target_markets=flow.target_markets(pages)
    )
    violations = jm.must_not_violations(plans, pages, policy=policy, taxonomy=taxonomy)
    counters = {k: v for k, v in run.counters.items() if v}
    return {
        "status": run.status.value,
        "run_ms": elapsed,
        "counters": counters,
        "must_not_violations": {k: len(v) for k, v in violations.items()},
        "metrics": metrics,
        "graph_component_values": _graph_values(plans, ranking.weights.get("graph", 0.0)),
        "_plans": plans,
    }


async def run(provider_name: str, database_url: str) -> dict[str, Any]:
    from ken_core.db.engine import create_engine
    from ken_core.db.session import create_sessionmaker
    from ken_core.providers.model.factory import build_model_provider
    from ken_services.intelligence.taxonomy import load_taxonomy
    from ken_services.recommendations.pipeline import load_policies

    flow = _import("tests.unit.recommendation_evals.db_flow")
    jm = _import("tests.unit.recommendation_evals.judgments")
    settings = _settings(provider_name, database_url)
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    provider = build_model_provider(settings)
    try:
        caps = await provider.capabilities()
        embed_model = next((r.model for r in caps.roles if r.role.value == "embedding"), None)
        policy, ranking = load_policies(settings)
        taxonomy = load_taxonomy(settings.taxonomy_path)

        async def fresh_corpus() -> dict[str, Any]:
            await flow.execute(sessionmaker, flow.TRUNCATE_EVERYTHING)
            began = time.perf_counter()
            ingest = await flow.ingest_corpus(sessionmaker, jm.CORPUS_PATH)
            analysis = await flow.analyse(sessionmaker, settings, provider)
            return {
                "ingest": ingest.counters.model_dump(mode="json"),
                "analysis": {k: v for k, v in analysis.counters.items() if v},
                "analysis_status": analysis.status.value,
                "setup_ms": round((time.perf_counter() - began) * 1000.0, 1),
            }

        setup = await fresh_corpus()
        configs = _configs(policy, ranking)
        results: dict[str, dict[str, Any]] = {}
        for name, (pol, rank) in configs.items():
            results[name] = await _evaluate(
                flow, jm, sessionmaker, settings, provider, pol, rank, taxonomy
            )
        # Determinism across a fresh ingestion (new random page ids).
        await fresh_corpus()
        repeat = await _evaluate(
            flow, jm, sessionmaker, settings, provider, policy, ranking, taxonomy
        )
    finally:
        await provider.aclose()
        await engine.dispose()

    base = _plan_keys(results["all"]["_plans"])
    again = _plan_keys(repeat["_plans"])
    summary_keys = (
        "plans", "sources_with_plans", "precision_at_3_macro", "precision_at_3_micro",
        "recall_good_macro", "bad_rate", "unjudged_rate", "anchor_exact_match",
        "anchor_pairs_proposed", "anchor_names_target_market_rate", "mean_total_score",
    )  # fmt: skip
    table: dict[str, dict[str, Any]] = {}
    for name, result in results.items():
        keys = _plan_keys(result["_plans"])
        table[name] = {k: result["metrics"][k] for k in summary_keys}
        table[name]["must_not_violations"] = sum(result["must_not_violations"].values())
        table[name]["plans_vs_all"] = {
            "same": len(keys & base),
            "only_here": len(keys - base),
            "only_in_all": len(base - keys),
        }
        if name != "all" and keys != base:
            result["diff_vs_all"] = {
                "only_here": _labelled(keys - base, jm),
                "only_in_all": _labelled(base - keys, jm),
            }
    for result in (*results.values(), repeat):
        result.pop("_plans")
    all_metrics = results["all"]["metrics"]
    return {
        "benchmark": "ken-phase3-recommendations",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": {"python": platform.python_version(), "machine": platform.machine()},
        "provider": provider_name,
        "embedding_model": embed_model,
        "semantic_floor": settings.similarity_min_score,
        "policy": {
            "version": policy.version,
            "ranking_version": ranking.version,
            "ranking_weights": dict(ranking.weights),
        },
        "corpus": {
            "file": str(jm.CORPUS_PATH.relative_to(REPO_ROOT)),
            "judgments": str(jm.JUDGMENTS_PATH.relative_to(REPO_ROOT)),
            "judged_sources": all_metrics["judged_sources"],
        },
        "setup": setup,
        "summary": table,
        "determinism_after_reingest": {
            "identical": base == again,
            "only_first": _labelled(base - again, jm),
            "only_second": _labelled(again - base, jm),
            "first_counters": results["all"]["counters"],
            "second_counters": repeat["counters"],
            "second_metrics": {k: repeat["metrics"][k] for k in summary_keys},
        },
        "configs": {
            name: {k: v for k, v in result.items() if k != "metrics"}
            | {"metrics": {k: v for k, v in result["metrics"].items() if k != "per_source"}}
            for name, result in results.items()
        },
        "per_source_all": all_metrics["per_source"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 recommendation benchmark")
    parser.add_argument("--provider", choices=("mock", "ollama"), default="mock")
    parser.add_argument("--output", type=Path, help="report path (default: results/<name>.json)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("KEN_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
        help="TEST database URL (the name must end in _test)",
    )
    args = parser.parse_args(argv)
    _settings(args.provider, args.database_url)  # refuse a non-test database before migrating
    _migrate(args.database_url)
    report = asyncio.run(run(args.provider, args.database_url))
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    sys.stdout.write(text)
    default = "mock.json" if args.provider == "mock" else "ollama-nomic-embed-text.json"
    output = args.output if args.output is not None else RESULTS / default
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
