"""Phase 3 integration support: a small synthetic corpus run through the REAL pipeline.

Pages are written as a JSONL export (docs/CONTRACTS.md section 13) and imported with the
real file source and normaliser; the real Phase 2 analysis (real taxonomy policy, real
deterministic analyser) and the deterministic ``MockModelProvider`` embeddings follow; the
recommendation engine then runs with workstream A's real filter / score / placement modules.

Every report mentions OTHER markets naturally (e.g. the EV report says "lithium-ion
batteries"), so placements are real: an anchor is always text that is already there.
All content is fictional; the site is the reserved ``site.test`` host.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import IngestionRunDetail
from ken_contracts.jobs import JobRunDetail
from ken_contracts.recommendations import (
    ENGINE_VERSION,
    LinkPlan,
    LinkPlanStatus,
    PlacementLocator,
    PlacementPreview,
    RetrievalChannel,
    ScoreBreakdown,
    ScoreComponent,
)
from ken_core.db.repositories import recommendations as repo
from ken_core.providers.model.mock import MockModelProvider
from ken_services.intelligence.pipeline import run_analysis
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.ingest import run_ingestion
from ken_services.inventory.sources.file_export import FileExportContentSource
from ken_services.recommendations.pipeline import run_recommendations
from tests.conftest import make_settings

SM = async_sessionmaker[AsyncSession]
SITE: Final = "https://site.test"
SETTINGS: Final = make_settings(similarity_min_score=0.3, mock_embedding_dim=256)

EV: Final = "/reports/india-electric-vehicles-market"
LI: Final = "/reports/india-lithium-ion-batteries-market"
CHARGING: Final = "/reports/india-ev-charging-market"
STORAGE: Final = "/reports/india-energy-storage-market"
SOLAR: Final = "/reports/india-solar-power-market"


def _p(*sentences: str) -> str:
    return "<p>" + " ".join(sentences) + "</p>"


OUTLOOK = _p(
    "Analysts interviewed for this study expect investment to rise each year as costs fall,",
    "domestic supply improves and new projects reach financial close across the major states,",
    "supported by stable policy, patient private capital and a growing pool of skilled",
    "engineers, installers and project managers.",
)
EV_FILLER = (
    "Electric vehicle adoption in India is accelerating as electric cars and electric "
    "scooters become cheaper to own and operate.",
    "State subsidies and falling ownership costs keep demand for electric vehicles rising "
    "in every metro region.",
    "Fleet operators report that electric mobility lowers running costs for delivery and "
    "ride hailing services.",
)
EV_MARKET_PARAS = (
    _p(*EV_FILLER),
    _p(
        "Vehicle makers depend on imported lithium-ion batteries, and local cell plants are "
        "planned to cut that dependence over the coming decade."
    ),
    _p(
        "Public charging infrastructure remains thin outside large cities, which slows "
        "adoption among private buyers in smaller towns."
    ),
    _p(*EV_FILLER[::-1]),
    _p("The electric vehicle market in India is expected to grow strongly through 2030."),
)
LI_PARAS = (
    OUTLOOK,
    _p(
        "Lithium-ion battery manufacturing in India is scaling up as gigafactories open and "
        "battery cells are produced locally for the first time.",
        "Cell makers are investing in lithium-ion batteries for both mobility and grid uses.",
    ),
    _p(
        "Demand from electric vehicles is the largest driver of cell orders, followed by "
        "consumer electronics and industrial equipment."
    ),
    _p(
        "Utilities are also buying cells for energy storage projects that balance the grid "
        "during peak hours."
    ),
    _p(
        "Battery cell prices have fallen sharply, and lithium-ion battery makers expect "
        "further declines as local production of battery cells grows."
    ),
    _p("Li-ion batteries remain the dominant chemistry in India for the rest of the decade."),
)
CHARGING_PARAS = (
    OUTLOOK,
    _p(
        "EV charging networks in India are expanding along highways and inside cities, with "
        "fast chargers and charging stations installed at fuel retail sites.",
        "Charging infrastructure operators are signing long term site agreements.",
    ),
    _p(
        "Growth in electric vehicles on the road makes each new charging station more "
        "profitable as utilisation rises."
    ),
    _p(
        "Charge points at offices and apartment blocks are the fastest growing segment of "
        "EV charging, according to operators interviewed for this report."
    ),
    _p("Charging stations powered by rooftop solar are being piloted in several states."),
    _p("The EV charging infrastructure market in India is set for rapid expansion."),
)
STORAGE_PARAS = (
    OUTLOOK,
    _p(
        "Energy storage capacity in India is growing as grid operators procure battery "
        "energy storage systems and battery storage tenders multiply.",
        "Grid storage is becoming central to integrating variable supply.",
    ),
    _p(
        "Most projects use lithium-ion batteries, although alternative chemistries are being "
        "tested at pilot scale by state utilities."
    ),
    _p(
        "Pairing storage with solar power plants lets developers shift daytime output into "
        "the evening peak and sell it at higher prices."
    ),
    _p(
        "Battery energy storage tenders now attract strong bids, and energy storage is a "
        "standard part of new grid planning in India."
    ),
    _p("Energy storage deployments in India will multiply through the decade."),
)
SOLAR_PARAS = (
    OUTLOOK,
    _p(
        "Solar power capacity in India keeps breaking records as utility scale parks and "
        "rooftop solar installations expand photovoltaic generation.",
        "Solar energy tariffs remain among the lowest in the world.",
    ),
    _p(
        "Developers increasingly add energy storage to new parks so that solar output can be "
        "delivered after sunset."
    ),
    _p(
        "Module supply chains are shifting towards domestic manufacturing of solar panels "
        "under production incentives, and solar pv demand stays strong."
    ),
    _p(
        "Rooftop solar adoption among commercial buildings is rising quickly, and solar "
        "power purchase agreements are now common for large offices."
    ),
    _p("The solar power market in India is forecast to keep growing quickly."),
)
CORPUS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    EV: ("India Electric Vehicles Market Outlook 2030", EV_MARKET_PARAS),
    LI: ("India Lithium-ion Batteries Market Report", LI_PARAS),
    CHARGING: ("India EV Charging Infrastructure Market", CHARGING_PARAS),
    STORAGE: ("India Energy Storage Market Outlook", STORAGE_PARAS),
    SOLAR: ("India Solar Power Market Report", SOLAR_PARAS),
}


def body_html(title: str, paragraphs: Sequence[str]) -> str:
    return f"<main><h1>{title}</h1>" + "".join(paragraphs) + "</main>"


def record(path: str, *, title: str | None = None, paragraphs: Sequence[str] | None = None,
           **fields: Any) -> dict[str, Any]:  # fmt: skip
    default_title, default_paragraphs = CORPUS.get(path, ("Page", ()))
    title = title or default_title
    return {
        "url": SITE + path,
        "title": title,
        "body_html": body_html(title, paragraphs if paragraphs is not None else default_paragraphs),
        "publication_state": "published",
        "language": "en",
        **fields,
    }


def write_export(directory: Path, records: Sequence[dict[str, Any]], *, name: str) -> Path:
    header = {
        "format": "ken-inventory-export",
        "version": 1,
        "site_url": SITE + "/",
        "allowed_hosts": ["site.test"],
        "exported_at": "2026-10-14T00:00:00Z",
        "generator": "recommendation-tests 1",
    }
    path = directory / name
    lines = [json.dumps(header), *(json.dumps(r) for r in records)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def import_records(
    sessionmaker: SM, directory: Path, records: Sequence[dict[str, Any]], *, name: str = "x.jsonl"
) -> IngestionRunDetail:
    source = FileExportContentSource(
        write_export(directory, records, name=name), max_bytes=10_000_000
    )
    return await run_ingestion(source=source, sessionmaker=sessionmaker)


def provider() -> MockModelProvider:
    return MockModelProvider(SETTINGS)


async def analyse(sessionmaker: SM) -> JobRunDetail:
    return await run_analysis(
        sessionmaker=sessionmaker,
        settings=SETTINGS,
        model_provider=provider(),
        triggered_by="test",
    )


async def recommend(sessionmaker: SM, **kwargs: Any) -> JobRunDetail:
    kwargs.setdefault("model_provider", provider())
    return await run_recommendations(
        sessionmaker=sessionmaker, settings=SETTINGS, triggered_by="test", **kwargs
    )


async def seed_corpus(sessionmaker: SM, directory: Path, *extra: dict[str, Any]) -> None:
    await import_records(sessionmaker, directory, [record(p) for p in CORPUS] + list(extra))
    analysed = await analyse(sessionmaker)
    assert analysed.counters["errors"] == 0, analysed.counters


async def page_ids(sessionmaker: SM) -> dict[str, uuid.UUID]:
    async with sessionmaker() as session:
        found = (await session.execute(text("SELECT canonical_url, id FROM pages"))).all()
    return {str(u).removeprefix(SITE): uuid.UUID(str(i)) for u, i in found}


async def rows(sessionmaker: SM, sql: str, **params: Any) -> list[Any]:
    async with sessionmaker() as session:
        return list((await session.execute(text(sql), params)).all())


async def scalar(sessionmaker: SM, sql: str, **params: Any) -> Any:
    async with sessionmaker() as session:
        return await session.scalar(text(sql), params)


# --------------------------------------------------------------------------- #
# Hand-made plans (transition and reader tests)
# --------------------------------------------------------------------------- #
def breakdown(total: float = 0.5) -> ScoreBreakdown:
    component = ScoreComponent(name="semantic", value=total, weight=1.0, contribution=total)
    return ScoreBreakdown(
        components=(component,),
        relevance=total,
        factors={"placement_quality": 1.0},
        total=total,
        semantic_floor=0.3,
        policy_name="ranking",
        policy_version=1,
    )


def make_plan(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    status: LinkPlanStatus = LinkPlanStatus.PROPOSED,
    block_text: str = "Batteries power electric vehicles.",
    anchor: str = "electric vehicles",
    total: float = 0.5,
    created_at: datetime | None = None,
    **overrides: Any,
) -> LinkPlan:
    start = block_text.index(anchor)
    now = created_at or datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "run_id": None,
        "source_page_id": source_id,
        "source_page_version": 1,
        "source_url": f"{SITE}/s",
        "target_page_id": target_id,
        "target_page_version": 1,
        "target_url": f"{SITE}/t",
        "target_title": "Target",
        "relationship_type": None,
        "channels": (RetrievalChannel.SEMANTIC,),
        "anchor_text": anchor,
        "anchor_source": "target_topic",
        "locator": PlacementLocator(
            block_id="b1",
            block_kind="p",
            occurrence_index=0,
            start=start,
            end=start + len(anchor),
            context_hash=sha256_hex(block_text),
        ),
        "preview": PlacementPreview(
            before=block_text[:start], anchor=anchor, after=block_text[start + len(anchor) :]
        ),
        "score": breakdown(total),
        "confidence": total,
        "reason_codes": (),
        "rationale": "Link.",
        "engine_version": ENGINE_VERSION,
        "policy_version": 1,
        "ranking_policy_version": 1,
        "analysis_version": "tax1-alg1",
        "status": status,
        "created_at": now,
        "updated_at": now,
    } | overrides
    return LinkPlan(**values)


async def insert_plans(sessionmaker: SM, *plans: LinkPlan) -> None:
    async with sessionmaker() as session, session.begin():
        for plan in plans:
            await repo.insert_plan(session, plan=plan)
            await repo.insert_transition(
                session,
                plan_id=plan.id,
                from_status=None,
                to_status=plan.status,
                actor="system:test",
                note=None,
                occurred_at=plan.created_at,
            )
