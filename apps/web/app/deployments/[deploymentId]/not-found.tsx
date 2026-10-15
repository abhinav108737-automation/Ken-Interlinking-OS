import Link from "next/link";

export default function DeploymentNotFound() {
  return (
    <section aria-labelledby="deployment-not-found-heading" className="space-y-3">
      <h1 id="deployment-not-found-heading" className="text-2xl font-semibold">
        Deployment not found
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no deployment with this id. It may have been mistyped, or the database may have been reset since the link
        was made.
      </p>
      <p>
        <Link href="/deployments" className="text-blue-800 underline underline-offset-4">
          Back to deployments
        </Link>
      </p>
    </section>
  );
}
