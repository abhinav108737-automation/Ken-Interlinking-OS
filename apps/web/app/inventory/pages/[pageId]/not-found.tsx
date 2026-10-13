import Link from "next/link";

export default function PageNotFound() {
  return (
    <section aria-labelledby="page-not-found-heading" className="space-y-3">
      <h1 id="page-not-found-heading" className="text-2xl font-semibold">
        Page not found
      </h1>
      <p className="max-w-prose text-slate-800">
        The inventory has no page with this id. It may have been mistyped, or the database may have
        been reset since the link was made.
      </p>
      <p>
        <Link href="/inventory" className="text-blue-800 underline underline-offset-4">
          Back to the content inventory
        </Link>
      </p>
    </section>
  );
}
