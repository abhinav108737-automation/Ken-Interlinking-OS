import type { Metadata } from "next";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AutonomyModeBanner } from "@/components/autonomy/AutonomyModeBanner";
import { AutonomySummaryPanel } from "@/components/autonomy/AutonomySummaryPanel";
import { DecisionTable } from "@/components/autonomy/DecisionTable";
import { LifecycleStatePanel } from "@/components/autonomy/LifecycleStatePanel";
import {
  getAutonomyMode,
  getAutonomySummary,
  getRecommendationSummary,
  listAutonomyDecisions,
} from "@/lib/api";
import { ATTEMPTS_PATH, BLOCKED_PATH, DECISIONS_PATH } from "@/lib/autonomy";

// Live operational state: never prerender or cache this page.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Autonomy",
};

const RECENT_DECISIONS = 10;

export default async function AutonomyPage() {
  const [mode, summary, plans, decisions] = await Promise.all([
    getAutonomyMode(),
    getAutonomySummary(),
    getRecommendationSummary(),
    listAutonomyDecisions({ limit: RECENT_DECISIONS }),
  ]);

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold">Autonomy</h1>
        <AutonomyModeBanner health={mode.ok ? mode.data : null} />
        {!mode.ok && <ApiErrorNotice error={mode.error} what="Autonomy mode" />}
        <p className="max-w-prose text-slate-800">
          Link publishing is governed by policy and taken autonomously. There is no approval step in the normal
          workflow: this screen is for monitoring it, and for the exceptions —{" "}
          <Link href={BLOCKED_PATH} className="text-blue-800 underline underline-offset-4">
            blocked actions
          </Link>
          ,{" "}
          <Link href={ATTEMPTS_PATH} className="text-blue-800 underline underline-offset-4">
            publish attempts and rollbacks
          </Link>{" "}
          and the{" "}
          <Link href={DECISIONS_PATH} className="text-blue-800 underline underline-offset-4">
            full decision log
          </Link>
          .
        </p>
      </header>

      <section aria-labelledby="states-heading" className="space-y-3">
        <h2 id="states-heading" className="text-xl font-semibold">
          Operational state
        </h2>
        {plans.ok ? (
          <LifecycleStatePanel counts={plans.data.plans_by_status} />
        ) : (
          <ApiErrorNotice error={plans.error} what="Plan states" />
        )}
      </section>

      <section aria-labelledby="summary-heading" className="space-y-3">
        <h2 id="summary-heading" className="text-xl font-semibold">
          Decisions and blocks
        </h2>
        {summary.ok ? (
          <AutonomySummaryPanel summary={summary.data} />
        ) : (
          <ApiErrorNotice error={summary.error} what="Autonomy summary" />
        )}
      </section>

      <section aria-labelledby="recent-heading" className="space-y-3">
        <h2 id="recent-heading" className="text-xl font-semibold">
          Latest decisions
        </h2>
        {decisions.ok ? (
          <>
            <DecisionTable
              decisions={decisions.data.items}
              total={decisions.data.page.total}
              caption={`Latest ${RECENT_DECISIONS} autonomy decisions`}
            />
            <p className="text-sm">
              <Link href={DECISIONS_PATH} className="text-blue-800 underline underline-offset-4">
                All autonomy decisions
              </Link>
            </p>
          </>
        ) : (
          <ApiErrorNotice error={decisions.error} what="Autonomy decisions" />
        )}
      </section>
    </div>
  );
}
