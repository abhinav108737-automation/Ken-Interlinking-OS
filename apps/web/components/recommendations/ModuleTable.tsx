import Link from "next/link";

import type { PageModuleView } from "@/lib/api-client";
import { formatDateTime } from "@/lib/format";
import { jobRunHref } from "@/lib/intelligence";
import { pageDisplayTitle } from "@/lib/inventory";
import { describeReasonCode } from "@/lib/recommendations";

import { pageHref } from "../inventory/PageInventoryTable";
import { CELL, EmptyRow, HeaderRow, ROW, TableFrame } from "../inventory/Table";

const COLUMNS = ["Page", "Proposed items", "Computed"];

/**
 * One module's items, in the order they would render.
 *
 * The rank is shown because it is the editorial claim: item 1 is the one the reader is most
 * likely to want. The reason codes are shown beside each item for the same reason the plan
 * screens show them — a reviewer should never have to guess why something was proposed.
 */
function Items({ module }: { readonly module: PageModuleView }) {
  if (module.items.length === 0) return <span className="text-slate-600">—</span>;
  return (
    <ol className="space-y-2">
      {module.items.map((item) => (
        <li key={item.target_page_id} className="flex gap-2">
          <span className="font-mono text-xs text-slate-600" aria-hidden="true">
            {item.rank}.
          </span>
          <span>
            <Link
              href={pageHref(item.target_page_id)}
              className="break-words text-blue-800 underline underline-offset-2"
            >
              {item.target_title ?? item.target_url}
            </Link>
            {item.geography_label === null ? null : (
              <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">
                {item.geography_label}
              </span>
            )}
            <span className="block text-xs text-slate-700">
              {item.reason_codes.map(describeReasonCode).join(" · ")}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}

/** Related-report modules, one row per page, items in render order (API order). */
export function ModuleTable({
  caption,
  modules,
  total,
  emptyMessage,
}: {
  readonly caption: string;
  readonly modules: readonly PageModuleView[];
  readonly total: number;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption={caption} count={total}>
      <HeaderRow columns={COLUMNS} />
      <tbody>
        {modules.length === 0 ? (
          <EmptyRow colSpan={COLUMNS.length}>{emptyMessage}</EmptyRow>
        ) : (
          modules.map((module) => (
            <tr key={`${module.kind}:${module.page_id}`} className={ROW}>
              <th scope="row" className={`${CELL} align-top font-medium`}>
                <Link
                  href={pageHref(module.page_id)}
                  className="break-words text-blue-800 underline underline-offset-2"
                >
                  {pageDisplayTitle(module)}
                </Link>
                <span className="block break-all font-mono text-xs font-normal text-slate-600">
                  {module.canonical_url}
                </span>
                <span className="mt-1 block text-xs font-normal text-slate-700">
                  {module.market_core}
                  {module.geography_label === null ? " · global" : ` · ${module.geography_label}`}
                </span>
              </th>
              <td className={`${CELL} align-top`}>
                <Items module={module} />
              </td>
              <td className={`${CELL} align-top whitespace-nowrap`}>
                <time dateTime={module.computed_at}>{formatDateTime(module.computed_at)}</time>
                <Link href={jobRunHref(module.run_id)} className="block text-xs text-blue-800 underline underline-offset-2">
                  Run <span className="sr-only">{module.run_id}</span>
                </Link>
                <span className="block font-mono text-xs text-slate-600">{module.analysis_version}</span>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
