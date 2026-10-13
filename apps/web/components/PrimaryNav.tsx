"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { NAV_LINKS, type NavLink, activeNavHref } from "@/lib/nav";

export interface PrimaryNavProps {
  readonly links?: readonly NavLink[];
}

/** Primary navigation. The current section is marked with `aria-current="page"` and in text weight. */
export function PrimaryNav({ links = NAV_LINKS }: PrimaryNavProps) {
  const active = activeNavHref(usePathname(), links);
  return (
    <nav aria-label="Primary">
      <ul className="flex gap-4">
        {links.map((link) => {
          const current = link.href === active;
          return (
            <li key={link.href}>
              <Link
                href={link.href}
                aria-current={current ? "page" : undefined}
                className={`rounded px-1 text-blue-800 underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700${current ? " font-semibold underline" : ""}`}
              >
                {link.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
