import Link from "next/link";

export default function RunNotFound() {
  return (
    <section aria-labelledby="run-not-found-heading" className="space-y-3">
      <h1 id="run-not-found-heading" className="text-2xl font-semibold">
        Run not found
      </h1>
      <p className="max-w-prose text-slate-800">
        The inventory has no ingestion run with this id. It may have been mistyped, or the database may have
        been reset since the link was made.
      </p>
      <p>
        <Link href="/inventory/runs" className="text-blue-800 underline underline-offset-4">
          Back to ingestion runs
        </Link>
      </p>
    </section>
  );
}
