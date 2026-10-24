import type { DeploymentView } from "@/lib/api-client";

const DECISION_LABEL: Readonly<Record<string, string>> = {
  auto_publish: "auto-publish",
  auto_publish_with_extra_verification: "auto-publish with extra verification",
};

/** The audit tag of a deployment published by the autonomy engine (ADR-0018). Nothing for a human one. */
export function AutonomousBadge({ deployment }: { readonly deployment: DeploymentView }) {
  if (deployment.autonomous !== true) {
    return null;
  }
  const decision = deployment.autonomy_decision ?? null;
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-violet-700 bg-violet-50 px-2 py-0.5 text-sm font-medium text-violet-950"
      title={
        `Published by the autonomy engine${decision === null ? "" : `: ${DECISION_LABEL[decision] ?? decision}`}` +
        `${deployment.autonomy_decision_id == null ? "" : ` (decision #${deployment.autonomy_decision_id})`}. ` +
        "The requesting account is the operator identity the engine runs as."
      }
    >
      <span aria-hidden="true">⚙</span>
      <span>Autonomous</span>
    </span>
  );
}
