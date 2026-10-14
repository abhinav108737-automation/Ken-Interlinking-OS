import Link from "next/link";

import type { PageTypeView, TaxonomyNodeView } from "@/lib/api-client";
import { formatCount } from "@/lib/format";
import {
  type TaxonomyTreeNode,
  buildTaxonomyTree,
  describeCommercialImportance,
  describeIntent,
  describeNodeKind,
  taxonomyNodeHref,
  yesNo,
} from "@/lib/intelligence";

import { CELL, EmptyRow, HeaderRow, NUM_CELL, ROW, TableFrame } from "../inventory/Table";

function pageCountText(count: number): string {
  return `${formatCount(count)} ${count === 1 ? "page" : "pages"}`;
}

function TreeItems({ items, showKind }: { readonly items: readonly TaxonomyTreeNode[]; readonly showKind: boolean }) {
  return (
    <>
      {items.map(({ node, children }) => (
        <li key={node.id}>
          <span className="flex flex-wrap items-baseline gap-x-2">
            <Link href={taxonomyNodeHref(node.id)} className="text-blue-800 underline underline-offset-2">
              {node.label}
            </Link>
            {showKind && <span className="text-xs text-slate-700">{describeNodeKind(node.kind)}</span>}
            <span className="text-sm tabular-nums text-slate-700">{pageCountText(node.page_count)}</span>
          </span>
          {children.length > 0 && (
            <ul className="ml-5 mt-1 space-y-1 border-l border-slate-300 pl-3">
              <TreeItems items={children} showKind={showKind} />
            </ul>
          )}
        </li>
      ))}
    </>
  );
}

/**
 * A taxonomy (markets or geographies) as a real nested list, so assistive
 * technology announces the hierarchy and level. Each node links to its pages.
 */
export function TaxonomyTree({
  nodes,
  label,
  showKind = false,
  emptyMessage,
}: {
  readonly nodes: readonly TaxonomyNodeView[];
  /** Accessible name of the tree, e.g. "Markets". */
  readonly label: string;
  readonly showKind?: boolean;
  readonly emptyMessage: string;
}) {
  if (nodes.length === 0) {
    return <p className="text-slate-700">{emptyMessage}</p>;
  }
  return (
    <ul aria-label={label} className="space-y-1 text-sm">
      <TreeItems items={buildTaxonomyTree(nodes)} showKind={showKind} />
    </ul>
  );
}

const PAGE_TYPE_COLUMNS = ["Page type", "Intent", "Commercial importance", "Linkable as source", "Linkable as target", "Pages"];

export function PageTypeTable({ pageTypes }: { readonly pageTypes: readonly PageTypeView[] }) {
  return (
    <TableFrame caption="Page types" count={pageTypes.length}>
      <HeaderRow columns={PAGE_TYPE_COLUMNS} />
      <tbody>
        {pageTypes.length === 0 ? (
          <EmptyRow colSpan={PAGE_TYPE_COLUMNS.length}>The taxonomy defines no page types.</EmptyRow>
        ) : (
          pageTypes.map((type) => (
            <tr key={type.id} className={ROW}>
              <th scope="row" className={`${CELL} font-medium`}>
                {type.label}
                <span className="block font-mono text-xs font-normal text-slate-600">{type.id}</span>
              </th>
              <td className={CELL}>{describeIntent(type.intent)}</td>
              <td className={CELL}>{describeCommercialImportance(type.commercial_importance)}</td>
              <td className={CELL}>{yesNo(type.linkable_as_source)}</td>
              <td className={CELL}>{yesNo(type.linkable_as_target)}</td>
              <td className={NUM_CELL}>{formatCount(type.page_count)}</td>
            </tr>
          ))
        )}
      </tbody>
    </TableFrame>
  );
}
