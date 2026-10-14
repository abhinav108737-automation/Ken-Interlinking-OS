/**
 * `GET /auth/expired?next=…` — where a server-side API 401 lands (lib/api.ts).
 *
 * Rendering cannot change cookies, so this route handler does it: it clears
 * the session cookie ONLY when the API confirms (`GET /auth/me` → 401) that the
 * token it carries is dead, then continues to `/login?next=…`. A cross-site
 * request carries no SameSite=Strict cookie, so it can never log anyone out.
 */
import { redirect } from "next/navigation";
import type { NextRequest } from "next/server";

import { endSession, getSessionUser, hasSessionCookie, isUnauthenticatedError } from "@/lib/api";
import { loginHref, safeNextPath } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<never> {
  const next = safeNextPath(request.nextUrl.searchParams.get("next"));
  if (await hasSessionCookie()) {
    const me = await getSessionUser();
    if (!me.ok && isUnauthenticatedError(me.error)) {
      await endSession();
      redirect(loginHref(next, "expired"));
    }
  }
  redirect(loginHref(next));
}
