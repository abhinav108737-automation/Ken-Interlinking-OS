"""Derive a PROPOSED taxonomy (policies/taxonomy/taxonomy.v2.yaml) from a real-site export.

Deterministic: no model, no network. Input is the JSONL export produced from the approved read-only
crawl of kenresearch.com (docs/CONTRACTS.md, "JSONL export format"). Output is DATA for review:

* geographies  -- countries and regions found as leading words of report / benchmarking URL slugs;
* markets      -- the remaining slug words, grouped under industries by keyword rules;
* page types   -- Ken's real URL families (reports, benchmarks, surveys, articles, ...);
* protections  -- legal pages.

It does NOT invent a company/product gazetteer (``entities`` stays empty): that needs Ken's data.
The file is written with ``status: proposed`` and is never loaded by default; promoting it is the
owner's decision (a ``KEN_TAXONOMY_PATH`` change plus re-analysis).

    .venv/Scripts/python.exe evals/intelligence/derive_taxonomy.py EXPORT.jsonl OUT.yaml
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path
from typing import Any, Final

import yaml

from ken_contracts.intelligence import TaxonomyPolicy

# id -> (label, kind, parent, extra aliases). Only the entries seen in the data are emitted.
_GEO: Final[dict[str, tuple[str, str, str | None, tuple[str, ...]]]] = {
    "global": ("Global", "region", None, ("worldwide",)),
    "asia-pacific": ("Asia Pacific", "region", "global", ("apac", "asia-pacific")),
    "southeast-asia": (
        "Southeast Asia",
        "region",
        "asia-pacific",
        ("sea", "south east asia", "asean"),
    ),
    "south-asia": ("South Asia", "region", "asia-pacific", ()),
    "middle-east": ("Middle East", "region", "global", ("mena",)),
    "gcc": ("GCC", "region", "middle-east", ("gulf cooperation council", "gulf region")),
    "mea": ("Middle East & Africa", "region", "global", ("mea",)),
    "europe": ("Europe", "region", "global", ()),
    "eastern-europe": ("Eastern Europe", "region", "europe", ()),
    "north-america": ("North America", "region", "global", ()),
    "latin-america": ("Latin America", "region", "global", ("latam",)),
    "africa": ("Africa", "region", "global", ()),
    "oceania": ("Oceania", "region", "global", ()),
    "india": ("India", "country", "south-asia", ("indian",)),
    "bangladesh": ("Bangladesh", "country", "south-asia", ()),
    "pakistan": ("Pakistan", "country", "south-asia", ()),
    "sri-lanka": ("Sri Lanka", "country", "south-asia", ()),
    "indonesia": ("Indonesia", "country", "southeast-asia", ("indonesian",)),
    "vietnam": ("Vietnam", "country", "southeast-asia", ("viet nam", "vietnamese")),
    "thailand": ("Thailand", "country", "southeast-asia", ("thai",)),
    "malaysia": ("Malaysia", "country", "southeast-asia", ("malaysian",)),
    "philippines": ("Philippines", "country", "southeast-asia", ("philippine",)),
    "singapore": ("Singapore", "country", "southeast-asia", ()),
    "china": ("China", "country", "asia-pacific", ("chinese",)),
    "japan": ("Japan", "country", "asia-pacific", ("japanese",)),
    "south-korea": ("South Korea", "country", "asia-pacific", ("korea",)),
    "hong-kong": ("Hong Kong", "country", "asia-pacific", ()),
    "taiwan": ("Taiwan", "country", "asia-pacific", ()),
    "australia": ("Australia", "country", "oceania", ("australian",)),
    "new-zealand": ("New Zealand", "country", "oceania", ()),
    "saudi-arabia": ("Saudi Arabia", "country", "gcc", ("ksa", "saudi")),
    "uae": ("United Arab Emirates", "country", "gcc", ("uae", "dubai", "abu dhabi")),
    "qatar": ("Qatar", "country", "gcc", ()),
    "kuwait": ("Kuwait", "country", "gcc", ()),
    "oman": ("Oman", "country", "gcc", ()),
    "bahrain": ("Bahrain", "country", "gcc", ()),
    "egypt": ("Egypt", "country", "middle-east", ()),
    "turkey": ("Turkey", "country", "middle-east", ("turkiye",)),
    "israel": ("Israel", "country", "middle-east", ()),
    "south-africa": ("South Africa", "country", "africa", ()),
    "nigeria": ("Nigeria", "country", "africa", ()),
    "kenya": ("Kenya", "country", "africa", ()),
    "sierra-leone": ("Sierra Leone", "country", "africa", ()),
    "morocco": ("Morocco", "country", "africa", ()),
    "germany": ("Germany", "country", "europe", ("german",)),
    "united-kingdom": ("United Kingdom", "country", "europe", ("uk", "britain")),
    "france": ("France", "country", "europe", ()),
    "italy": ("Italy", "country", "europe", ()),
    "spain": ("Spain", "country", "europe", ()),
    "portugal": ("Portugal", "country", "europe", ()),
    "netherlands": ("Netherlands", "country", "europe", ()),
    "poland": ("Poland", "country", "eastern-europe", ()),
    "united-states": ("United States", "country", "north-america", ("usa", "us", "u.s.")),
    "canada": ("Canada", "country", "north-america", ()),
    "mexico": ("Mexico", "country", "latin-america", ()),
    "brazil": ("Brazil", "country", "latin-america", ()),
    "chile": ("Chile", "country", "latin-america", ()),
    "argentina": ("Argentina", "country", "latin-america", ()),
    "colombia": ("Colombia", "country", "latin-america", ()),
}

# Slug-word prefixes that name each geography (longest first at match time).
_GEO_SLUGS: Final[dict[str, str]] = {
    "global": "global",
    "worldwide": "global",
    "asia-pacific": "asia-pacific",
    "apac": "asia-pacific",
    "asia": "asia-pacific",
    "southeast-asia": "southeast-asia",
    "south-east-asia": "southeast-asia",
    "sea": "southeast-asia",
    "asean": "southeast-asia",
    "south-asia": "south-asia",
    "middle-east": "middle-east",
    "gcc": "gcc",
    "mea": "mea",
    "mena": "middle-east",
    "europe": "europe",
    "eastern-europe": "eastern-europe",
    "north-america": "north-america",
    "latin-america": "latin-america",
    "latam": "latin-america",
    "africa": "africa",
    "usa": "united-states",
    "us": "united-states",
    "united-states": "united-states",
    "united-kingdom": "united-kingdom",
    "uk": "united-kingdom",
    "ksa": "saudi-arabia",
    "saudi-arabia": "saudi-arabia",
    "saudi": "saudi-arabia",
    "uae": "uae",
    "united-arab-emirates": "uae",
    "hong-kong": "hong-kong",
    "south-korea": "south-korea",
    "south-africa": "south-africa",
    "sierra-leone": "sierra-leone",
    "new-zealand": "new-zealand",
    "sri-lanka": "sri-lanka",
    **{g: g for g, (_, kind, *_r) in _GEO.items() if kind == "country" and "-" not in g},
}

# Industry rules, first match wins. A rule matches when ANY of its tokens is a token of the slug.
_INDUSTRIES: Final[tuple[tuple[str, str, frozenset[str]], ...]] = (
    (
        "financial-services",
        "Financial Services",
        frozenset(
            [
                "insurance",
                "insurtech",
                "bancassurance",
                "bank",
                "banking",
                "fintech",
                "payment",
                "payments",
                "lending",
                "loan",
                "wealth",
                "credit",
                "finance",
                "mortgage",
                "broking",
                "brokerage",
                "trading",
                "remittance",
                "bnpl",
                "mobile",
                "money",
                "neobanks",
                "atm",
                "cash",
            ]
        ),
    ),
    (
        "healthcare-life-sciences",
        "Healthcare & Life Sciences",
        frozenset(
            [
                "health",
                "healthcare",
                "medical",
                "pharma",
                "pharmaceutical",
                "pharmaceuticals",
                "clinical",
                "hospital",
                "hospitals",
                "diagnostic",
                "diagnostics",
                "telemedicine",
                "dental",
                "biotech",
                "nutraceutical",
                "supplements",
                "dietary",
                "therapeutics",
                "cardiac",
                "wellness",
                "medicine",
                "surgical",
                "orthopedic",
                "biosimilars",
                "needles",
                "parkinsons",
                "aesthetic",
                "hygiene",
                "neuro",
                "disease",
                "treatment",
                "dermatology",
                "ophthalmic",
                "optical",
            ]
        ),
    ),
    (
        "automotive-mobility",
        "Automotive & Mobility",
        frozenset(
            [
                "car",
                "cars",
                "vehicle",
                "vehicles",
                "ev",
                "evs",
                "automotive",
                "truck",
                "trucks",
                "tyre",
                "tyres",
                "tire",
                "motorcycle",
                "motorcycles",
                "bus",
                "charging",
                "mobility",
                "rental",
                "fleet",
                "scooter",
                "autonomous",
                "aftermarket",
            ]
        ),
    ),
    (
        "energy-utilities",
        "Energy & Utilities",
        frozenset(
            [
                "energy",
                "solar",
                "wind",
                "power",
                "battery",
                "batteries",
                "oil",
                "gas",
                "fuel",
                "hydrogen",
                "utilities",
                "electricity",
                "nuclear",
                "grid",
                "lng",
                "petroleum",
                "coal",
                "renewable",
                "biofuel",
            ]
        ),
    ),
    (
        "agriculture-food",
        "Agriculture & Food",
        frozenset(
            [
                "seed",
                "seeds",
                "fertilizer",
                "fertilizers",
                "fertiliser",
                "agri",
                "agriculture",
                "agricultural",
                "cashew",
                "food",
                "foods",
                "beverage",
                "beverages",
                "crop",
                "herbicide",
                "herbicides",
                "farm",
                "farming",
                "dairy",
                "meat",
                "catering",
                "nutrition",
                "poultry",
                "fisheries",
                "aquaculture",
                "grain",
                "rice",
                "sugar",
                "coffee",
                "tea",
                "pesticide",
                "fruit",
                "vegetable",
                "bacon",
                "butter",
                "dry",
                "fruits",
                "water",
                "foodservice",
                "flavors",
                "flavored",
                "fragrances",
                "tobacco",
                "chocolate",
                "bakery",
                "snacks",
                "juice",
                "wine",
                "beer",
                "spirits",
                "confectionery",
            ]
        ),
    ),
    (
        "technology-software",
        "Technology & Software",
        frozenset(
            [
                "cloud",
                "saas",
                "software",
                "cybersecurity",
                "cyber",
                "ai",
                "data",
                "analytics",
                "iot",
                "digital",
                "platform",
                "platforms",
                "it",
                "semiconductor",
                "semiconductors",
                "electronics",
                "robotics",
                "blockchain",
                "cryptocurrency",
                "computing",
                "network",
                "networking",
                "datacenter",
                "erp",
                "crm",
                "sensors",
                "sensor",
                "customer-relationship-management",
                "management",
                "intelligence",
            ]
        ),
    ),
    (
        "logistics-transport",
        "Logistics & Transport",
        frozenset(
            [
                "logistics",
                "shipping",
                "freight",
                "warehousing",
                "warehouse",
                "aviation",
                "airline",
                "airlines",
                "airport",
                "supply",
                "cold",
                "transport",
                "transportation",
                "maritime",
                "rail",
                "courier",
                "delivery",
                "ports",
                "3pl",
            ]
        ),
    ),
    (
        "real-estate-construction",
        "Real Estate & Construction",
        frozenset(
            [
                "real",
                "estate",
                "construction",
                "building",
                "buildings",
                "office",
                "housing",
                "parking",
                "infrastructure",
                "cement",
                "residential",
                "commercial",
                "property",
                "interior",
                "hvac",
                "elevators",
            ]
        ),
    ),
    (
        "education-services",
        "Education & Professional Services",
        frozenset(
            [
                "education",
                "elearning",
                "e-learning",
                "student",
                "bootcamp",
                "training",
                "school",
                "schools",
                "university",
                "coding",
                "recruitment",
                "staffing",
                "consulting",
                "outsourcing",
                "hr",
            ]
        ),
    ),
    (
        "media-telecom",
        "Media, Telecom & Entertainment",
        frozenset(
            [
                "media",
                "telecom",
                "telecommunications",
                "streaming",
                "ott",
                "gaming",
                "broadband",
                "entertainment",
                "advertising",
                "publishing",
                "esports",
                "music",
                "film",
                "broadcasting",
                "cable",
                "tv",
            ]
        ),
    ),
    (
        "industrial-manufacturing",
        "Industrial & Manufacturing",
        frozenset(
            [
                "machinery",
                "equipment",
                "manufacturing",
                "packaging",
                "chemicals",
                "chemical",
                "paints",
                "coatings",
                "steel",
                "textile",
                "textiles",
                "plastic",
                "plastics",
                "paper",
                "industrial",
                "metals",
                "mining",
                "glass",
                "rubber",
                "adhesives",
                "pumps",
                "valves",
                "tools",
                "processing",
                "recycling",
                "waste",
                "composites",
                "explosives",
                "ferro",
                "polyester",
                "fiber",
                "fibre",
                "membrane",
                "coating",
                "molybdenum",
                "alloys",
                "ceramics",
                "lubricants",
                "electrode",
                "instruments",
            ]
        ),
    ),
    (
        "consumer-retail",
        "Consumer & Retail",
        frozenset(
            [
                "retail",
                "consumer",
                "apparel",
                "furniture",
                "appliance",
                "appliances",
                "washing",
                "cosmetic",
                "cosmetics",
                "beauty",
                "stationery",
                "fashion",
                "toys",
                "sports",
                "pos",
                "jewellery",
                "jewelry",
                "footwear",
                "luxury",
                "ecommerce",
                "e-commerce",
                "hotels",
                "hospitality",
                "tourism",
                "travel",
                "restaurants",
                "golf",
                "comic",
                "camera",
                "lens",
                "led",
                "television",
                "mattress",
                "bedding",
                "personal",
                "care",
                "hangover",
                "landscaping",
                "night",
                "vision",
                "lights",
                "gloves",
                "clubs",
            ]
        ),
    ),
)
_FALLBACK_INDUSTRY: Final = ("other-industries", "Other Industries")

_UPPER: Final = frozenset(
    [
        "ev",
        "ai",
        "saas",
        "iot",
        "it",
        "hvac",
        "pos",
        "erp",
        "crm",
        "bnpl",
        "lng",
        "mdr",
        "soc",
        "ott",
        "3pl",
        "hr",
        "uae",
        "ksa",
        "gcc",
        "usa",
        "uk",
    ]
)


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _label(slug: str) -> str:
    return " ".join(w.upper() if w in _UPPER else w.capitalize() for w in slug.split("-"))


def _split_geo(slug: str) -> tuple[str | None, str]:
    """Longest leading geography phrase of a slug, and the rest."""
    words = slug.split("-")
    for n in range(min(4, len(words) - 1), 0, -1):
        geo = _GEO_SLUGS.get("-".join(words[:n]))
        if geo:
            return geo, "-".join(words[n:])
    return None, slug


def _market_slug(kind: str, url: str) -> str | None:
    slug = _slug(url)
    if kind == "report":
        slug = re.sub(r"^industry-report-on-", "", slug)
        slug = re.sub(r"-market$", "", slug)
    elif kind == "competition_benchmarking":
        slug = re.sub(r"-competition-benchmarking(-\d{4})?$", "", slug)
    else:
        return None
    return slug or None


def _industry(slug: str) -> tuple[str, str]:
    tokens = set(slug.split("-")) | {
        "-".join(p) for p in zip(slug.split("-"), slug.split("-")[1:], strict=False)
    }
    for iid, label, keys in _INDUSTRIES:
        if tokens & keys:
            return iid, label
    return _FALLBACK_INDUSTRY


def _page_types() -> list[dict[str, Any]]:
    def rule(
        pid: str,
        label: str,
        patterns: list[str],
        intent: str,
        importance: int,
        src: bool = True,
        tgt: bool = True,
    ) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": pid,
            "label": label,
            "path_patterns": patterns,
            "intent": intent,
            "commercial_importance": importance,
        }
        if not src:
            d["linkable_as_source"] = False
        if not tgt:
            d["linkable_as_target"] = False
        return d

    return [
        rule(
            "hub",
            "Home / listing page",
            [
                r"^/$",
                r"^/(industry-reports|articles|case-studies|survey|pov|competition-benchmarking)/?$",
            ],
            "navigational",
            1,
            src=False,
        ),
        rule(
            "transactional",
            "Contact / enquiry / purchase",
            [r"^/(contact-us|talk-to-us|book-a-discovery-call|sample-report|dossier360)(/|$)"],
            "transactional",
            1,
            src=False,
            tgt=False,
        ),
        rule(
            "corporate",
            "Corporate / legal",
            [r"^/(about-us|careers|expert-panel|privacy-policy|terms-and-conditions)(/|$)"],
            "corporate",
            0,
            src=False,
            tgt=False,
        ),
        rule(
            "benchmark",
            "Competition benchmarking",
            [r"^/competition-benchmarking/"],
            "commercial_research",
            3,
        ),
        rule(
            "report",
            "Market research report",
            [r"^/industry-reports/", r"^/[a-z0-9-]+-market/?$"],
            "commercial_research",
            3,
        ),
        rule("survey", "Survey", [r"^/survey/"], "commercial_research", 2),
        rule("case_study", "Case study", [r"^/case-studies/"], "informational", 2),
        rule("pov", "Point of view", [r"^/pov/"], "informational", 2),
        rule("insight", "Article", [r"^/articles/"], "informational", 2),
        rule(
            "brand_comparison",
            "Ken Research comparison page",
            [r"^/ken-research-vs-"],
            "commercial_research",
            1,
        ),
        rule(
            "consulting",
            "Consulting service page",
            [r"^/(consulting|procurement-research)(/|$)"],
            "commercial_research",
            2,
        ),
        rule("other", "Other page", [r"^/"], "informational", 1),
    ]


def derive(export: Path) -> dict[str, Any]:
    rows = [
        json.loads(line) for line in export.read_text(encoding="utf-8").splitlines() if line.strip()
    ][1:]
    seen_geo: set[str] = set()
    markets: dict[str, dict[str, Any]] = {}
    industries: dict[str, str] = {}
    unmatched: list[str] = []
    for row in rows:
        kind = row.get("metadata", {}).get("sitemap_kind", "")
        slug = _market_slug(kind, row["url"])
        if slug is None:
            continue
        geo, rest = _split_geo(slug)
        if geo:
            seen_geo.add(geo)
        if not rest or rest == "market":
            continue
        iid, ilabel = _industry(rest)
        industries[iid] = ilabel
        if iid == _FALLBACK_INDUSTRY[0]:
            unmatched.append(rest)
        node = markets.setdefault(
            rest,
            {
                "id": rest[:99],
                "label": _label(rest),
                "parent": iid,
                "aliases": sorted({rest.replace("-", " "), _label(rest).lower()}),
            },
        )
        node["aliases"] = sorted(set(node["aliases"]) | {rest.replace("-", " ")})

    geos = set(seen_geo)
    for g in list(geos):  # keep every ancestor so parents resolve
        p = _GEO[g][2]
        while p:
            geos.add(p)
            p = _GEO[p][2]
    geos.add("global")
    geographies = []
    for gid in sorted(geos, key=lambda g: (_GEO[g][1] != "region", g)):
        label, kind, parent, extra = _GEO[gid]
        geographies.append(
            {
                "id": gid,
                "label": label,
                "kind": "global" if gid == "global" else kind,
                "parent": parent,
                "aliases": sorted({label.lower(), *extra}),
            }
        )
    market_nodes = [{"id": i, "label": lbl} for i, lbl in sorted(industries.items())]
    market_nodes += sorted(markets.values(), key=lambda m: str(m["id"]))
    return {
        "policy": "taxonomy",
        "version": 2,
        "status": "proposed",
        "source": f"derived from {len(rows)} pages of the approved kenresearch.com crawl "
        "(2026-10-29); "
        "owner review required; entities empty",
        "markets": market_nodes,
        "geographies": geographies,
        "entities": [],
        "page_types": _page_types(),
        "default_page_type": "other",
        "protections": [
            {
                "id": "legal-pages",
                "reason": "Legal and policy pages are never modified.",
                "path_patterns": [
                    r"^/([a-z]{2}(-[a-z]{2})?/)?(privacy|privacy-policy|terms|terms-and-conditions|cookie-policy|legal|disclaimer)(\.(html?|php))?(/|$)"
                ],
            }
        ],
        "eligibility": {"min_words_source": 150, "min_words_target": 100},
        "_stats": {"unmatched_industry": sorted(set(unmatched))},
    }


def main() -> None:
    export, out = Path(sys.argv[1]), Path(sys.argv[2])
    doc = derive(export)
    stats = doc.pop("_stats")
    for node in doc["geographies"]:
        if node["parent"] is None:
            del node["parent"]
    for node in doc["markets"]:
        if node.get("parent") is None:
            node.pop("parent", None)
    TaxonomyPolicy.model_validate(
        doc
    )  # the project's own validator: ids unique, references resolve
    header = (
        "# PROPOSED taxonomy v2 -- derived by evals/intelligence/derive_taxonomy.py.\n"
        "# DATA, NOT CODE. status: proposed => never loaded by default. Review, then promote\n"
        "# by pointing KEN_TAXONOMY_PATH at it and re-running analysis. Entities are EMPTY:\n"
        "# they need Ken's data. Page types come from the sitemap; markets from report slugs.\n\n"
    )
    out.write_text(
        header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110),
        encoding="utf-8",
    )
    cnt = collections.Counter(str(m.get("parent")) for m in doc["markets"])
    print("markets", len(doc["markets"]), "geographies", len(doc["geographies"]))
    print("per industry", dict(cnt))
    print("unmatched (first 40):", stats["unmatched_industry"][:40])


if __name__ == "__main__":
    main()
