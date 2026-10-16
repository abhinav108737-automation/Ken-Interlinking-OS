import Link from "next/link";

export default function BlockedActionNotFound() {
  return (
    <section aria-labelledby="blocked-not-found-heading" className="space-y-3">
      <h1 id="blocked-not-found-heading" className="text-2xl font-semibold">
        No such plan
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no recommendation with this id, so there is nothing to investigate. The id may have been mistyped, or
        the database may have been reset since the link was made.
      </p>
      <p>
        <Link href="/autonomy/blocked" className="text-blue-800 underline underline-offset-4">
          Back to blocked actions
        </Link>
      </p>
    </section>
  );
}
