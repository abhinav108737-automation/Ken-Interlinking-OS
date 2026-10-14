/**
 * Pure session helpers for the web tier (ADR-0013 section 2).
 *
 * The API accepts only `Authorization: Bearer <token>`. The web app keeps the
 * token in ITS OWN HttpOnly, SameSite=Strict cookie and forwards it from the
 * server (lib/api.ts, the one module that reads the cookie). Browser
 * JavaScript never sees the token, and an Electron shell that loads this web
 * app inherits the same custody (ADR-0008).
 *
 * Nothing here touches cookies, headers or the environment: these are pure
 * functions of their arguments, so they are unit-tested directly and stay
 * portable. Authorisation is never decided here; the API decides.
 */

/** Name of the web app's session cookie. Not readable from JavaScript (HttpOnly). */
export const SESSION_COOKIE = "ken_session";

/**
 * Request header the proxy (`proxy.ts`) sets to the requested path + query, so
 * a server-side 401 can send the reviewer back where they were after sign-in.
 * The proxy always overwrites it; a client-supplied value never survives.
 */
export const REQUEST_PATH_HEADER = "x-ken-request-path";

export const LOGIN_PATH = "/login";
/** Route handler that clears a session cookie the API no longer accepts, then goes to /login. */
export const SESSION_EXPIRED_PATH = "/auth/expired";

/** The API's session lifetime (ken_contracts.auth.SESSION_TTL_HOURS); a cookie never outlives it. */
export const MAX_SESSION_SECONDS = 12 * 60 * 60;

export interface SessionCookieOptions {
  readonly httpOnly: true;
  readonly sameSite: "strict";
  readonly path: "/";
  readonly secure: boolean;
  readonly maxAge: number;
}

/**
 * Seconds until `expiresAt` (the session's remaining TTL), floored and capped at
 * the API's 12-hour lifetime. `null` when the time is unparseable or already
 * past: such a session must not be stored.
 */
export function sessionMaxAgeSeconds(expiresAt: string, nowMs: number): number | null {
  const expires = Date.parse(expiresAt);
  if (!Number.isFinite(expires) || !Number.isFinite(nowMs)) return null;
  const seconds = Math.floor((expires - nowMs) / 1000);
  if (seconds <= 0) return null;
  return Math.min(seconds, MAX_SESSION_SECONDS);
}

/** Attributes of the session cookie. HttpOnly, SameSite=Strict and Path=/ are not configurable. */
export function sessionCookieOptions(maxAge: number, secure: boolean): SessionCookieOptions {
  return { httpOnly: true, sameSite: "strict", path: "/", secure, maxAge };
}

/** Attributes that delete the session cookie (same name, path and flags; Max-Age 0). */
export function clearedSessionCookieOptions(secure: boolean): SessionCookieOptions {
  return sessionCookieOptions(0, secure);
}

const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/** Host name of a `Host` header value (`127.0.0.1:3000` → `127.0.0.1`, `[::1]:3000` → `[::1]`). */
function hostnameOf(host: string): string {
  const trimmed = host.trim().toLowerCase();
  if (trimmed.startsWith("[")) {
    const close = trimmed.indexOf("]");
    return close === -1 ? trimmed : trimmed.slice(0, close + 1);
  }
  const colon = trimmed.indexOf(":");
  return colon === -1 ? trimmed : trimmed.slice(0, colon);
}

/** True for a loopback `Host` (local development: `localhost`, `127.0.0.1`, `[::1]`, `*.localhost`). */
export function isLoopbackHost(host: string | null | undefined): boolean {
  if (typeof host !== "string" || host.trim() === "") return false;
  const name = hostnameOf(host);
  return LOOPBACK_HOSTNAMES.has(name) || name.endsWith(".localhost");
}

/**
 * Whether the session cookie carries `Secure`. Decided per request, without an
 * environment variable: always Secure, EXCEPT plain-HTTP local development on
 * a loopback host (the compose file publishes the web app on 127.0.0.1 only).
 * Behind a TLS proxy (`X-Forwarded-Proto: https`) it is always Secure.
 */
export function shouldUseSecureCookie(host: string | null | undefined, forwardedProto: string | null | undefined): boolean {
  const proto = (forwardedProto ?? "").split(",")[0]?.trim().toLowerCase() ?? "";
  if (proto === "https") return true;
  return !isLoopbackHost(host);
}

const NEXT_BASE = "http://ken.invalid";
/** Router-internal query parameters that must never be replayed in a redirect. */
const INTERNAL_PARAMS = ["_rsc"];

/**
 * A safe same-origin path to return to after sign-in, or `fallback`.
 *
 * Accepts only an absolute path on this origin: `/x?y`. Rejects schemes,
 * protocol-relative (`//evil`), backslash tricks (`/\evil`), control
 * characters, and the sign-in routes themselves (no redirect loops).
 */
export function safeNextPath(raw: unknown, fallback = "/"): string {
  if (typeof raw !== "string" || raw === "" || raw.length > 2048) return fallback;
  if (!raw.startsWith("/") || raw.startsWith("//")) return fallback;
  if (/[\\\u0000-\u001f\u007f]/.test(raw)) return fallback;
  let url: URL;
  try {
    url = new URL(raw, NEXT_BASE);
  } catch {
    return fallback;
  }
  if (url.origin !== NEXT_BASE) return fallback;
  for (const name of INTERNAL_PARAMS) url.searchParams.delete(name);
  const path = `${url.pathname}${url.search}`;
  if (url.pathname === LOGIN_PATH || url.pathname.startsWith("/auth/")) return fallback;
  return path;
}

function withNext(base: string, next: string, extra: Readonly<Record<string, string>> = {}): string {
  const params = new URLSearchParams(extra);
  const safe = safeNextPath(next);
  if (safe !== "/") params.set("next", safe);
  const query = params.toString();
  return query === "" ? base : `${base}?${query}`;
}

/** `/login`, carrying a safe `next`. */
export function loginHref(next: string = "/", reason?: "expired"): string {
  return withNext(LOGIN_PATH, next, reason === undefined ? {} : { reason });
}

/** `/auth/expired`, carrying a safe `next`. */
export function sessionExpiredHref(next: string = "/"): string {
  return withNext(SESSION_EXPIRED_PATH, next);
}

/** Login form limits, as `ken_contracts.auth.LoginRequest` declares them. */
export const USERNAME_MIN = 3;
export const USERNAME_MAX = 64;
export const PASSWORD_MAX = 256;

/** Client-side check of the login form; the API is the authority. `null` when acceptable. */
export function validateLoginInput(username: string, password: string): string | null {
  if (username.length < USERNAME_MIN || username.length > USERNAME_MAX) {
    return `Enter a username of ${USERNAME_MIN} to ${USERNAME_MAX} characters.`;
  }
  if (password.length === 0 || password.length > PASSWORD_MAX) {
    return `Enter a password of 1 to ${PASSWORD_MAX} characters.`;
  }
  return null;
}

/** State of the sign-in form (server action result). Never contains the password. */
export interface LoginFormState {
  readonly error: string | null;
  /** Echoed so the field keeps its value. */
  readonly username: string;
}
