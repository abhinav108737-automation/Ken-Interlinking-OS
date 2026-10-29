import { SHADOW_BADGE, describeAutonomyModeShort, isShadowMode } from "@/lib/autonomy";

import { StatusBadge } from "../StatusBadge";

/**
 * The per-row mode marker. Every row of every autonomy list carries it, because
 * the mode that was in force when the row was written is a property of the ROW,
 * not of the system now: a shadow decision stays a counterfactual after the
 * operator moves to `autonomous`.
 *
 * A shadow row gets the unmistakable "did not happen" badge; any other mode
 * gets its plain name, so the column is never empty and never ambiguous.
 */
export function ModeCell({ mode }: { readonly mode: string }) {
  if (isShadowMode(mode)) {
    return <StatusBadge presentation={SHADOW_BADGE} />;
  }
  return <span className="whitespace-nowrap">{describeAutonomyModeShort(mode)}</span>;
}

/**
 * The sentence that accompanies a shadow record wherever one is shown on its
 * own (a detail panel, not a table row). Renders nothing outside shadow.
 */
export function ShadowNotice({
  mode,
  wouldHave,
}: {
  readonly mode: string;
  /** The labelled decision that would have been taken, when the API recorded one. */
  readonly wouldHave: string | null;
}) {
  if (!isShadowMode(mode)) return null;
  return (
    <p role="note" className="max-w-prose rounded border-2 border-amber-700 bg-amber-50 p-3 text-sm text-amber-950">
      <span className="font-semibold">
        <span aria-hidden="true">! </span>
        This did not happen.
      </span>{" "}
      It was recorded while the autonomy mode was shadow: nothing was published and nothing on the site changed.
      {wouldHave === null
        ? " The API recorded no counterfactual decision for it."
        : ` Had the mode been autonomous, the decision would have been “${wouldHave}”.`}
    </p>
  );
}
