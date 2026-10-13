# Desktop App

Reserved for the future Electron application.

The Electron app will reuse the existing React/Next.js UI and communicate with the existing FastAPI backend.

Electron is intentionally not installed during Phase 0.

## Security requirements for the future Electron app

These are binding for whoever builds the desktop shell (see ADR-0008).

- **Electron is intentionally not installed.** Adding `electron`, `electron-builder`,
  `electron-forge` / `@electron-forge/*` or any other Electron tooling requires an explicit
  user decision (new dependency + new runtime). `apps/web/tests/electron-guard.test.ts` fails
  if one appears in `apps/web/package.json`, the root `package.json` or `package-lock.json`;
  that test must be updated deliberately as part of that decision.
- **BrowserWindow `webPreferences`:** `contextIsolation: true`, `nodeIntegration: false`,
  `sandbox: true`. Do not enable `webviewTag`, `nodeIntegrationInWorker` or
  `nodeIntegrationInSubFrames`, and keep `webSecurity` on.
- **No `remote` module** (`@electron/remote` included).
- **Preload exposes only the typed bridge.** The preload calls
  `contextBridge.exposeInMainWorld("kenDesktop", bridge)` where `bridge` implements exactly
  `PlatformBridge` from `apps/web/lib/platform/types.ts` — nothing more. Never expose
  `ipcRenderer`, `require`, `process` or any Electron/Node object to the renderer.
- **Explicit, allowlisted IPC.** One named channel per bridge method (e.g.
  `ken:platform:get`, `ken:notification:show`), handled with `ipcMain.handle` in the main
  process, which validates every argument. No generic `invoke(channel, ...)` / `send(channel, ...)`
  passthrough. Arguments and results are structured-cloneable plain data.
- **Renderer → FastAPI over HTTP only.** All business data comes from the control API, exactly as
  in the browser. Neither the renderer nor the preload connects to PostgreSQL (or any database),
  and the main process does not proxy database access either.
- **How the web app detects the shell.** `apps/web/lib/platform` checks `window.kenDesktop` at
  runtime, validates its shape, and falls back to the browser implementation when it is missing or
  malformed. React components import `platform` from `@/lib/platform` and never import Electron —
  ESLint rejects `electron`, `electron/*` and `@electron/*` in `apps/web` (static, `require()` and
  dynamic `import()` forms).
- **Navigation and content:** load only the app's own origin; deny `window.open` and in-app
  navigation to other origins (`setWindowOpenHandler`, `will-navigate`) and open external links
  with `shell.openExternal` after allowlisting; keep a restrictive Content-Security-Policy.

### Open decision: how the desktop app serves the UI

`apps/web` currently renders `/system` with Next.js **server rendering** (`dynamic = "force-dynamic"`):
the readiness/meta calls run on the Next.js server, which reads `KEN_API_BASE_URL`. A static export
alone cannot serve that page. When Electron is actually built, choose one of:

1. **Sidecar:** run the Next.js standalone server (`output: "standalone"`, already configured) as a
   local child process bound to `127.0.0.1`, and load it in the BrowserWindow; or
2. **Client-side fetching:** move that data fetching into the client using the environment-agnostic
   `createApiClient({ baseUrl, fetch })` from `apps/web/lib/api-client.ts`, with the base URL
   provided by the desktop shell (and CORS configured on FastAPI accordingly).

This is deliberately left open until the desktop app is scheduled; it needs an ADR at that time.
