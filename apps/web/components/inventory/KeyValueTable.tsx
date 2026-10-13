import { CELL, EmptyRow, ROW, TableFrame } from "./Table";

export interface KeyValueRow {
  readonly key: string;
  readonly value: string;
}

/** Key/value pairs rendered strictly as text. */
export function KeyValueTable({
  caption,
  rows,
  keyHeader = "Key",
  valueHeader = "Value",
  emptyMessage,
}: {
  readonly caption: string;
  readonly rows: readonly KeyValueRow[];
  readonly keyHeader?: string;
  readonly valueHeader?: string;
  readonly emptyMessage: string;
}) {
  return (
    <TableFrame caption={caption} count={rows.length}>
      <thead className="bg-slate-100 text-slate-900">
        <tr>
          <th scope="col" className={`${CELL} font-semibold`}>
            {keyHeader}
          </th>
          <th scope="col" className={`${CELL} font-semibold`}>
            {valueHeader}
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={2}>{emptyMessage}</EmptyRow>
        ) : (
          rows.map((row) => (
            <tr key={row.key} className={ROW}>
              <th scope="row" className={`${CELL} font-mono text-xs font-medium`}>
                {row.key}
              </th>
              <td className={`${CELL} break-all font-mono text-xs`}>{row.value}</td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
