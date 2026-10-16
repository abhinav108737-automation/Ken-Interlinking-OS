import Link from "next/link";

export default function ProposalNotFound() {
  return (
    <section aria-labelledby="proposal-not-found-heading" className="space-y-3">
      <h1 id="proposal-not-found-heading" className="text-2xl font-semibold">
        Proposal not found
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no learning proposal with this id. It may have been mistyped, or the database may have been reset.
      </p>
      <p>
        <Link href="/learning/proposals" className="text-blue-800 underline underline-offset-4">
          Back to proposals
        </Link>
      </p>
    </section>
  );
}
