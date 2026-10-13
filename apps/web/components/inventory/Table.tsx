import type { ReactNode } from "react";

/** Shared table chrome for the inventory screens, matching DependencyTable. */
export function TableFrame({
  caption,
  count,
  children,
}: {
  readonly caption: string;
  readonly count?: number;
  readonly children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto rounded border border-slate-300">
      <table className="min-w-full border-collapse text-left text-sm">
        <caption className="px-3 py-2 text-left font-semibold text-slate-900">
          {caption}
          {count !== undefined && <span className="font-normal text-slate-700"> ({count})</span>}
        </caption>
        {children}
      </table>
    </div>
  );
}

export function HeaderRow({ columns }: { readonly columns: readonly string[] }) {
  return (
    <thead className="bg-slate-100 text-slate-900">
      <tr>
        {columns.map((column) => (
          <th key={column} scope="col" className="whitespace-nowrap px-3 py-2 font-semibold">
            {column}
          </th>
        ))}
      </tr>
    </thead>
  );
}

export function EmptyRow({ colSpan, children }: { readonly colSpan: number; readonly children: ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-3 text-slate-700">
        {children}
      </td>
    </tr>
  );
}

export const CELL = "px-3 py-2";
export const NUM_CELL = "px-3 py-2 text-right tabular-nums";
export const ROW = "border-t border-slate-200 align-top";
