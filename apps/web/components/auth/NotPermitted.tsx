import type { Permission } from "@/lib/api-client";
import { describePermission } from "@/lib/auth";

/** Shown instead of a screen the session's role may not open. The API refuses it anyway. */
export function NotPermitted({ permission, what }: { readonly permission: Permission; readonly what: string }) {
  return (
    <div role="alert" className="max-w-prose rounded border border-slate-500 bg-slate-50 p-4 text-slate-900">
      <h2 className="font-semibold">Not available for your role</h2>
      <p className="mt-1 text-sm">
        {what} requires the permission “{describePermission(permission)}”, which your role does not have. Ask an
        operator if you need access.
      </p>
    </div>
  );
}
