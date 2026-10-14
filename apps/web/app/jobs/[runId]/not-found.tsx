import Link from "next/link";

export default function JobRunNotFound() {
  return (
    <section aria-labelledby="job-not-found-heading" className="space-y-3">
      <h1 id="job-not-found-heading" className="text-2xl font-semibold">
        Job run not found
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no job run with this id. It may have been mistyped, or the database may have been reset
        since the link was made.
      </p>
      <p>
        <Link href="/jobs" className="text-blue-800 underline underline-offset-4">
          Back to job runs
        </Link>
      </p>
    </section>
  );
}
