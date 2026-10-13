/**
 * Platform bridge contract (ADR-0008).
 *
 * The ONLY way UI code reaches capabilities that differ between the web build
 * and a future Electron desktop build. Components import `platform` from
 * `@/lib/platform`; they never import Electron (ESLint blocks it).
 *
 * Rules for every member of `PlatformBridge` — they exist because a desktop
 * implementation will cross an Electron IPC boundary (preload -> main):
 *
 * 1. Methods are async and return a Promise, even when the web implementation
 *    could answer synchronously.
 * 2. Arguments and results are plain, structured-cloneable data: strings,
 *    numbers, booleans, null, arrays and plain objects. No functions, class
 *    instances, DOM nodes, Errors-as-values, Symbols or callbacks.
 * 3. No Electron type (`IpcRenderer`, `BrowserWindow`, `Event`, ...) and no
 *    Node type may appear here. This file must type-check without Electron
 *    installed.
 * 4. Each method maps to one explicit, allowlisted IPC channel in the desktop
 *    preload. There is no generic `invoke(channel, ...args)` escape hatch.
 * 5. Adding a method is a contract change: implement it in `browser.ts`, and
 *    the runtime guard in `desktop.ts` picks it up automatically.
 * 6. Business data never flows through the bridge: data comes from FastAPI over
 *    HTTP, and the UI never talks to PostgreSQL.
 */
export type PlatformKind = "web" | "desktop";

export interface PlatformBridge {
  /** Which shell is hosting the UI. */
  getPlatform(): Promise<PlatformKind>;

  /**
   * Best-effort user notification. Never prompts for permission and resolves
   * (does not reject) when notifications are unavailable or not permitted.
   */
  showNotification(
    title: string,
    body: string
  ): Promise<void>;
}
