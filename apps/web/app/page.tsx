import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { DashboardView } from "@/components/dashboard/DashboardView";
import { getDashboard, requireSessionUser } from "@/lib/api";
import { can, permissionsOf } from "@/lib/auth";

// Live counts: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Dashboard",
};

export default async function DashboardPage() {
  const [result, me] = await Promise.all([getDashboard(), requireSessionUser()]);
  const canReview = can(permissionsOf(me.ok ? me.data : null), "review");
  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="max-w-prose text-slate-800">
          The state of the content inventory, the recommendation queue and recent reviews. Approved recommendations are
          deployed by an operator admin, one source page at a time, and listed under{" "}
          <Link href="/deployments" className="text-blue-800 underline underline-offset-4">
            deployments
          </Link>
          ; nothing deploys automatically. See{" "}
          <Link href="/system" className="text-blue-800 underline underline-offset-4">
            system status
          </Link>{" "}
          for the API, database and model server.
        </p>
      </header>
      {result.ok ? (
        <DashboardView summary={result.data} canReview={canReview} />
      ) : (
        <ApiErrorNotice error={result.error} what="Dashboard" />
      )}
    </div>
  );
}
