"""Retrieval / embedding benchmark over the Phase 2 demo corpus.

    .venv/bin/python evals/intelligence/benchmark.py                    # mock (default)
    .venv/bin/python evals/intelligence/benchmark.py --provider ollama  # local nomic-embed-text
    ... --output evals/intelligence/results/mock.json

Prints one JSON report: provider, the PROVIDER-REPORTED embedding model (from
``capabilities()`` and from every ``EmbeddingResult``), the dimension taken from
the result (never assumed), per-batch embedding latency, and recall@5/@10, MRR,
mean ranks and pairwise accuracy for the taxonomy, embedding and combined
channels over ``tests/fixtures/demo/relatedness.json``.

Model access goes ONLY through ``build_model_provider(Settings(...))``. In
``ollama`` mode the only network target is the local daemon at
``http://127.0.0.1:11434``; the script refuses any other base URL and reads no
``.env``. No paid API, no external service, nothing is stored: it is an
offline eval (RUN_OFFLINE_EVAL, ALLOW) that writes a report and changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from urllib.parse import urlsplit

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
LOCAL_OLLAMA: Final = "http://127.0.0.1:11434"


def _demo() -> ModuleType:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("tests.unit.intelligence_evals.demo_corpus")


def _settings(provider: str) -> Any:
    from ken_core.config import Settings

    overrides: dict[str, Any] = {"model_provider": provider}
    if provider == "ollama":
        overrides["ollama_base_url"] = LOCAL_OLLAMA
    settings = Settings(_env_file=None, **overrides)
    host = urlsplit(settings.ollama_base_url).hostname
    if provider == "ollama" and host != "127.0.0.1":
        raise SystemExit(f"refusing non-local Ollama base URL host {host!r}")
    return settings


async def run(provider_name: str) -> dict[str, Any]:
    from ken_core.providers.model.factory import build_model_provider

    demo = _demo()
    settings = _settings(provider_name)
    rel = demo.load_relatedness()
    provider = build_model_provider(settings)
    try:
        embedding = await demo.embed_pages(
            provider,
            rel.pool,
            max_chars=settings.embedding_max_chars,
            prefix=settings.embedding_document_prefix,
            batch_size=settings.embedding_batch_size,
        )
    finally:
        await provider.aclose()
    metrics = demo.evaluate_all(embedding.vectors)
    latencies = embedding.batch_latency_ms
    return {
        "benchmark": "ken-phase2-demo-retrieval",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": {"python": platform.python_version(), "machine": platform.machine()},
        "provider": embedding.provider,
        "model": embedding.model,
        "capabilities_model": embedding.capabilities_model,
        "identity_consistent": embedding.model == embedding.capabilities_model,
        "dimensions": embedding.dimensions,
        "recipe": {
            "max_chars": settings.embedding_max_chars,
            "document_prefix": settings.embedding_document_prefix,
            "batch_size": settings.embedding_batch_size,
        },
        "corpus": {
            "file": str(demo.CORPUS_PATH.relative_to(REPO_ROOT)),
            "pages": len(demo.load_pages()),
            "pool": len(rel.pool),
            "labelled_pairs": len(rel.labels),
            "taxonomy": f"v{demo.taxonomy().version} ({demo.taxonomy().source})",
        },
        "embedding_latency_ms": {
            "batches": len(latencies),
            "per_batch": latencies,
            "mean_per_batch": round(sum(latencies) / len(latencies), 3),
            "total": round(sum(latencies), 3),
        },
        "channels": {name: m.as_dict() for name, m in metrics.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2 demo-corpus retrieval benchmark")
    parser.add_argument("--provider", choices=("mock", "ollama"), default="mock")
    parser.add_argument("--output", type=Path, help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    # Corpus loading and analysis use their own event loop; do them before ours starts.
    demo = _demo()
    demo.load_pages()
    demo.analyses()
    report = asyncio.run(run(args.provider))
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    sys.stdout.write(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
