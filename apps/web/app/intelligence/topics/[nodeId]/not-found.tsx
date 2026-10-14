import Link from "next/link";

export default function TaxonomyNodeNotFound() {
  return (
    <section aria-labelledby="node-not-found-heading" className="space-y-3">
      <h1 id="node-not-found-heading" className="text-2xl font-semibold">
        Taxonomy node not found
      </h1>
      <p className="max-w-prose text-slate-800">
        The active taxonomy has no market or geography with this id. It may have been mistyped, or the
        taxonomy may have changed since the link was made.
      </p>
      <p>
        <Link href="/intelligence" className="text-blue-800 underline underline-offset-4">
          Back to content intelligence
        </Link>
      </p>
    </section>
  );
}
