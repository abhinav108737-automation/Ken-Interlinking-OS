import { resolvePlatform } from "./desktop";
import type { PlatformBridge } from "./types";

/**
 * The platform bridge for this environment. Resolved once at module load: the
 * browser implementation on the server and in a plain browser; the desktop
 * bridge when a future Electron preload has exposed a valid `window.kenDesktop`.
 *
 * Server and client may therefore resolve differently. Do not branch rendered
 * markup on the platform during server rendering; call `getPlatform()` from an
 * effect or event handler instead.
 */
export const platform: PlatformBridge = resolvePlatform();

export { browserPlatform } from "./browser";
export { DESKTOP_BRIDGE_GLOBAL, isPlatformBridge, resolvePlatform } from "./desktop";

export type {
  PlatformBridge,
  PlatformKind,
} from "./types";
export type { BridgeHost } from "./desktop";
