import type { Heading } from "@/lib/api-client";

/** Headings as a nested-looking outline (indent by level). Text only. */
export function HeadingsOutline({ headings }: { readonly headings: readonly Heading[] }) {
  if (headings.length === 0) {
    return <p className="text-slate-700">This version has no headings.</p>;
  }
  return (
    <ol className="space-y-1 text-sm">
      {headings.map((heading) => (
        <li
          key={heading.block_id}
          style={{ paddingLeft: `${(Math.min(Math.max(heading.level, 1), 6) - 1) * 1.25}rem` }}
        >
          <span className="mr-2 font-mono text-xs text-slate-600">H{heading.level}</span>
          {heading.text}
        </li>
      ))}
    </ol>
  );
}

/**
 * The extracted body TEXT, in a keyboard-focusable scroll region. The stored
 * `body_html` is deliberately never accepted here: it is untrusted third-party
 * markup and the contract forbids rendering it as HTML.
 */
export function BodyText({ text, label }: { readonly text: string; readonly label: string }) {
  if (text.trim() === "") {
    return <p className="text-slate-700">No body text was extracted for this version.</p>;
  }
  return (
    <section
      aria-label={label}
      tabIndex={0}
      className="max-h-96 overflow-auto rounded border border-slate-300 bg-slate-50 p-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700"
    >
      <pre className="whitespace-pre-wrap break-words font-sans text-sm text-slate-900">{text}</pre>
    </section>
  );
}
