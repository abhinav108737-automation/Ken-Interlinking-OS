import Link from "next/link";

import { AUDIT_EVENTS_PATH, AUDIT_REVIEWS_PATH } from "@/lib/audit";

/** Switch between the two audit views. */
export function AuditNav({ current }: { readonly current: "reviews" | "events" }) {
  const links = [
    { key: "reviews", href: AUDIT_REVIEWS_PATH, label: "Review actions" },
    { key: "events", href: AUDIT_EVENTS_PATH, label: "Event log" },
  ] as const;
  return (
    <nav aria-label="Audit views">
      <ul className="flex flex-wrap gap-4 text-sm">
        {links.map((link) => (
          <li key={link.key}>
            <Link
              href={link.href}
              aria-current={current === link.key ? "page" : undefined}
              className={`text-blue-800 underline-offset-4 hover:underline${current === link.key ? " font-semibold underline" : ""}`}
            >
              {link.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
