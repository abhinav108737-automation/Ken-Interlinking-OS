import { IMPORT_COMMANDS } from "@/lib/analytics";

/**
 * The empty state when nothing has been imported. It shows the exact commands,
 * because imports are CLI-only: no button here may trigger an external fetch
 * (ADR-0015 sections 6 and 12.8).
 */
export function NoAnalyticsData({
  heading = "No analytics data yet",
}: {
  readonly heading?: string;
}) {
  return (
    <div role="status" className="space-y-3 rounded border border-slate-300 bg-slate-50 p-4 text-slate-900">
      <p className="font-semibold">{heading}</p>
      <p className="max-w-prose text-sm">
        Nothing has been imported or aggregated, so there is nothing to show. Imports run from the command line only —
        this console never fetches from an analytics provider. Run one of these from the repository root:
      </p>
      <dl className="space-y-2">
        {IMPORT_COMMANDS.map((entry) => (
          <div key={entry.command}>
            <dt>
              <code className="block break-all rounded bg-white px-2 py-1 font-mono text-xs text-slate-900">
                {entry.command}
              </code>
            </dt>
            <dd className="mt-0.5 text-xs text-slate-700">{entry.what}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
