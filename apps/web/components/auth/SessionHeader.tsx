import Link from "next/link";

import type { PrincipalView } from "@/lib/api-client";
import { describeRole, describeRoleDetail } from "@/lib/auth";
import { LOGIN_PATH } from "@/lib/session";

export interface SessionHeaderProps {
  /** The signed-in user, or `null` when signed out (or the API is unreachable). */
  readonly user: PrincipalView | null;
  /** Logout server action (POST with Next's same-origin check). */
  readonly logoutAction: () => Promise<void>;
}

/** Who is signed in, their role, and sign-out. Text only. */
export function SessionHeader({ user, logoutAction }: SessionHeaderProps) {
  if (user === null) {
    return (
      <p className="text-sm">
        <Link href={LOGIN_PATH} className="text-blue-800 underline underline-offset-4">
          Sign in
        </Link>
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm" aria-label="Signed-in user" role="group">
      <p>
        <span className="sr-only">Signed in as </span>
        <span className="font-medium">{user.display_name}</span>{" "}
        <span className="font-mono text-xs text-slate-700">({user.username})</span>
      </p>
      <p className="rounded border border-slate-400 bg-white px-2 py-0.5 text-xs" title={describeRoleDetail(user.role)}>
        <span className="sr-only">Role: </span>
        {describeRole(user.role)}
      </p>
      <form action={logoutAction}>
        <button
          type="submit"
          className="rounded border border-slate-500 bg-white px-2 py-0.5 text-slate-900 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700"
        >
          Sign out
        </button>
      </form>
    </div>
  );
}
