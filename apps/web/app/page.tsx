import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { DashboardView } from "@/components/dashboard/DashboardView";
import { getAutonomyMode, getAutonomySummary, getDashboard, requireSessionUser } from "@/lib/api";
import { BLOCKED_PATH } from "@/lib/autonomy";
import { can, permissionsOf } from "@/lib/auth";

// Live counts: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Dashboard",
};

export default async function DashboardPage() {
  // Each call returns a result and never throws, so one failure cannot blank
  // the page: the dashboard renders with whatever came back.
  const [result, autonomy, mode, me] = await Promise.all([
    getDashboard(),
    getAutonomySummary(),
    getAutonomyMode(),
    requireSessionUser(),
  ]);
  const canReview = can(permissionsOf(me.ok ? me.data : null), "review");
  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="max-w-prose text-slate-800">
          Link publishing is governed by policy and taken autonomously: there is no approval step in the normal
          workflow. This console is for monitoring it and handling the exceptions —{" "}
          <Link href={BLOCKED_PATH} className="text-blue-800 underline underline-offset-4">
            blocked actions
          </Link>
          , failed verifications and rollbacks. See{" "}
          <Link href="/system" className="text-blue-800 underline underline-offset-4">
            system status
          </Link>{" "}
          for the API, database, vector store, model server and CMS.
        </p>
      </header>
      {!autonomy.ok && <ApiErrorNotice error={autonomy.error} what="Autonomy summary" />}
      {!mode.ok && <ApiErrorNotice error={mode.error} what="Autonomy mode" />}
      {result.ok ? (
        <DashboardView
          summary={result.data}
          autonomy={autonomy.ok ? autonomy.data : null}
          health={mode.ok ? mode.data : null}
          canReview={canReview}
        />
      ) : (
        <ApiErrorNotice error={result.error} what="Dashboard" />
      )}
    </div>
  );
}
