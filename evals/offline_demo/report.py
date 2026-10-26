# ruff: noqa: E501  -- report layout strings
"""Management-facing interlinking report from the offline demo database.

    .venv/bin/python evals/offline_demo/report.py [--out docs/pilot/offline_demo_report]

Reads (never writes) the database named by ``KEN_DATABASE_URL`` and produces ``INTERLINKING_REPORT.md``,
``recommendations.csv`` and ``report.json``. Findings are SEPARATED BY PROVENANCE: every page carries a
label stamped at import (``synthetic_base``, ``synthetic_extension``, ``authorized_import``). Only
``authorized_import`` pages can ever appear under "authorised content"; an unlabelled page is reported as
unlabelled and is never counted as real. No synthetic number is an SEO outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "contracts" / "python")]

SYNTHETIC = {"synthetic_base", "synthetic_extension"}
AUTHORIZED = "authorized_import"
BANNER = (
    "> **SYNTHETIC DEMONSTRATION.** Every page analysed in this report is fictional (the reserved\n"
    "> `research.example.com` demo site). Nothing here is Ken Research content, and no figure below is\n"
    "> an SEO outcome: nothing was published, crawled or measured against real search traffic."
)
SHORT = "https://research.example.com"

PAGES_SQL = """
SELECT p.id::text AS id, p.canonical_url AS url, p.title, p.publication_state AS state, p.word_count,
       coalesce(v.metadata->>'provenance', 'unlabelled') AS provenance,
       v.metadata->>'situation' AS situation, v.metadata->>'cms_protected' AS cms_protected,
       a.page_type, a.market_id, a.geographies,
       coalesce(g.in_degree, 0) AS in_degree, coalesce(g.out_degree, 0) AS out_degree,
       coalesce(g.is_orphan, false) AS is_orphan, g.internal_pagerank AS pagerank,
       coalesce((SELECT true FROM page_protections pr WHERE pr.page_id = p.id AND pr.active LIMIT 1),
                false) AS protected
FROM pages p
JOIN page_versions v ON v.page_id = p.id AND v.version = p.current_version
LEFT JOIN page_analyses a ON a.page_id = p.id AND a.page_version = p.current_version
LEFT JOIN page_graph_metrics g ON g.page_id = p.id AND g.page_version = p.current_version
WHERE p.gone_at IS NULL
ORDER BY p.canonical_url
"""

PLANS_SQL = """
SELECT l.id::text AS plan_id, l.status, l.source_url, l.target_url, l.target_title, l.anchor_text,
       l.block_id, l.start_offset, l.end_offset, l.preview, l.score, l.total_score, l.confidence,
       l.reason_codes, l.channels, l.relationship_type, l.rationale,
       r.verdict AS critic_verdict, r.reasons AS critic_reasons, r.explanation AS critic_explanation,
       r.fallback_used AS critic_fallback, r.model AS critic_model,
       d.mode AS autonomy_mode, d.decision AS autonomy_decision, d.would_have_decision,
       d.risk_tier, d.confidence AS autonomy_confidence, d.blocks AS autonomy_blocks,
       d.critic_outcome
FROM link_plans l
LEFT JOIN LATERAL (SELECT * FROM agent_run_plans r WHERE r.plan_id = l.id
                   ORDER BY reviewed_at DESC LIMIT 1) r ON true
LEFT JOIN LATERAL (SELECT * FROM autonomy_decisions d WHERE d.plan_id = l.id
                   ORDER BY evaluated_at DESC LIMIT 1) d ON true
ORDER BY l.total_score DESC, l.id
"""


def short(url: str) -> str:
    return url.replace(SHORT, "") or "/"


def pct(num: int, den: int) -> str:
    return "n/a" if den == 0 else f"{100 * num / den:.0f}% ({num}/{den})"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append(
            "| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |"
        )
    return "\n".join(out)


async def load(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            who = await c.scalar(text("SELECT current_user || '@' || current_database()"))
            pages = [dict(r._mapping) for r in await c.execute(text(PAGES_SQL))]
            plans = [dict(r._mapping) for r in await c.execute(text(PLANS_SQL))]
            rejections = {
                r[0] or "(candidate became a plan)": r[1]
                for r in await c.execute(
                    text(
                        "SELECT rejection, count(*) FROM link_candidates GROUP BY 1 ORDER BY 2 DESC"
                    )
                )
            }
            links = {
                r[0]: r[1]
                for r in await c.execute(
                    text("SELECT link_type, count(*) FROM existing_links GROUP BY 1")
                )
            }
            opps = {
                r[0]: r[1]
                for r in await c.execute(
                    text("SELECT kind, count(*) FROM opportunities GROUP BY 1")
                )
            }
            side = {}
            for table in ("deployments", "deployment_items", "publish_attempts"):
                side[table] = await c.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            dec = {
                (r[0], r[1]): r[2]
                for r in await c.execute(
                    text(
                        "SELECT mode, would_have_decision, count(*) FROM autonomy_decisions "
                        "GROUP BY 1, 2"
                    )
                )
            }
    finally:
        await engine.dispose()
    return {
        "database": who,
        "pages": pages,
        "plans": plans,
        "rejections": rejections,
        "links": links,
        "opportunities": opps,
        "side_effects": side,
        "decisions": dec,
    }


def evaluation(data: dict[str, Any]) -> dict[str, Any]:
    """Gold-judgment metrics on SYNTHETIC BASE pages only (hand-written before the engine existed)."""
    from tests.unit.recommendation_evals.judgments import source_judgments

    judged = source_judgments()
    base_urls = {p["url"] for p in data["pages"] if p["provenance"] == "synthetic_base"}
    counts: Counter[str] = Counter()
    critic: Counter[str] = Counter()
    for plan in data["plans"]:
        if plan["source_url"] not in base_urls or plan["source_url"] not in judged:
            continue
        label = judged[plan["source_url"]].label(plan["target_url"])
        counts[label] += 1
        critic[(label, plan["critic_verdict"] or "none")] += 1  # type: ignore[index]
    total = sum(counts.values())
    relevant = counts["good"] + counts["acceptable"]
    bad_total = counts["bad"]
    return {
        "plans_judged_source": total,
        "labels": dict(counts),
        "precision_good_or_acceptable": pct(relevant, total),
        "bad_rate": pct(bad_total, total),
        "critic_by_label": {f"{k[0]}/{k[1]}": v for k, v in sorted(critic.items())},
        "bad_plans_accepted_by_critic": sum(
            v for (lab, verdict), v in critic.items() if lab == "bad" and verdict == "accept"
        ),
    }


def components(plan: dict[str, Any]) -> str:
    comps = (plan["score"] or {}).get("components", [])
    return ", ".join(f"{c['name']} {c['contribution']:.2f}" for c in comps if c["contribution"] > 0)


def build(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    pages, plans = data["pages"], data["plans"]
    by_prov = defaultdict(list)
    for p in pages:
        by_prov[p["provenance"]].append(p)
    real = by_prov.get(AUTHORIZED, [])
    unl = by_prov.get("unlabelled", [])
    published = [p for p in pages if p["state"] == "published"]
    out: list[str] = ["# Interlinking Report: offline shadow demonstration", "", BANNER, ""]
    out += [
        f"Database: `{data['database']}` · mode: **shadow** · CMS: **mock** · published: **nothing** "
        f"(deployments {data['side_effects']['deployments']}, publish attempts "
        f"{data['side_effects']['publish_attempts']}).",
        "",
        "## 1. What was analysed",
        "",
        md_table(
            ["Provenance", "Pages", "Meaning"],
            [
                [
                    "synthetic_base",
                    len(by_prov.get("synthetic_base", [])),
                    "fictional demo corpus (65 records, one duplicate URL merged)",
                ],
                [
                    "synthetic_extension",
                    len(by_prov.get("synthetic_extension", [])),
                    "11 fictional pages added for orphan / hub-spoke / protected / no-opportunity cases",
                ],
                [
                    "authorized_import",
                    len(real),
                    "real content the operator is authorised to use (none unless imported)",
                ],
                [
                    "unlabelled",
                    len(unl),
                    "imported without a label: NOT counted as real or as synthetic",
                ],
            ],
        ),
        "",
        f"{len(pages)} pages in total, {len(published)} published. Page types: "
        + ", ".join(
            f"{k} {v}"
            for k, v in sorted(Counter(p["page_type"] or "unanalysed" for p in pages).items())
        )
        + ".",
        "",
        "## 2. Internal-link architecture (existing links, before any recommendation)",
        "",
        f"Existing links: internal {data['links'].get('internal', 0)}, external "
        f"{data['links'].get('external', 0)}. Orphan pages (no internal link points to them): "
        f"{sum(1 for p in published if p['is_orphan'])} of {len(published)} published.",
        "",
    ]
    hubs = [p for p in pages if p["page_type"] == "hub"]
    out += [
        "**Hubs and what they link to**",
        "",
        md_table(
            ["Hub", "Outbound links", "Inbound links", "Provenance"],
            [[short(h["url"]), h["out_degree"], h["in_degree"], h["provenance"]] for h in hubs],
        ),
        "",
        "**Orphan and weakly linked pages**",
        "",
        md_table(
            ["Page", "Type", "Inbound", "Situation", "Provenance"],
            [
                [
                    short(p["url"]),
                    p["page_type"],
                    p["in_degree"],
                    p["situation"] or "",
                    p["provenance"],
                ]
                for p in pages
                if p["is_orphan"] or p["in_degree"] <= 1
            ][:40],
        ),
        "",
        "## 3. Orphan opportunities",
        "",
    ]
    orphan_urls = {p["url"] for p in published if p["is_orphan"]}
    to_orphans = [pl for pl in plans if pl["target_url"] in orphan_urls]
    fixed = {pl["target_url"] for pl in to_orphans if pl["critic_verdict"] == "accept"}
    out += [
        f"{len(orphan_urls)} published orphan pages; {len(to_orphans)} proposed links point to "
        f"{len({pl['target_url'] for pl in to_orphans})} of them; {len(fixed)} would receive a link that "
        "the critic accepted (the shadow engine publishes none).",
        "",
        md_table(
            ["Orphan target", "Proposed from", "Anchor", "Critic", "Shadow outcome"],
            [
                [
                    short(pl["target_url"]),
                    short(pl["source_url"]),
                    pl["anchor_text"],
                    pl["critic_verdict"],
                    pl["would_have_decision"]
                    or pl["autonomy_decision"]
                    or "none (critic did not accept)",
                ]
                for pl in to_orphans
            ],
        ),
        "",
        "## 4. Candidate generation and funnel",
        "",
        md_table(
            ["Outcome of a candidate pair", "Count"],
            [[k, v] for k, v in data["rejections"].items()],
        ),
        "",
        f"{len(plans)} link plans were created. Opportunity signals: "
        + ", ".join(f"{k} {v}" for k, v in sorted(data["opportunities"].items()))
        + ".",
        "",
        "## 5. Recommendations (every plan; the CSV carries all columns)",
        "",
        md_table(
            [
                "#",
                "Source",
                "Destination",
                "Anchor",
                "Placement context",
                "Score",
                "Why (top components)",
                "Critic",
                "Shadow outcome (would be)",
                "Confidence",
            ],
            [
                [
                    i + 1,
                    short(pl["source_url"]),
                    short(pl["target_url"]),
                    pl["anchor_text"],
                    f"…{(pl['preview'] or {}).get('before', '')[-45:]} [{pl['anchor_text']}] {(pl['preview'] or {}).get('after', '')[:45]}…",
                    f"{pl['total_score']:.2f}",
                    components(pl),
                    f"{pl['critic_verdict']}" + (" (fallback)" if pl["critic_fallback"] else ""),
                    f"{pl['would_have_decision'] or pl['autonomy_decision'] or 'not evaluated'}",
                    f"{(pl['autonomy_confidence'] if pl['autonomy_confidence'] is not None else pl['confidence']):.2f}",
                ]
                for i, pl in enumerate(plans)
            ],
        ),
        "",
        "## 6. Blocked or declined recommendations",
        "",
    ]
    blocked = [
        pl
        for pl in plans
        if pl["critic_verdict"] != "accept" or (pl["would_have_decision"] == "block")
    ]
    out += [
        md_table(
            [
                "Source → destination",
                "Anchor",
                "Critic",
                "Critic explanation",
                "Hard blocks",
                "Shadow outcome",
            ],
            [
                [
                    f"{short(pl['source_url'])} → {short(pl['target_url'])}",
                    pl["anchor_text"],
                    pl["critic_verdict"],
                    (pl["critic_explanation"] or "")[:120],
                    ", ".join(pl["autonomy_blocks"] or []) or "none",
                    pl["would_have_decision"]
                    or pl["autonomy_decision"]
                    or "not evaluated (critic did not accept)",
                ]
                for pl in blocked
            ],
        ),
        "",
        "Pairs the engine refused before they became plans are counted in section 4 "
        "(`target_protected`, `target_ineligible`, `target_excluded`, `already_linked`, "
        "`no_natural_anchor`, `below_min_relevance`).",
        "",
        "## 7. Pages with no valid linking opportunity",
        "",
    ]
    src = {pl["source_url"] for pl in plans}
    tgt = {pl["target_url"] for pl in plans}
    none_ = [
        p
        for p in published
        if p["url"] not in src and p["url"] not in tgt and p["page_type"] in {"report", "insight"}
    ]
    out += [
        md_table(
            ["Page", "Type", "Why (situation label / state)", "Provenance"],
            [
                [
                    short(p["url"]),
                    p["page_type"],
                    p["situation"]
                    or (
                        "protected"
                        if p["protected"]
                        else "no sufficiently relevant, unlinked mention"
                    ),
                    p["provenance"],
                ]
                for p in none_
            ],
        ),
        "",
        "**Designed situations (synthetic extension) versus what the engine did**",
        "",
        md_table(
            ["Extension page", "Designed situation", "Plans out", "Plans in", "Observed"],
            [
                [
                    short(p["url"]),
                    p["situation"],
                    sum(1 for pl in plans if pl["source_url"] == p["url"]),
                    sum(1 for pl in plans if pl["target_url"] == p["url"]),
                    "protected/draft: no plan targets it"
                    if (p["protected"] or p["state"] != "published")
                    and not any(pl["target_url"] == p["url"] for pl in plans)
                    else (
                        "engine proposed links"
                        if any(
                            pl["target_url"] == p["url"] or pl["source_url"] == p["url"]
                            for pl in plans
                        )
                        else "no plan found (expected opportunity NOT produced)"
                        if "opportunit" in (p["situation"] or "")
                        and "no valid" not in (p["situation"] or "")
                        else "no plan, as designed"
                    ),
                ]
                for p in pages
                if p["provenance"] == "synthetic_extension"
            ],
        ),
        "",
        "## 8. Evaluation (SYNTHETIC gold judgments, base corpus only)",
        "",
    ]
    ev = evaluation(data)
    out += [
        "The 65-page base corpus has hand-written GOOD / ACCEPTABLE / BAD target judgments that were "
        "written before the engine existed. They measure whether this software behaves as designed on "
        "fictional pages; they are **not** an estimate of SEO or of Ken Research page quality.",
        "",
        md_table(
            ["Metric", "Value"],
            [
                ["Plans on judged base sources", ev["plans_judged_source"]],
                ["Label mix", json.dumps(ev["labels"])],
                ["Precision (GOOD or ACCEPTABLE)", ev["precision_good_or_acceptable"]],
                ["BAD-target rate (must be 0)", ev["bad_rate"]],
                ["BAD plans the critic accepted (must be 0)", ev["bad_plans_accepted_by_critic"]],
                ["Critic verdict by gold label", json.dumps(ev["critic_by_label"])],
            ],
        ),
        "",
        "## 9. Shadow-mode safety",
        "",
        md_table(
            ["Check", "Result"],
            [
                [
                    "Deployments / deployment items / publish attempts",
                    json.dumps(data["side_effects"]),
                ],
                [
                    "Autonomy decisions recorded (mode, would-be decision, count)",
                    json.dumps({f"{k[0]}/{k[1]}": v for k, v in data["decisions"].items()}),
                ],
                [
                    "Every recorded decision is `shadow`",
                    str(all(k[0] == "shadow" for k in data["decisions"])),
                ],
            ],
        ),
        "",
        "## 10. Findings from authorised real content",
        "",
    ]
    if real:
        real_urls = {p["url"] for p in real}
        rp = [pl for pl in plans if pl["source_url"] in real_urls]
        out += [
            f"{len(real)} pages were imported with the `authorized_import` label; {len(rp)} plans have "
            "one of them as the source. Gold-judgment metrics do not exist for real content, so none "
            "are shown. Review these plans by hand before drawing any conclusion.",
            "",
            md_table(
                ["Source → destination", "Anchor", "Critic", "Shadow outcome"],
                [
                    [
                        f"{short(pl['source_url'])} → {short(pl['target_url'])}",
                        pl["anchor_text"],
                        pl["critic_verdict"],
                        pl["would_have_decision"]
                        or pl["autonomy_decision"]
                        or "none (critic did not accept)",
                    ]
                    for pl in rp
                ],
            ),
        ]
    else:
        out += [
            "**None.** No real or authorised content was imported into this run, so there are no real findings. "
            "Import a folder with `import-html-dir ... --provenance authorized_import` or a JSONL export "
            "(see `docs/pilot/OFFLINE_INTERLINKING_DEMO.md`)."
        ]
    if unl:
        out += [
            "",
            f"**{len(unl)} unlabelled pages** were found; they are excluded from every real-content statement.",
        ]
    out += [
        "",
        "## 11. What this does not show",
        "",
        "- No ranking, traffic, click, crawl-budget or revenue effect: nothing was published or measured.",
        "- Critic and ranking quality on real Ken Research pages is unmeasured; the corpus is short, templated "
        "and fictional, and the thresholds are uncalibrated (no review history exists).",
        "- See the evidence list in `docs/pilot/MANAGEMENT_DEMO_GUIDE.md`.",
        "",
    ]
    return "\n".join(out), plans


def write(out_dir: Path, data: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown, plans = build(data)
    (out_dir / "INTERLINKING_REPORT.md").write_text(markdown)
    fields = [
        "plan_id",
        "source_url",
        "target_url",
        "anchor_text",
        "block_id",
        "start_offset",
        "end_offset",
        "total_score",
        "confidence",
        "critic_verdict",
        "critic_fallback",
        "critic_explanation",
        "autonomy_mode",
        "autonomy_decision",
        "would_have_decision",
        "risk_tier",
        "autonomy_blocks",
        "reason_codes",
        "rationale",
    ]
    with (out_dir / "recommendations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for plan in plans:
            writer.writerow(
                {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in plan.items()}
            )
    summary = {
        "database": data["database"],
        "pages": len(data["pages"]),
        "plans": len(plans),
        "provenance": dict(Counter(p["provenance"] for p in data["pages"])),
        "side_effects": data["side_effects"],
        "evaluation_synthetic": evaluation(data),
    }
    (out_dir / "report.json").write_text(json.dumps(summary, indent=1, default=str))


def main() -> int:
    import os

    from ken_core.config import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "pilot" / "offline_demo_report")
    args = parser.parse_args()
    url = os.environ.get("KEN_DATABASE_URL") or Settings().database_url
    data = asyncio.run(load(url))
    write(args.out, data)
    print(
        json.dumps(
            {
                "report": str(args.out / "INTERLINKING_REPORT.md"),
                "plans": len(data["plans"]),
                "pages": len(data["pages"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
