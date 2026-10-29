import type { AutonomyHealth } from "@/lib/api-client";
import { describeAutonomyMode, describePublishing, isShadowMode } from "@/lib/autonomy";

import { StatusBadge } from "../StatusBadge";

export interface AutonomyModeBannerProps {
  /** `GET /autonomy/mode`, or `null` when the call failed or the session has none. */
  readonly health: AutonomyHealth | null;
  /** Compact form for a sub-screen that already carries the full banner above it. */
  readonly compact?: boolean;
}

/**
 * The autonomy mode, on every relevant screen (ADR-0018 §2). This is not
 * decoration: in `shadow` nothing is really published, so a reader who does not
 * see the mode can mistake a counterfactual for an action that happened. The
 * banner is therefore not dismissible, it states the mode in words (never
 * colour alone), and in `shadow` it says outright that nothing is published.
 *
 * It renders nothing when the mode is unknown rather than guessing: an absent
 * banner is better than a wrong one, and the operations screens show the API
 * error separately.
 */
export function AutonomyModeBanner({ health, compact = false }: AutonomyModeBannerProps) {
  if (health === null) return null;
  const mode = describeAutonomyMode(health.mode);
  const publishing = describePublishing(health);
  const shadow = isShadowMode(health.mode);
  const frame = shadow
    ? "border-amber-700 bg-amber-50 text-amber-950"
    : health.publishing_enabled
      ? "border-red-700 bg-red-50 text-red-950"
      : "border-slate-500 bg-slate-50 text-slate-900";
  return (
    <div
      role="note"
      aria-label="Autonomy mode"
      className={`rounded border-2 p-3 ${frame} ${compact ? "text-sm" : ""}`}
    >
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-semibold">Autonomy mode</span>
        <StatusBadge presentation={mode} />
        <StatusBadge presentation={publishing} />
      </p>
      {!compact && (
        <>
          <p className="mt-1 max-w-prose text-sm">{mode.description}</p>
          {shadow && (
            <p className="mt-1 max-w-prose text-sm font-semibold">
              Every decision recorded while this mode is in force is a counterfactual. Nothing on the site has
              changed because of it.
            </p>
          )}
          <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs">
            <div className="flex gap-1">
              <dt className="font-medium">CMS provider</dt>
              <dd className="font-mono">{health.cms_provider}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="font-medium">CMS writes</dt>
              <dd>{health.cms_writes_enabled ? "enabled" : "disabled"}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="font-medium">Autonomous acknowledged</dt>
              <dd>{health.autonomous_acknowledged ? "yes" : "no"}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="font-medium">Production writes allowed</dt>
              <dd>{health.allow_production ? "yes" : "no"}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="font-medium">Default verification</dt>
              <dd>{health.verification_profile_default}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="font-medium">Policy</dt>
              <dd>version {health.policy_version}</dd>
            </div>
            <div className="flex gap-1">
              <dt className="font-medium">Calibration</dt>
              <dd>{health.calibration_version ?? "none — confidence is raw"}</dd>
            </div>
          </dl>
        </>
      )}
    </div>
  );
}
