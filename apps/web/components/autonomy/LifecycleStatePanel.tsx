import Link from "next/link";

import { type OperationalStateCount, isPrimaryState, rollUpStates } from "@/lib/autonomy";
import { formatCount } from "@/lib/format";
import { buildHref } from "@/lib/inventory";
import { RECOMMENDATIONS_PATH, describeLinkPlanStatus } from "@/lib/recommendations";

import { StatusBadge } from "../StatusBadge";
import { CELL, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

/** The recommendations list filtered to one contract status. */
function statusHref(status: string): string {
  return buildHref(RECOMMENDATIONS_PATH, { status });
}

function Tile({ entry }: { readonly entry: OperationalStateCount }) {
  return (
    <div className="rounded border border-slate-300 bg-white p-3">
      <p className="text-2xl font-semibold tabular-nums">{formatCount(entry.count)}</p>
      <p className="mt-1">
        <StatusBadge presentation={entry.presentation} />
      </p>
      <p className="mt-1 text-xs text-slate-700">{entry.presentation.description}</p>
    </div>
  );
}

/**
 * The organising idea of this console (ADR-0018 §3): the seven operational
 * states, not an approval queue. Rendered as tiles for scanning AND as a table
 * carrying the same numbers plus the contract statuses rolled into each, so the
 * roll-up is auditable and the figures are reachable without the tiles.
 *
 * Statuses this build does not recognise are listed separately rather than
 * dropped: a count that appears nowhere would make the total a lie.
 */
export function LifecycleStatePanel({
  counts,
  caption = "Link plans by operational state",
}: {
  readonly counts: Readonly<Record<string, number>>;
  readonly caption?: string;
}) {
  const { states, unknown, total } = rollUpStates(counts);
  const primary = states.filter((entry) => isPrimaryState(entry.state));
  const secondary = states.filter((entry) => !isPrimaryState(entry.state));
  return (
    <div className="space-y-4">
      <ul className="grid list-none gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {primary.map((entry) => (
          <li key={entry.state}>
            <Tile entry={entry} />
          </li>
        ))}
      </ul>

      <TableFrame caption={caption} count={total}>
        <thead className="bg-slate-100 text-slate-900">
          <tr>
            <th scope="col" className={`${CELL} font-semibold`}>
              Operational state
            </th>
            <th scope="col" className={`${CELL} font-semibold`}>
              Contract statuses it includes
            </th>
            <th scope="col" className={`${NUM_CELL} font-semibold`}>
              Plans
            </th>
          </tr>
        </thead>
        <tbody>
          {[...primary, ...secondary].map((entry) => (
            <tr key={entry.state} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                <StatusBadge presentation={entry.presentation} />
                {!isPrimaryState(entry.state) && (
                  <span className="ml-2 text-xs font-normal text-slate-700">(not a primary state)</span>
                )}
              </th>
              <td className={CELL}>
                <ul className="flex list-none flex-wrap gap-x-3 gap-y-1 text-xs">
                  {entry.statuses.map((status) => (
                    <li key={status}>
                      <Link href={statusHref(status)} className="text-blue-800 underline underline-offset-2">
                        {describeLinkPlanStatus(status).label}
                      </Link>
                    </li>
                  ))}
                </ul>
              </td>
              <td className={NUM_CELL}>{formatCount(entry.count)}</td>
            </tr>
          ))}
          {unknown.length > 0 &&
            unknown.map((row) => (
              <tr key={row.key} className={ROW}>
                <th scope="row" className={`${CELL} font-medium`}>
                  Unknown ({row.key})
                </th>
                <td className={CELL}>
                  <span className="text-xs text-slate-700">
                    A status this console build does not recognise. The API still reports it.
                  </span>
                </td>
                <td className={NUM_CELL}>{formatCount(row.count)}</td>
              </tr>
            ))}
        </tbody>
      </TableFrame>
    </div>
  );
}
