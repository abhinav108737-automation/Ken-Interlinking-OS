import Link from "next/link";

export default function HomePage() {
  return (
    <section aria-labelledby="home-heading" className="space-y-4">
      <h1 id="home-heading" className="text-2xl font-semibold">
        Ken Interlink OS
      </h1>
      <p className="max-w-prose text-slate-800">
        This is the Phase 0 foundation of the Ken Research Agentic Interlinking OS. It provides the
        application shell and a live view of the control API and its dependencies; review and
        recommendation screens arrive in later phases. See{" "}
        <Link href="/system" className="text-blue-800 underline underline-offset-4">
          system status
        </Link>{" "}
        to check the API, database and model server.
      </p>
    </section>
  );
}
