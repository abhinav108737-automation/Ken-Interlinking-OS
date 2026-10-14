import Link from "next/link";

export default function AgentRunNotFound() {
  return (
    <section aria-labelledby="agent-run-not-found-heading" className="space-y-3">
      <h1 id="agent-run-not-found-heading" className="text-2xl font-semibold">
        Agent run not found
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no agent run with this id. It may have been mistyped, or the database may have been reset since the
        link was made.
      </p>
      <p>
        <Link href="/agents" className="text-blue-800 underline underline-offset-4">
          Back to agent runs
        </Link>
      </p>
    </section>
  );
}
