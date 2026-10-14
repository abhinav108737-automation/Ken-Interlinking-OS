/**
 * Next.js proxy (formerly "middleware"). It does ONE thing: records the
 * requested path + query in a request header, so a server-side API 401 can
 * send the reviewer back to the same page after sign-in (lib/api.ts).
 *
 * It deliberately makes NO authentication or authorisation decision
 * (ADR-0013 section 7): it never reads the session cookie, never redirects and
 * never blocks a request. The API decides every permission.
 *
 * The header is always overwritten, so a client-supplied value never survives,
 * and lib/session.ts `safeNextPath` sanitises it again before any redirect.
 */
import { type NextRequest, NextResponse } from "next/server";

import { REQUEST_PATH_HEADER } from "@/lib/session";

export function proxy(request: NextRequest) {
  const forwarded = new Headers(request.headers);
  forwarded.set(REQUEST_PATH_HEADER, `${request.nextUrl.pathname}${request.nextUrl.search}`);
  return NextResponse.next({ request: { headers: forwarded } });
}

export const config = {
  // Pages, server actions and route handlers; never static assets.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
