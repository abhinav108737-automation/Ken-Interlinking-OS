/**
 * Primary navigation model. Pure and portable.
 *
 * An entry may name the permission it needs; `navLinksFor` hides it from a
 * session without that permission (the API enforces it regardless).
 */
import type { Permission } from "./api-client";
import type { PermissionSet } from "./auth";

export interface NavLink {
  readonly href: string;
  readonly label: string;
  /** Hide this entry unless the session has this permission. */
  readonly requires?: Permission;
}

export const NAV_LINKS: readonly NavLink[] = [
  { href: "/", label: "Dashboard" },
  { href: "/inventory", label: "Inventory" },
  { href: "/inventory/runs", label: "Runs" },
  { href: "/intelligence", label: "Intelligence" },
  { href: "/recommendations", label: "Recommendations" },
  { href: "/agents", label: "Agents" },
  { href: "/deployments", label: "Deployments" },
  { href: "/analytics", label: "Analytics" },
  { href: "/jobs", label: "Jobs" },
  { href: "/audit/reviews", label: "Audit", requires: "read_audit" },
  { href: "/settings/policies", label: "Settings", requires: "read_settings" },
  { href: "/system", label: "System" },
];

/** The entries a session may see. With no session, only entries that need no permission. */
export function navLinksFor(permissions: PermissionSet, links: readonly NavLink[] = NAV_LINKS): NavLink[] {
  return links.filter((link) => link.requires === undefined || permissions.has(link.requires));
}

function matches(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Section prefixes owned by a nav entry whose href is one of their sub-pages. */
const SECTION_ALIASES: Readonly<Record<string, string>> = {
  "/audit": "/audit/reviews",
  "/settings": "/settings/policies",
};

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
  if (best === null) {
    for (const [prefix, href] of Object.entries(SECTION_ALIASES)) {
      if (matches(pathname, prefix) && links.some((link) => link.href === href)) return href;
    }
  }
  return best;
}
