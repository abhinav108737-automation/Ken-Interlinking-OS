/**
 * Detection of the future Electron desktop bridge (ADR-0008).
 *
 * No Electron import and no Electron type: the desktop preload is expected to
 * call `contextBridge.exposeInMainWorld("kenDesktop", bridge)`, which makes a
 * frozen plain object available as `window.kenDesktop`. Its value is treated
 * as UNTRUSTED until it passes `isPlatformBridge`; anything malformed falls
 * back to the browser implementation instead of crashing the UI.
 */
import { browserPlatform } from "./browser";
import type { PlatformBridge, PlatformKind } from "./types";

/** Global name the desktop preload must expose the bridge under. */
export const DESKTOP_BRIDGE_GLOBAL = "kenDesktop";

declare global {
  interface Window {
    /**
     * Set only by the future Electron preload via contextBridge. Typed as
     * `unknown` on purpose: validate with `isPlatformBridge` before use.
     */
    kenDesktop?: unknown;
  }
}

/** Where the bridge is looked up. Defaults to `window` (absent on the server). */
export interface BridgeHost {
  readonly kenDesktop?: unknown;
}

// A Record over `keyof PlatformBridge` is exhaustive by construction: adding a
// method to the interface fails type-checking here until it is listed.
const BRIDGE_METHODS: Readonly<Record<keyof PlatformBridge, true>> = {
  getPlatform: true,
  showNotification: true,
};

const REQUIRED_METHODS = Object.keys(BRIDGE_METHODS) as readonly (keyof PlatformBridge)[];

/** True when `value` has every `PlatformBridge` method as a function. Never throws. */
export function isPlatformBridge(value: unknown): value is PlatformBridge {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  try {
    const candidate = value as Record<string, unknown>;
    return REQUIRED_METHODS.every((name) => typeof candidate[name] === "function");
  } catch {
    // A hostile or broken getter must not take the UI down.
    return false;
  }
}

function isPlatformKind(value: unknown): value is PlatformKind {
  return value === "web" || value === "desktop";
}

/**
 * Wrap a validated desktop bridge. Method references are captured once (later
 * mutation of `window.kenDesktop` has no effect) and results that cross the
 * IPC boundary are validated before reaching UI code.
 */
function adaptDesktopBridge(bridge: PlatformBridge): PlatformBridge {
  const { getPlatform, showNotification } = bridge;
  return Object.freeze({
    async getPlatform(): Promise<PlatformKind> {
      const kind: unknown = await getPlatform.call(bridge);
      // The value came from the desktop shell; an unrecognised answer still means desktop.
      return isPlatformKind(kind) ? kind : "desktop";
    },
    async showNotification(title: string, body: string): Promise<void> {
      await showNotification.call(bridge, title, body);
    },
  });
}

function defaultHost(): BridgeHost | undefined {
  return typeof window === "undefined" ? undefined : window;
}

/**
 * The bridge for the current environment: the desktop bridge when a valid one
 * is exposed, otherwise `browserPlatform` (always, on the server).
 */
export function resolvePlatform(host: BridgeHost | undefined = defaultHost()): PlatformBridge {
  if (host === undefined) {
    return browserPlatform;
  }
  let candidate: unknown;
  try {
    candidate = host.kenDesktop;
  } catch {
    return browserPlatform;
  }
  return isPlatformBridge(candidate) ? adaptDesktopBridge(candidate) : browserPlatform;
}
