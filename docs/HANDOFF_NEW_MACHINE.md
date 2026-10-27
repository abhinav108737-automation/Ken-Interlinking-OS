# Picking this project up on another machine

Repository: `https://github.com/abhinav108737-automation/Ken-Interlinking-OS` (private).
**The latest work is on the branch `phase-1-content-inventory`** (all phases, the pilot work and the sitemap crawler);
`main` is older. Tags `phase-0-complete` ... `phase-7-complete`, `local-product-hardened` and `local-product-complete`
mark the milestones. Start with `START_HERE.md`, `NEXT_STEPS.md`, `FINAL_PROJECT_REPORT.md` and `docs/DEVELOPMENT.md`.

## What is NOT in Git (on purpose) and must be recreated

| Item | Why it is not in Git | What to do |
|---|---|---|
| `.env` | holds database passwords and settings | copy `.env.example` to `.env` and fill it in; `make db-roles ARGS=--write-env` creates the app-role DSNs |
| Google service-account key (`ken-performance-evidence.json`) | a credential | copy it over a secure channel; point `KEN_ANALYTICS_SERVICE_ACCOUNT_FILE` at it. Never put it inside the repo or a OneDrive-synced folder |
| `pilots/kenresearch/approvals/*.json` | your signed authorisation; gitignored | write a new one from `*.TEMPLATE.json` (the current one expires 2026-10-28) |
| PostgreSQL data (container, port **5433**) | database contents | recreate with `docker compose up -d`, then migrations. The 25 crawled Ken pages are in `ken_kenresearch_pilot` and can be re-fetched with the sitemap pilot; local dumps are in `~/.ken-backups/` on the old Mac |
| Qdrant index | derived and rebuildable | rebuilt from PostgreSQL |
| Ollama models (`qwen3:8b`, the embedding model) | large | `ollama pull` them |
| `.venv`, `node_modules`, Playwright Chromium | machine specific | `make setup`, `npm ci`, `python -m playwright install chromium` |
| Dashboard login | a one-time password was set on the old Mac | create a new user with `make user-create` |

## OneDrive caution
If the project folder is synced by OneDrive, do not let it sync `.venv`, `node_modules`, `.next`, `.git` lock files or
`.env`: exclude them, or keep the working copy outside OneDrive and use Git to move work between machines. Do not edit
the same branch on two machines at once without pulling first.

## Rules that still apply
Autonomy stays in `shadow` mode. No publishing to the real CMS. The live site is crawled only with a valid approval file,
the 3-second spacing and the pilot limits (`docs/pilot/KEN_PUBLIC_CRAWL_PILOT.md`, ADR-0020).
