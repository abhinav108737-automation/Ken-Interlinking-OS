import type { PolicyDocument } from "@/lib/api-client";
import { describePolicyKind, describePolicyKindDetail } from "@/lib/audit";
import { toDisplayEntries } from "@/lib/format";

import { CELL, EmptyRow, ROW, TableFrame } from "../inventory/Table";

/** One active policy document, read-only: identity facts plus its top-level keys as text. */
export function PolicyDocumentView({ policy }: { readonly policy: PolicyDocument }) {
  const entries = toDisplayEntries(policy.content);
  const headingId = `policy-${policy.kind}-heading`;
  return (
    <section aria-labelledby={headingId} className="space-y-3 rounded border border-slate-300 p-4">
      <h2 id={headingId} className="text-xl font-semibold">
        {describePolicyKind(policy.kind)} policy
      </h2>
      {describePolicyKindDetail(policy.kind) !== null && (
        <p className="text-sm text-slate-700">{describePolicyKindDetail(policy.kind)}</p>
      )}
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="font-medium text-slate-700">Version</dt>
        <dd className="font-mono">{String(policy.version)}</dd>
        <dt className="font-medium text-slate-700">File</dt>
        <dd className="break-all font-mono text-xs">{policy.path}</dd>
        <dt className="font-medium text-slate-700">SHA-256</dt>
        <dd className="break-all font-mono text-xs">{policy.sha256}</dd>
      </dl>
      <TableFrame caption={`${describePolicyKind(policy.kind)} policy content`} count={entries.length}>
        <thead className="bg-slate-100 text-slate-900">
          <tr>
            <th scope="col" className={`${CELL} font-semibold`}>
              Key
            </th>
            <th scope="col" className={`${CELL} font-semibold`}>
              Value (JSON)
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.length === 0 ? (
            <EmptyRow colSpan={2}>The document is empty.</EmptyRow>
          ) : (
            entries.map((entry) => (
              <tr key={entry.key} className={ROW}>
                <th scope="row" className={`${CELL} align-top font-mono text-xs font-medium`}>
                  {entry.key}
                </th>
                <td className={CELL}>
                  <code className="block max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs">{entry.value}</code>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </TableFrame>
    </section>
  );
}
