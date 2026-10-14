"use server";
/**
 * Sign-in and sign-out (ADR-0013 sections 2 and 7). Both are server actions,
 * so Next.js applies its same-origin check; the token is stored only in the
 * HttpOnly cookie (lib/api.ts) and never returned to the browser.
 *
 * The password is never logged, stored or echoed back in the form state.
 */
import { redirect } from "next/navigation";

import { endSession, login, logout, startSession } from "@/lib/api";
import { LOGIN_PATH, type LoginFormState, safeNextPath, validateLoginInput } from "@/lib/session";

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

export async function loginAction(_previous: LoginFormState, form: FormData): Promise<LoginFormState> {
  const username = text(form, "username").trim();
  const password = text(form, "password");
  const next = safeNextPath(text(form, "next"));

  const invalid = validateLoginInput(username, password);
  if (invalid !== null) return { error: invalid, username };

  const result = await login({ username, password });
  if (!result.ok) {
    const { error } = result;
    const message =
      error.status === 401
        ? "Sign-in failed: the username or password was not accepted, or the account is temporarily locked."
        : error.status === 422
          ? "Sign-in failed: enter a valid username and password."
          : error.kind === "network" || error.kind === "timeout" || error.kind === "config"
            ? "Sign-in is unavailable: the control API could not be reached."
            : "Sign-in failed: the control API returned an unexpected response.";
    return { error: message, username };
  }
  if (!(await startSession(result.data))) {
    return { error: "Sign-in failed: the API returned an unusable session.", username };
  }
  redirect(next);
}

export async function logoutAction(): Promise<void> {
  // Revoke server-side first; an already-dead session (401) still ends here.
  await logout();
  await endSession();
  redirect(LOGIN_PATH);
}
