# ADR-0008 — Electron readiness

- **Status:** Accepted (user decision). Electron itself is **deferred**; the architecture must stay
  compatible with it.
- **Date:** 2026-10-13 (rules authored by the user; enforcement added in the post-Phase-0 cleanup)
- **Phase:** 0 / 0.5

## Context

The product may later ship as an Electron desktop application as well as a web application. Doing
nothing now would let the UI drift into patterns that are expensive to undo later: components importing
Electron or Node APIs, data access that bypasses the API, or a bridge that exposes raw IPC. The
desktop app is not being built yet, so the goal is **compatibility, not implementation**.

The same prospect of desktop distribution is why the database driver moved from LGPL psycopg to
Apache-2.0 asyncpg (ADR-0004).

## Decision

The user's architecture rules, verbatim:

- apps/web is the canonical React/Next.js UI
- future Electron lives under apps/desktop
- React components must not import electron directly
- desktop-only features go through a typed platform bridge
- UI talks to FastAPI, never directly to PostgreSQL
- OpenAPI remains the source of truth for generated TypeScript types
- Electron nodeIntegration is not used by app UI
- contextIsolation stays enabled
- preload exposes only a narrow typed API
- Electron is deferred, but the architecture must remain compatible

Made concrete:

1. **The bridge** is `PlatformBridge` (`apps/web/lib/platform/types.ts`). Its methods are async; their
   arguments and results are plain, structured-cloneable data, because they will cross an IPC boundary;
   no Electron or Node type may appear in it. Components import `platform` from `@/lib/platform`.
2. **The future preload** exposes exactly that interface as `window.kenDesktop` via
   `contextBridge.exposeInMainWorld`, with **one allowlisted IPC channel per method** and no generic
   `invoke(channel, …)` / `send` passthrough.
3. **The future `BrowserWindow`** uses `contextIsolation: true`, `nodeIntegration: false`,
   `sandbox: true`, and no `remote` module.
4. **All business data** flows renderer → FastAPI over HTTP. No database access from the renderer, the
   preload or the main process. The bridge never carries business data.
5. **Electron and its tooling** (`electron`, `electron-builder`, `electron-forge`) are **not installed**.
   Adding them is an explicit user decision.

## Enforcement — what exists today

| Mechanism | Where | What it guarantees |
|---|---|---|
| **Electron import guard** | `apps/web/eslint.config.mjs` | `no-restricted-imports` + `no-restricted-syntax` reject `electron`, `electron/*`, `@electron/*` and `@electron-forge/*` everywhere in `apps/web`: static, `import type`, side-effect, re-export, `require(…)`, dynamic `import(…)`, TS `import x = require(…)`, `typeof import(…)`, and `window`/`globalThis`/`self`/`global` `.require(…)` (the classic `nodeIntegration` pattern). Part of `make lint` / `make verify`. |
| **Portability guard** | same file | `components/**` and **all of `lib/**` by default** (only `lib/api.ts`, the deliberate server entry, is exempt — adding another is a reviewable one-line change) may not import Node built-ins, `server-only`, server-only Next.js APIs (`next/headers`, `next/server`, `next/cache`), database clients (`pg`, `postgres`, `@prisma/*`, `drizzle-orm`, `knex`, `typeorm`, `sequelize`, …) or the env-reading `@/lib/api`, nor use `process` / `Buffer` / `__dirname` / `require` — including via `globalThis.` / `window.`. |
| **Guard tests** | `apps/web/tests/electron-guard.test.ts`, `portability-guard.test.ts` | Prove each rule fires for every forbidden form, and assert that **no Electron package** appears in `apps/web/package.json`, the root `package.json` or the root lockfile. |
| **Runtime bridge resolution** | `apps/web/lib/platform/desktop.ts` | `resolvePlatform()` uses `window.kenDesktop` only if it structurally satisfies `PlatformBridge`; otherwise, and always on the server, it falls back to `browserPlatform`. A malformed bridge can never crash the UI. |
| **Environment-agnostic API core** | `apps/web/lib/api-client.ts` | `createApiClient({ baseUrl, fetch?, timeoutMs? })` has no dependency on `process`, `window`, Node or Electron. `lib/api.ts` is the **only** place that reads `KEN_API_BASE_URL`. |

Verified 2026-10-13: a real component file importing `electron` (and `node:fs`) fails `npm run lint`.

**Limits, stated plainly:** lint guards catch accidental imports only. They do not catch computed or
aliased specifiers and calls (`require(name)`, `const r = window.require; r(…)`, `const g = globalThis;
g.process`), `eval` / `new Function`, `eslint-disable` comments, anything outside `apps/web` or in a
listed server entry, transitive dependencies that use Node internally, or Electron packages nested below
the top level of the lockfile. **Runtime safety in a future desktop build comes from
`contextIsolation: true`, `nodeIntegration: false` and `sandbox: true` — not from lint.**

## Consequences

- `apps/web` stays the single UI codebase. `apps/desktop` holds only a README until the desktop app is
  scheduled.
- **Open decision, deferred until Electron is actually built:** `/system` is currently server-rendered
  by Next.js. A desktop build must either
  1. run the Next.js standalone server locally as a sidecar, keeping server rendering; or
  2. move data fetching client-side with `createApiClient`, which requires CORS on FastAPI.

  Record the choice in a follow-up ADR.
  **Constrained by ADR-0013 (Phase 5):** the web app keeps the session token in an HttpOnly
  cookie and forwards it server-side, so option 1 (the local Next server as a sidecar) is the
  path that works unchanged. Option 2 would need its own token-custody design (no token in
  renderer-accessible storage).
- **Licence check for desktop packaging:** `sharp` (LGPL) is excluded from the web runtime image by
  Next's output trace, but desktop packagers copy `node_modules` differently. Re-verify the packaged
  bundle's licence inventory before any distribution (see ADR-0004).
