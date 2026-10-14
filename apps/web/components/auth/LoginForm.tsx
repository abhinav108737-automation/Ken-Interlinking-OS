"use client";

import { useActionState } from "react";

import { type LoginFormState, PASSWORD_MAX, USERNAME_MAX, USERNAME_MIN } from "@/lib/session";

export interface LoginFormProps {
  readonly action: (state: LoginFormState, form: FormData) => Promise<LoginFormState>;
  /** Safe same-origin path to continue to after sign-in. */
  readonly next: string;
}

const FIELD = "w-full rounded border border-slate-400 bg-white px-2 py-1";

/** Username/password form. The password field is never pre-filled or echoed. */
export function LoginForm({ action, next }: LoginFormProps) {
  const [state, formAction, pending] = useActionState(action, { error: null, username: "" });
  return (
    <form action={formAction} className="max-w-sm space-y-4" aria-describedby={state.error ? "login-error" : undefined}>
      <input type="hidden" name="next" value={next} />
      {state.error !== null && (
        <p id="login-error" role="alert" className="rounded border border-red-700 bg-red-50 p-3 text-sm text-red-950">
          <span aria-hidden="true">✕ </span>
          {state.error}
        </p>
      )}
      <div className="space-y-1">
        <label htmlFor="login-username" className="block font-medium text-slate-800">
          Username
        </label>
        <input
          id="login-username"
          name="username"
          type="text"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          required
          minLength={USERNAME_MIN}
          maxLength={USERNAME_MAX}
          defaultValue={state.username}
          className={FIELD}
        />
      </div>
      <div className="space-y-1">
        <label htmlFor="login-password" className="block font-medium text-slate-800">
          Password
        </label>
        <input
          id="login-password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={PASSWORD_MAX}
          className={FIELD}
        />
      </div>
      <button
        type="submit"
        disabled={pending}
        aria-disabled={pending}
        className="rounded border border-blue-800 bg-blue-800 px-4 py-1.5 font-medium text-white hover:bg-blue-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-60"
      >
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
