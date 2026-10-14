import type { Metadata } from "next";

import { loginAction } from "@/app/auth/actions";
import { LoginForm } from "@/components/auth/LoginForm";
import { type SearchParams, firstParam } from "@/lib/inventory";
import { safeNextPath } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Sign in",
};

export default async function LoginPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const next = safeNextPath(firstParam(params, "next"));
  const expired = firstParam(params, "reason") === "expired";
  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Sign in</h1>
        <p className="max-w-prose text-slate-800">
          Sign in with the account an operator created for you (<code>make user-create</code>). There are no default
          accounts.
        </p>
      </header>
      {expired && (
        <p role="status" className="max-w-prose rounded border border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
          <span aria-hidden="true">! </span>
          Your session has ended. Sign in again to continue.
        </p>
      )}
      <LoginForm action={loginAction} next={next} />
    </div>
  );
}
