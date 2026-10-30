# Interlinking baseline — 10 results from the data already built

**Date:** 2026-10-30 · **Database:** `ken_real500` (498 real pages, Playwright-rendered sample) · **Nothing fetched or
added for this report.** The system's own recommendation engine produced the plans; nothing was edited by hand.

> **Purpose:** a baseline to evaluate and then optimise. **Nothing here is published.** The CMS is a mock.
> The verdicts below are the assistant's judgement and are meant to be challenged. The sortable copy is
> [interlink_baseline_2026-10-30.csv](interlink_baseline_2026-10-30.csv) (it has the full plan ids, context
> sentences and a blank place for your own verdict).

## How the 10 were chosen

All 126 proposed plans, ranked by the engine's own confidence, taking the best plan **per source page** (so one
page cannot fill the list), then the top 10. No manual picking. Confidence is the engine's raw score, not a
calibrated probability.

## Result: 2 good, 1 acceptable, 7 poor

| # | Source → Target | Anchor | Verdict | Why |
|---|---|---|---|---|
| 1 | India polyester fibre → Global protective textiles | "protective textiles" | **Good** | Natural list of end uses; the target is the dedicated report |
| 2 | India dry fruits → Africa cashew | "cashew" | Acceptable | Relevant (raw-nut supply), but not the "same geography" the reasons claim |
| 3 | Indonesia natural personal care → **India** cosmetics | "cosmetics" | Poor | Indonesian sentence linked to the India report; generic word. *Handed on by the critic* |
| 4 | North America nuts & seeds → APAC vegetable seed | "seed" | Poor | Edible seeds linked to a *planting*-seed report. *Handed on by the critic* |
| 5 | APAC golf → Global men's golf clubs | "Golf clubs" | **Good** | Specific phrase, same market |
| 6 | Saudi EMS → **Oman** medical devices | "medical devices" | Poor | Saudi sentence linked to the Oman report |
| 7 | India flavours & fragrances → India cosmetics | "Cosmetics" | Poor | Anchor is one word inside another report's *title* in a related-reports list |
| 8 | Global golf gloves → Global men's golf clubs | "golf clubs" | Poor | Here "clubs" means organisations, not the equipment the target covers |
| 9 | Brazil fertilisers → APAC vegetable seed | "Seed" | Poor | Word inside the title "Singapore Seed Varieties Market" |
| 10 | China car finance → USA used car | "Used Car" | Poor | Word inside the title "Thailand Used Car Sales Market" |

Full context sentences and plan ids are in the CSV. In the dashboard, open **Recommendations** and search the
source URL.

## Why the poor ones are poor (root causes, from the code and data)

1. **"Same geography" is satisfied by incidental mentions** (affects 2, 3, 4, 6, 9, 10). All ten plans carry the
   reason `same_geography`, but six cross country or region. The rule (`recommendations/scoring.py`,
   `same_country`) is true when the two pages share *any country mentioned in their text*. The Africa cashew report
   mentions India three times, so "India dry fruits → Africa cashew" counts as the same country.
   **Fix:** compare each page's *primary* geography (top-scored, or the one in its title or URL), and keep mentions as
   weak evidence only.
2. **Anchors inside lists of report titles** (7, 9, 10). The text is a "related reports" list, and the anchor is a
   word inside another report's title. **Fix:** exclude blocks that are lists of titles; require the anchor to sit
   in running prose with a minimum amount of surrounding text.
3. **One generic word as the anchor** (3, 4, 6, 8: "cosmetics", "seed", "medical devices", "golf clubs"). A single
   common word links to whichever report has it in its name, whatever the sense. **Fix:** require the anchor to be
   the target's market phrase or a specific multi-word form, and check that the *sentence* (not only the page) is
   about the target's topic. This is the open question in NEXT_STEPS item 9, and the new `anchor_too_generic`
   feedback reason is the right label for reviewers.
4. **The local critic is not catching these.** Two of the ten (3 and 4) were reviewed by the 8B model and handed on to the review queue;
   both are poor. With n = 2 this is a warning, not a measurement. Critic calibration needs real reviewer decisions.

## What good looks like here (1 and 5)

The anchor is a specific phrase inside a running sentence, the sentence is about the target's subject, and the
target is the dedicated page for it. Plans 1 and 5 are the pattern to keep; the filters above should remove most
of the rest while keeping those.

## Suggested next step (needs your agreement)

Apply fixes 1–3 as deterministic rules (they are policy and code changes, so they get tests and an evaluation
fixture), then re-run on the same 498 pages and compare against this baseline: same selection rule, same ten
slots. Expected effect: fewer than 126 plans, a higher share like 1 and 5. This report is the "before".
