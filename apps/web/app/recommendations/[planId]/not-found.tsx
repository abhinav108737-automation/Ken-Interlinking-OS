import Link from "next/link";

export default function RecommendationNotFound() {
  return (
    <section aria-labelledby="plan-not-found-heading" className="space-y-3">
      <h1 id="plan-not-found-heading" className="text-2xl font-semibold">
        Recommendation not found
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no recommendation with this id. It may have been mistyped, or the database may have been reset since
        the link was made.
      </p>
      <p>
        <Link href="/recommendations" className="text-blue-800 underline underline-offset-4">
          Back to recommendations
        </Link>
      </p>
    </section>
  );
}
