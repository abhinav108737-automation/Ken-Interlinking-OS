# ruff: noqa: E501  -- corpus prose is data, not code
"""Build the SYNTHETIC extension corpus for the offline interlinking demo.

    .venv/bin/python evals/offline_demo/build_extension.py          # rewrite the JSONL
    .venv/bin/python evals/offline_demo/build_extension.py --check  # CI: committed file is current

Everything here is fictional text about fictional markets on the reserved ``research.example.com``
site, written to give the 65-page demo corpus the page situations it lacks: ORPHAN reports, a
hub/spoke cluster, a CMS-protected page, a draft, pages that already carry links and pages with NO
valid linking opportunity. It is not Ken Research content and says nothing about Ken Research SEO.
There is no randomness: the output is committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "fixtures" / "offline_demo" / "synthetic_extension.jsonl"
SITE = "https://research.example.com"
STAMP = "2026-10-20T09:00:00Z"


def link(path: str, text: str) -> str:
    return f'<a href="{SITE}{path}">{text}</a>'


def page(
    path: str,
    title: str,
    page_type: str,
    description: str,
    paragraphs: list[tuple[str, str]],
    *,
    state: str = "published",
    extra: dict[str, str] | None = None,
    note: str,
) -> dict[str, Any]:
    body = "".join(f"<h2>{h}</h2><p>{p}</p>" for h, p in paragraphs)
    return {
        "url": f"{SITE}{path}",
        "external_id": f"ken-demo-ext-{path.strip('/').replace('/', '-')}"[:120],
        "page_type": page_type,
        "title": title,
        "meta_description": description,
        "publication_state": state,
        "language": "en",
        "canonical_url": f"{SITE}{path}",
        "published_at": "2025-06-01T09:00:00Z",
        "modified_at": STAMP,
        "metadata": {"synthetic": "extension", "situation": note, **(extra or {})},
        "body_html": f"<main><article><h1>{title}</h1>{body}</article></main>",
    }


def pages() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # --- ORPHANS (published, nothing links to them) -------------------------------------------
    out.append(
        page(
            "/industry-reports/philippines-ev-charging-market",
            "Philippines EV Charging Market Outlook",
            "report",
            "Country study of public and depot charging for electric vehicles in the Philippines.",
            [
                (
                    "EV charging market overview",
                    "The Philippines is early in its electric vehicle journey, and charging is the main constraint on adoption. Public chargers cluster in Metro Manila, Cebu and Davao, while most owners of electric three-wheelers and two-wheelers still rely on overnight charging at home or at small depots.",
                ),
                (
                    "Business models",
                    "Operators combine paid public fast chargers with partnerships at malls, fuel stations and condominium car parks. Utilisation is low outside the capital, so many networks lean on subscription and fleet contracts. A lithium-ion battery cell price decline would improve vehicle economics but not the density of chargers.",
                ),
                (
                    "Policy and grid",
                    "National incentives for electric vehicles and a gradual build-out of rooftop solar at charging sites support the market, yet permitting and grid connection times remain long. Island geography means charging corridors are discussed city by city rather than as one national highway network.",
                ),
                (
                    "Outlook",
                    "We expect charger counts to grow quickly from a small base through 2030, led by fleet depots and mall locations, with battery swapping a niche complement for two-wheelers rather than a replacement for public charging.",
                ),
            ],
            note="orphan: country-specific study, related to the India/Indonesia charging reports",
        )
    )
    out.append(
        page(
            "/industry-reports/india-battery-swapping-market",
            "India Battery Swapping Market Report",
            "report",
            "Sector study of battery swapping networks for electric two- and three-wheelers in India.",
            [
                (
                    "Battery swapping market overview",
                    "Battery swapping lets riders exchange a depleted pack for a charged one in minutes. In India the model has found its strongest footing with electric three-wheelers and delivery fleets, where downtime costs money and vehicles return to the same neighbourhoods every day.",
                ),
                (
                    "Standards and economics",
                    "Interoperable pack standards remain the central question. Operators that own the batteries can finance them separately from the vehicle, lowering the upfront price of an electric two-wheeler, but they carry the lithium-ion battery risk and need dense station networks to reach utilisation.",
                ),
                (
                    "Relationship to charging",
                    "Swapping competes with and complements EV charging infrastructure. Where home charging is easy it wins on convenience, and where riders have no parking it fills a gap. Several operators now co-locate swap cabinets with fast chargers at fuel stations.",
                ),
                (
                    "Outlook",
                    "We expect swapping to remain a focused segment tied to commercial fleets and to the electric two-wheeler market, supported by government incentives for standardised packs and by falling cell costs.",
                ),
            ],
            note="orphan: sector-specific study with strong siblings (two-wheelers, charging, swapping insight)",
        )
    )
    out.append(
        page(
            "/insights/ev-fleet-electrification-india",
            "Fleet Electrification in India: Where the Numbers Work",
            "insight",
            "Why delivery and ride-hailing fleets lead electric vehicle adoption in India.",
            [
                (
                    "Fleets lead",
                    "Commercial fleets electrify first because the total cost of ownership can be calculated route by route. A delivery operator running fixed daily distances can size batteries and chargers precisely, which retail buyers cannot.",
                ),
                (
                    "What fleets need",
                    "Reliable charging infrastructure at depots, predictable electricity tariffs and financing for vehicles are the three requirements we hear most. Some fleets also test battery swapping where vehicles cannot dwell long enough to charge.",
                ),
                (
                    "What it means for the wider market",
                    "Fleet volumes create the demand that justifies public chargers and local battery supply. Our "
                    + link(
                        "/industry-reports/india-electric-vehicle-market",
                        "India electric vehicle market report",
                    )
                    + " and our "
                    + link(
                        "/industry-reports/india-ev-charging-infrastructure-market",
                        "charging infrastructure outlook",
                    )
                    + " set out the scale of that effect, and the "
                    + link(
                        "/insights/fame-scheme-electric-vehicle-adoption-india",
                        "FAME scheme analysis",
                    )
                    + " explains the policy background.",
                ),
            ],
            note="orphan insight that already carries three existing links (those must not be re-proposed)",
        )
    )

    # --- HUB / SPOKE (logistics cluster) -------------------------------------------------------
    out.append(
        page(
            "/industries/logistics",
            "Logistics Industry Reports",
            "hub",
            "Hub for logistics, warehousing and last-mile delivery research.",
            [
                (
                    "Logistics research",
                    "Browse our research on warehousing, cold chain, e-commerce logistics and last-mile delivery across India, Southeast Asia and the Middle East.",
                ),
                (
                    "Reports",
                    "Start with "
                    + link("/industry-reports/india-warehousing-market", "India warehousing")
                    + ", "
                    + link(
                        "/industry-reports/southeast-asia-last-mile-delivery-market",
                        "Southeast Asia last-mile delivery",
                    )
                    + ", "
                    + link("/industry-reports/qatar-logistics-market", "Qatar logistics")
                    + " and "
                    + link(
                        "/industry-reports/thailand-ecommerce-logistics-market",
                        "Thailand e-commerce logistics",
                    )
                    + ".",
                ),
            ],
            note="hub: links down to its spokes; no page links up to it except its spokes",
        )
    )
    out.append(
        page(
            "/industry-reports/india-warehousing-market",
            "India Warehousing Market Outlook",
            "report",
            "Spoke report on organised warehousing capacity and demand in India.",
            [
                (
                    "Warehousing market overview",
                    "Organised warehousing in India is expanding as manufacturers, retailers and e-commerce platforms consolidate inventory into larger, better-located facilities. Grade A parks near Mumbai, Delhi, Bengaluru and Chennai attract most new leasing.",
                ),
                (
                    "Demand drivers",
                    "E-commerce fulfilment, third-party logistics and a shift to hub-and-spoke distribution after tax reforms drive absorption. Temperature-controlled space for food and pharmaceuticals is the fastest-growing niche and overlaps with the cold chain market.",
                ),
                (
                    "Last-mile links",
                    "Urban micro-fulfilment centres feed same-day delivery and connect warehousing to last-mile delivery economics, which differ sharply by city density.",
                ),
                (
                    "Part of the logistics cluster",
                    "See the "
                    + link("/industries/logistics", "logistics industry hub")
                    + " for related reports.",
                ),
            ],
            note="spoke: links up to the hub only; sideways opportunities to cold chain and last-mile",
        )
    )
    out.append(
        page(
            "/industry-reports/southeast-asia-last-mile-delivery-market",
            "Southeast Asia Last-Mile Delivery Market Report",
            "report",
            "Spoke report on last-mile parcel and food delivery in Southeast Asia.",
            [
                (
                    "Last-mile delivery market overview",
                    "Last-mile delivery in Southeast Asia is shaped by dense cities, two-wheeler fleets and fast-growing e-commerce volumes in Indonesia, Vietnam, Thailand and the Philippines.",
                ),
                (
                    "Operating models",
                    "Platforms mix employed couriers, gig riders and locker networks. Cost per parcel falls with drop density, which is why Bangkok and Ho Chi Minh City economics differ from island markets. Electric two-wheeler fleets are being tested to cut fuel costs.",
                ),
                (
                    "Warehousing and returns",
                    "Delivery performance depends on upstream warehousing and on returns handling, an underestimated cost in cross-border e-commerce.",
                ),
                (
                    "Part of the logistics cluster",
                    "See the "
                    + link("/industries/logistics", "logistics industry hub")
                    + " for related reports.",
                ),
            ],
            note="spoke: links up to the hub only; sideways opportunities to e-commerce logistics reports",
        )
    )

    # --- COUNTRY / SECTOR STUDIES --------------------------------------------------------------
    out.append(
        page(
            "/industry-reports/egypt-fintech-market",
            "Egypt Fintech Market Outlook",
            "report",
            "Country study of digital payments and lending in Egypt.",
            [
                (
                    "Fintech market overview",
                    "Egypt's fintech sector is driven by a young population, mobile wallets and central bank initiatives that widen access to bank accounts. Digital payments grew quickly as merchants accepted QR codes and instant transfers.",
                ),
                (
                    "Digital lending",
                    "Buy-now-pay-later and small-business lending platforms are scaling, though credit data remains thin. Lessons from India's digital lending and digital payments systems are widely discussed by Egyptian regulators.",
                ),
                (
                    "Outlook",
                    "We expect continued growth in wallet usage and embedded finance, with regulation of lending the main swing factor.",
                ),
            ],
            note="country-specific study with natural unlinked mentions of India digital lending/payments",
        )
    )

    # --- PROTECTED / DRAFT ---------------------------------------------------------------------
    out.append(
        page(
            "/industry-reports/uae-fintech-regulatory-brief",
            "UAE Fintech Regulatory Brief (client only)",
            "report",
            "Client-only regulatory brief on fintech licensing in the UAE.",
            [
                (
                    "Regulatory brief",
                    "This brief summarises licensing categories for payments and digital lending firms in the UAE and the supervisory expectations of the central bank. It is prepared for subscribing clients and is not for general distribution.",
                ),
                (
                    "Scope",
                    "Digital payments, digital lending and fintech sandboxes are covered, with comparisons to India and Singapore. The brief is updated quarterly and distributed under a restricted licence.",
                ),
                (
                    "Notes",
                    "Because the content is licensed, no automated process may edit this page.",
                ),
            ],
            extra={"cms_protected": "true"},
            note="protected: CMS-protected page; must never be edited or receive a link",
        )
    )
    out.append(
        page(
            "/industry-reports/kuwait-diagnostics-market",
            "Kuwait Diagnostics Market Report (draft)",
            "report",
            "Draft report on diagnostics in Kuwait.",
            [
                (
                    "Diagnostics market overview",
                    "Kuwait's diagnostics market combines public laboratories and private chains. Imaging and molecular testing are expanding with medical tourism and insurance coverage changes. This draft awaits editorial review.",
                ),
                (
                    "Notes",
                    "The report is unpublished and must not receive links until it is released.",
                ),
            ],
            state="draft",
            note="draft: unpublished, never a valid target",
        )
    )

    # --- NO VALID OPPORTUNITY ------------------------------------------------------------------
    out.append(
        page(
            "/industry-reports/global-postal-philately-market",
            "Global Postal Philately Market Report",
            "report",
            "Report on stamp collecting and philatelic auctions worldwide.",
            [
                (
                    "Philately market overview",
                    "Postal philately, the collection and study of stamps and postal history, is a small collectors' market served by auction houses, dealers and specialist societies. Prices are driven by rarity, condition and provenance rather than by industry cycles.",
                ),
                (
                    "Collectors and auctions",
                    "Rare issues sell through a handful of international auction houses, and online platforms have widened the buyer pool. Catalogues and grading services give structure to a market with little standardisation.",
                ),
                (
                    "Outlook",
                    "Demand is stable among older collectors and uneven among younger ones; the segment is unlikely to connect to the other markets covered on this site.",
                ),
            ],
            note="no valid opportunity: topically unrelated to every other page",
        )
    )
    out.append(
        page(
            "/insights/office-furniture-design-trends",
            "Office Furniture Design Trends",
            "insight",
            "Notes on ergonomic seating and modular desks for hybrid offices.",
            [
                (
                    "Hybrid offices",
                    "Hybrid work has shifted office furniture toward modular desks, shared meeting booths and ergonomic seating that adapts to many users in a day.",
                ),
                (
                    "Materials",
                    "Recycled fabrics and replaceable parts are the most common sustainability claims from manufacturers, although independent verification remains rare.",
                ),
                (
                    "What buyers ask",
                    "Facilities teams ask about warranty length, adjustability and cleaning, far more than about brand. This note does not relate to any market covered in our industry reports.",
                ),
            ],
            note="no valid opportunity: unrelated insight",
        )
    )
    return out


def render() -> str:
    header = {
        "format": "ken-inventory-export",
        "version": 1,
        "site_url": f"{SITE}/",
        "allowed_hosts": ["www.research.example.com"],
        "exported_at": "2026-10-20T00:00:00Z",
        "generator": "ken-offline-demo-extension 1 (SYNTHETIC)",
    }
    lines = [json.dumps(header, sort_keys=True)]
    lines += [json.dumps(p, sort_keys=True) for p in pages()]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("synthetic_extension.jsonl is out of date", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(pages())} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
