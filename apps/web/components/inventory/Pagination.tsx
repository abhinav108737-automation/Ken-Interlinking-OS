import Link from "next/link";

import type { PageInfo } from "@/lib/api-client";
import { buildHref, pageWindow } from "@/lib/inventory";
import { formatCount } from "@/lib/format";

export interface PaginationProps {
  readonly page: PageInfo;
  readonly itemCount: number;
  /** Route the links point at, e.g. `/inventory`. */
  readonly pathname: string;
  /** Current query parameters to preserve (filters, other sections' offsets). */
  readonly params: Readonly<Record<string, string | number | null | undefined>>;
  /** Query parameter carrying this list's offset. */
  readonly offsetParam?: string;
  /** Accessible name, e.g. "Pages pagination". */
  readonly label: string;
}

/** Offset/limit pagination with plain previous/next links (GET navigation only). */
export function Pagination({
  page,
  itemCount,
  pathname,
  params,
  offsetParam = "offset",
  label,
}: PaginationProps) {
  const range = pageWindow(page, itemCount);
  const hrefFor = (offset: number) =>
    buildHref(pathname, { ...params, [offsetParam]: offset === 0 ? undefined : offset });
  const linkClass =
    "rounded border border-slate-400 px-2 py-1 text-blue-800 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700";

  return (
    <nav aria-label={label} className="flex flex-wrap items-center gap-3 text-sm">
      <p className="text-slate-700">
        {range.first === 0
          ? `Showing 0 of ${formatCount(range.total)}`
          : `Showing ${formatCount(range.first)}–${formatCount(range.last)} of ${formatCount(range.total)}`}
      </p>
      {range.prevOffset !== null && (
        <Link href={hrefFor(range.prevOffset)} className={linkClass} rel="prev">
          ← Previous
        </Link>
      )}
      {range.nextOffset !== null && (
        <Link href={hrefFor(range.nextOffset)} className={linkClass} rel="next">
          Next →
        </Link>
      )}
    </nav>
  );
}
