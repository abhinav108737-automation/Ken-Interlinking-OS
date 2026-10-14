/**
 * Primary navigation model. Pure and portable.
 */
export interface NavLink {
  readonly href: string;
  readonly label: string;
}

export const NAV_LINKS: readonly NavLink[] = [
  { href: "/", label: "Home" },
  { href: "/inventory", label: "Inventory" },
  { href: "/inventory/runs", label: "Runs" },
  { href: "/intelligence", label: "Intelligence" },
  { href: "/recommendations", label: "Recommendations" },
  { href: "/jobs", label: "Jobs" },
  { href: "/system", label: "System" },
];

function matches(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * The href of the nav entry that owns `pathname`: the longest matching prefix,
 * so `/inventory/runs/x` belongs to "Runs", not "Inventory". `null` if none.
 */
export function activeNavHref(pathname: string | null, links: readonly NavLink[] = NAV_LINKS): string | null {
  if (pathname === null) {
    return null;
  }
  let best: string | null = null;
  for (const link of links) {
    if (matches(pathname, link.href) && (best === null || link.href.length > best.length)) {
      best = link.href;
    }
  }
  return best;
}
