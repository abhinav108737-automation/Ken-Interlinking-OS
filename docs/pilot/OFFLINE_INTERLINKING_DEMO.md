# Offline interlinking demonstration (no website, no CMS, nothing published)

Written for: engineers and operators who want to reproduce the demo or load content they are already
authorised to use.

What it is: the full shadow-mode interlinking pipeline run on **synthetic** pages (plus any folder you add), on this
machine only. Every step is local: PostgreSQL on port 5433 (database `ken_offline_demo`), Qdrant on 127.0.0.1
(collection `ken_offline_demo_pages`) and Ollama on 127.0.0.1. No request is sent to `kenresearch.com` or any other site,
no scraper or mirror is used, the CMS is the in-memory mock, `KEN_AUTONOMY_MODE` is forced to `shadow`, and the
runner refuses nothing less. Host PostgreSQL on port 5432 is never touched.

## 1. Prerequisites

```bash
make dev            # project PostgreSQL (5433) + Qdrant (6333), migrated
ollama list         # qwen3:8b-q4_K_M and nomic-embed-text must be present
```

## 2. Run the demo

```bash
make offline-demo ARGS=--fresh        # real local critic (qwen3:8b), about 12 minutes; use caffeinate (built in)
make offline-demo-fast                # deterministic rule critic, about 2 minutes
make offline-demo-report              # rebuild only the report from the existing demo database
```

Equivalent direct command: `.venv/bin/python evals/offline_demo/run_demo.py --fresh [--rule-critic]`.

The runner performs, in order: create + migrate + grant the demo database (`ken_offline_demo`) -> import the
65-record base corpus labelled `synthetic_base` -> import the 11-page extension labelled `synthetic_extension` ->
(optionally) import your authorised folder -> analyse and embed (normalisation, versioning, entities, local
embeddings) -> index into Qdrant and verify the index -> compute the link graph -> candidate discovery with lexical,
semantic, taxonomy and graph channels, ranking and anchor placement -> critic and deterministic policy gate -> shadow
autonomy decisions -> management report.

Output (committed): `docs/pilot/offline_demo_report/INTERLINKING_REPORT.md`, `recommendations.csv`, `report.json`.

## 3. The synthetic content

* `tests/fixtures/demo/research_site.jsonl`: the existing fictional 65-record corpus (reports, insights, hubs, a
  CMS-protected page, drafts, thin pages, legal pages, a canonical alias, existing links).
* `tests/fixtures/offline_demo/synthetic_extension.jsonl` (built by `evals/offline_demo/build_extension.py`, 11 pages)
  adds: three **orphans** (a country study, a sector study and an insight that already has three links), a
  **logistics hub with two spokes**, a country-specific fintech study, a **CMS-protected** page, a **draft**, and two
  pages with **no valid linking opportunity** (philately, office furniture).

All of it is on the reserved `research.example.com` domain and every link stays on it. A test fails if the text
contains "kenresearch".

## 4. Importing content you are already authorised to use

The ingestion architecture is unchanged: any source is a `ContentSourceProvider`, so a future authorised export or
folder goes through the same normalisation, versioning and analysis. Nothing here assumes access to a private CMS,
an internal drive or a credential. Two import paths exist:

```bash
# a) a JSONL export in the documented format (docs/CONTRACTS.md, header line then one record per page)
.venv/bin/python -m ken_services.inventory.cli import-file /path/to/export.jsonl --provenance authorized_import

# b) a folder of HTML files (index.html -> site root; reports/ev.html -> <site>/reports/ev)
make import-html-dir DIR=/path/to/html SITE=https://your-site.example/ PROVENANCE=authorized_import
```

Safety: local reads only; symlinks leaving the folder, files over 5 MB, non-UTF-8 files and duplicate URLs become
reported issues, never failures. The runner accepts such a folder only with an explicit statement:

```bash
.venv/bin/python evals/offline_demo/run_demo.py --fresh \
    --html-dir /path/to/html --html-site-url https://your-site.example/ --confirm-authorized
```

`--confirm-authorized` is **your** statement that you may already use those files. Pages imported this way are labelled
`authorized_import`, reported in their own section of the report, and are never mixed into the synthetic findings.
A page with no label is shown as `unlabelled` and is never counted as real.

## 5. Provenance rules (how synthetic and real findings stay apart)

Every imported page carries `metadata.provenance` (`synthetic_base`, `synthetic_extension`, `authorized_import`). The
report states the split in section 1, shows gold-judgment metrics for the synthetic base corpus only (section 8, headed
SYNTHETIC), and gives real content its own section 10 with no metrics at all (none can be computed without labels).
The report banner says no figure is an SEO outcome.

## 6. Checks

```bash
.venv/bin/pytest -q tests/unit/offline_demo tests/unit/inventory/test_html_dir_source.py \
    tests/unit/inventory/test_file_export_provenance.py
KEN_TEST_DATABASE_URL=postgresql+asyncpg://ken:ken@localhost:5433/ken_x_test \
    .venv/bin/pytest -q tests/integration/test_offline_import_provenance.py
.venv/bin/python evals/offline_demo/build_extension.py --check
make doctor
```

Reset: `make offline-demo ARGS=--fresh` drops only `ken_offline_demo` and the `ken_offline_demo_pages` collection.
