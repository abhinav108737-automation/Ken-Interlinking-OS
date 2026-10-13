import {
  type DependencyCheck,
  describeCheckKind,
  describeHealthStatus,
  formatLatency,
  requirementLabel,
  summarizeCheck,
} from "@/lib/status";

import { StatusBadge } from "./StatusBadge";

export interface DependencyTableProps {
  readonly checks: readonly DependencyCheck[];
  readonly caption?: string;
}

export function DependencyTable({ checks, caption = "Dependency checks" }: DependencyTableProps) {
  return (
    <div className="overflow-x-auto rounded border border-slate-300">
      <table className="min-w-full border-collapse text-left text-sm">
        <caption className="px-3 py-2 text-left font-semibold text-slate-900">
          {caption} <span className="font-normal text-slate-700">({checks.length})</span>
        </caption>
        <thead className="bg-slate-100 text-slate-900">
          <tr>
            <th scope="col" className="px-3 py-2 font-semibold">
              Name
            </th>
            <th scope="col" className="px-3 py-2 font-semibold">
              Kind
            </th>
            <th scope="col" className="px-3 py-2 font-semibold">
              Requirement
            </th>
            <th scope="col" className="px-3 py-2 font-semibold">
              Status
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              Latency
            </th>
            <th scope="col" className="px-3 py-2 font-semibold">
              Error / detail
            </th>
          </tr>
        </thead>
        <tbody>
          {checks.length === 0 ? (
            <tr>
              <td colSpan={6} className="px-3 py-3 text-slate-700">
                The API reported no dependency checks.
              </td>
            </tr>
          ) : (
            checks.map((check) => {
              const summary = summarizeCheck(check);
              return (
                <tr key={check.name} className="border-t border-slate-200 align-top">
                  <th scope="row" className="px-3 py-2 font-mono font-medium text-slate-900">
                    {check.name}
                  </th>
                  <td className="px-3 py-2">{describeCheckKind(check.kind)}</td>
                  <td className="px-3 py-2">
                    <span className={check.required ? "font-semibold" : undefined}>
                      {requirementLabel(check.required)}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge presentation={describeHealthStatus(check.status)} />
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatLatency(check.latency_ms)}
                  </td>
                  <td className="px-3 py-2 text-slate-800">
                    {summary.error === null && summary.detail === "" ? (
                      <span className="text-slate-600">None</span>
                    ) : (
                      <>
                        {summary.error !== null && (
                          <p className="text-red-900">
                            <span className="font-semibold">Error:</span> {summary.error}
                          </p>
                        )}
                        {summary.detail !== "" && (
                          <p className="break-words font-mono text-xs text-slate-700">
                            {summary.detail}
                          </p>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
