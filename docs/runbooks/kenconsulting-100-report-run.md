# Runbook — the Ken Consulting 100-report interlinking run

- **Status:** written 2026-11-19, for the first at-scale test on `thekenconsulting.com`.
- **Scope:** this is the *test* site. `kenresearch.com` is the production target and has no
  write-capable adapter yet. Nothing here applies to production without a new adapter.
- **Read first:** `docs/handover/2026-11-17-SESSION-HANDOVER.md` section 1.

---

## 0. Blockers to clear before the run

| # | Blocker | Owner | Evidence |
|---|---|---|---|
| ~~B1~~ | ~~`canonical_url_registry` missing: every write blocked~~ | ~~Ken Consulting tech team~~ | **CLEARED 2026-11-19** — re-measured, below |
| B2 | Markets of the new reports must exist in the taxonomy | us | §3 |
| B3 | `X-Robots-Tag: noindex, nofollow` served site-wide | Ken Consulting tech team | handover finding 5 |
| B4 | Market assignment mislabels topically-related reports, so `market_gate` refuses them | us | §3 |

### B1 — CLEARED. The migration landed 2026-11-19.

The missing table was `kenresea_kenresearch.canonical_url_registry`. Re-measured after the fix:

| Endpoint | Before | After |
|---|---|---|
| `POST /internal/pages/<slug>:<field>/link-changes/validate/` | 500, every page | **200** |
| `POST /internal/link-changes/validate/` (batch) | 500, every page | **200** |
| `GET /internal/reports/<slug>/` | 500 for 5 of 7 v0.2 | **200 for all 506** |
| `GET /internal/pages/<slug>:<field>/` | 200 | 200 |

A batch validate of one real link now returns a correct rendered preview, so the first step of
every apply and rollback works:

```
{"total":1,"valid":1,"invalid":0,"results":[{"index":0,"valid":true, ...
 "preview":{"before":"<p>The Qatar Tire Distribution and Retail Service ...",
            "after":"<p>The <a href=\"/industry-reports/qatar-organic-dyes-market\">Qatar</a> Tire ..."}}]}
```

Note the two endpoints take **different body shapes**: the batch one wraps changes in
`{"changes": [...]}`, the per-page one takes `{"expected_version", "link"}` bare. Posting the
batch shape to the per-page URL answers `400 invalid_request`, not 500.

### Three things the same fix changed, which the export had to absorb

1. **`product_version` is gone** from both the listing and the report payload. The version is
   now readable only from `product_code` — `KRV02-0057` for the seven demo reports,
   `KR-RPT-V02-64650` for the 499 published since. `is_version()` in the export reads
   `product_version` first and falls back to the code. Without this the export selected **zero**
   reports and reported success.
2. **The listing now returns 506 rows, all v0.2.** The ~28,000 v0.1 products are no longer
   listed. The client-side version filter is consequently a no-op today, and is kept only so a
   v0.1 row reappearing cannot silently enter a v0.2 run.
3. **Listing latency is normal again.** `limit=25`, `100` and `200` all answer in ~0.2 s, where
   `limit=200` previously timed out at 60 s. The default page size is now 100.

Full export after the fix: **506 of 506 reports, no failures, no field-by-field recoveries**
(previously 5 of 7 needed the fallback).

---

---

## 1. What the CMS actually contains

Measured 2026-11-19:

```
506 products listed, ALL of them v0.2 (re-measured 2026-11-19 after the migration)
  -> 7   original demo reports, product_code KRV02-00##
  -> 499 published by Snehal since,  product_code KR-RPT-V02-#####
  -> 506 of 506 read and exported cleanly
The ~28,000 v0.1 products are no longer returned by the listing endpoint at all.
```

The report payload also gained a `related` block. Swept across all 506:

| Field | Populated |
|---|---|
| `categories` (13 CMS categories, e.g. Healthcare 114, Technology and Telecom 81) | **506 / 506** |
| `adjacent_markets` — curated neighbouring markets, with resolved products | 2,365 product refs |
| `base_market_name` | 7 / 506 |
| `same_base_market` | **0 / 506** |

**Every one of those 2,365 adjacent-market product references points OUTSIDE the 506.** They
resolve to v0.1 pages such as `usa-customer-data-platform-market`. So the CMS's own curated
relatedness graph independently confirms the publishing-pattern finding: these 506 reports have
editorial neighbours, and none of the neighbours is in this corpus. `same_base_market` being
empty for all 506 says the same thing about regional siblings.

`categories` is worth having regardless — it is a CMS-authored market grouping for every report,
where our own `market_id` is inferred by phrase match and demonstrably mislabels (see B4).

Two consequences, both already handled in `infrastructure/scripts/export_kenconsulting.py`:

- **The export filters to `product_version == "v0.2"`.** The listing endpoint ignores every
  filter query parameter tried (`product_version`, `version`, `type` all return v0.1 rows), so
  the filter is applied client-side. Without it the export collects ~28,000 404s.
- **Page size 25, not 200.** Listing latency is superlinear in page size: `limit=1` answers in
  0.2 s, `limit=5` in 35 s, `limit=200` does not answer inside 60 s. At 25 the full catalogue
  walks in about 20 s.

Snehal's 100 reports must be published **as v0.2** or this pipeline will not see them.

---

## 2. Prepare the database

The operational databases are behind the code (`ken_interlink` at 0010, `ken_v3` at 0012; code
head is 0013). Use a dedicated database for the run rather than migrating either in place:

```bash
docker exec ken-interlink-postgres-1 psql -U ken -d postgres -c "create database ken_kc_scale"
KEN_DATABASE_URL="postgresql+asyncpg://ken:ken@127.0.0.1:5433/ken_kc_scale" \
  .venv/Scripts/alembic.exe upgrade head
```

Export `KEN_DATABASE_URL` for every command below.

---

## 3. Export, ingest, analyse, recommend

```bash
python infrastructure/scripts/export_kenconsulting.py --out run/kc.jsonl   # read-only
python -m ken_services.inventory.cli import-file run/kc.jsonl
python -m ken_services.intelligence.cli run
python -m ken_services.recommendations.cli run
```

**Check the market coverage before judging the output.** Market assignment is a phrase match
over taxonomy labels and aliases. `policies/taxonomy/taxonomy.v3.yaml` holds 5,451 markets, all
derived from *kenresearch.com*; the seven Ken Consulting demo reports needed six markets added
by hand (`taxonomy.v3-demo.yaml`). If the new reports' markets are absent, `market_gate` refuses
nearly everything and the run looks like an engine failure when it is a policy-data gap:

```sql
SELECT market_id, count(*) FROM page_analyses GROUP BY 1 ORDER BY 2 DESC;
```

A large `market_id IS NULL` share, or a high `rejected_market_unrelated` /
`rejected_no_market_keyword_anchor` count in the run counters, means the taxonomy needs
extending first.

### The anchor-supply limit — measured 2026-11-19, and it decides the yield

`market_keyword_anchors` is the pipeline's **only** anchor source, and every phrase it offers
contains the word *market*: "Saudi Arabia Cloud Services market", never a bare "cloud
services". So an in-prose link exists only when the SOURCE body literally names the TARGET's
market that way.

Measured over two real corpora pulled from this CMS:

| Corpus | Pages naming ≥1 market with the word "market" | Pages naming **another report's** market |
|---|---|---|
| 12 Saudi technology reports (v0.1) | 12 / 12 | **0** |
| 7 ASEAN/Gulf reports (v0.2) | 6 / 7 | **0** |

Every report names only its *own* market. Ken Research reports are written self-contained, so
the in-prose path yields **zero links however many reports go live** — this is a property of
the content, not a fault in the engine, and the engine is right to refuse rather than invent.

Two routes out, and the choice is the owner's:

1. **Related-report modules** (NEXT_STEPS item 31) — already built for exactly this, and
   verified working: 12 cold-chain reports across 12 geographies produced **12 modules, 62
   items**, each item a sibling of the same market core. Modules need no prose cross-reference
   because the link text is the target's own title.
2. **Relax the anchor rule** so editorially natural anchors qualify — the hand-made November
   test links used "two-wheeler" and "charging infrastructure", neither of which the engine
   would ever propose. This changes ADR-0011 / ADR-0027 anchor policy and needs an ADR.

Note the shapes differ: **in-prose** wants one geography and related markets; **modules** want
one market core across different geographies. A cluster built for one yields nothing for the
other.

---

## 4. Review, then deploy

Only **approved** plans ever deploy — approval is the human review gate and the bulk command
cannot reach past it. Review in the app, then:

```bash
# Always dry-run first. Writes nothing; validates against the CMS.
python -m ken_services.deployment.batch_cli apply --as-user <you> --max-sources 5

# Then execute, smallest batch first.
python -m ken_services.deployment.batch_cli apply --as-user <you> --max-sources 5 \
    --execute --out run/applied.json
```

Safety properties built into the command:

| Property | Behaviour |
|---|---|
| Dry-run default | nothing is sent without `--execute` |
| Writes flag | `--execute` exits 2 unless `KEN_CMS_WRITES_ENABLED=true` |
| Batch cap | `--max-sources` defaults to 5, hard maximum 100 |
| Circuit breaker | `--stop-after` consecutive failures aborts (default 3) |
| Re-runnable | operation ids are `uuid5` of the plan ids, so a re-run replays rather than deploying twice |
| Review gate | only `approved` plans are selected |

Start with `--max-sources 1`, confirm the page renders, then widen. Keep `--out`: it records
every deployment id, which is all a rollback needs.

### Revalidation

One `POST /api/revalidate` does not refresh all origin instances — about six calls were needed
to converge during the live test. `docs/live-tests/kenconsulting-2026-11/scripts/apply_all.py`
shows the call shape.

---

## 5. Rollback

```bash
# See what would be reversed.
python -m ken_services.deployment.batch_cli rollback --as-user <you> --all-live \
    --reason editorial_reversal

# Reverse it.
python -m ken_services.deployment.batch_cli rollback --as-user <you> --all-live \
    --reason editorial_reversal --execute --out run/rolled-back.json
```

Rollback is idempotent and retries transient CMS faults (5 attempts, backoff to 60 s), because
the Ken Consulting rollback holds a database row lock and answers HTTP 500 for minutes at a
time — during the live test one succeeded only on the fifth attempt, about 2.5 minutes in. Our
client timeout is 10 s, which is shorter than their write; the retry loop is what bridges that.

`--deployment <id>` reverses specific deployments instead of everything live.

### Still live from the November live test

Deployments **9–16** on `thekenconsulting.com` are hand-made test links, not engine output;
several would fail the ADR-0027 gates. They are kept so the frontend team can see rendering, and
should be rolled back once that is done. Ids are in
`docs/live-tests/kenconsulting-2026-11/applied.json`. Note B1: five of those pages cannot be
read through the internal API right now, so their rollback will fail until that is fixed.

---

## 6. Editorial placement rules (owner-confirmed 2026-11-17)

- **Never** the Table of Contents or the left-hand navigation / Overview panel. The *Market
  Overview body section* is allowed — the exclusion is the left nav panel.
- **Never** Research Methodology — boilerplate.
- **All other body sections are in scope**, and within them links land only in editorial prose:
  never a bullet, heading, segmentation label or table cell.

Enforced by `EXCLUDED_FIELDS` in `export_kenconsulting.py` and `block_is_prose` in
`services/ken_services/recommendations/placement.py`, and now genuinely exercised by
`test_placement_safety_page_never_anchors_under_excluded_markup`.

---

## 7. If something goes wrong

| Symptom | Do this |
|---|---|
| A deployment is stuck `applying` | `python -m ken_services.deployment.cli reconcile` — resends the stored request byte for byte |
| The breaker tripped | read the JSON lines; each names its source page and issue codes. Fix the cause before re-running — a re-run replays, it does not duplicate |
| Export says INCOMPLETE | some reports could not be read; the run would silently be missing them as sources *and* targets. Do not ingest a partial export |
| A page renders the anchor as escaped text | the 2026-11-17 render blocker has returned; stop and tell the tech team |
