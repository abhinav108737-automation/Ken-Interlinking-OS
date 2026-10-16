import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import type { AnalyticsSource } from "@/lib/api-client";
import { ANALYTICS_PATH, LINK_SOURCES, MAX_RANGE_DAYS, type RangeSelection, describeSource } from "@/lib/analytics";

export interface RangeFormProps {
  readonly range: RangeSelection;
  readonly source: AnalyticsSource | undefined;
  /** The sources offered. Defaults to the link-level sources. */
  readonly sources?: readonly AnalyticsSource[];
  /** Shown when a filter is active. */
  readonly clearHref?: string;
}

function DateField({
  id,
  name,
  label,
  value,
}: {
  readonly id: string;
  readonly name: string;
  readonly label: string;
  readonly value: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="font-medium text-slate-800">
        {label}
      </label>
      <input
        id={id}
        name={name}
        type="text"
        inputMode="numeric"
        autoComplete="off"
        defaultValue={value}
        maxLength={10}
        placeholder="YYYY-MM-DD"
        pattern="\d{4}-\d{2}-\d{2}"
        aria-describedby="analytics-range-hint"
        className="w-36 rounded border border-slate-400 bg-white px-2 py-1"
      />
    </div>
  );
}

/**
 * The source and date-range selector: a GET form, so it only changes the query
 * string and works without JavaScript. The dates are plain text so an invalid
 * value stays visible and can be corrected.
 */
export function RangeForm({ range, source, sources = LINK_SOURCES, clearHref }: RangeFormProps) {
  return (
    <div className="space-y-2">
      <FilterForm
        action={ANALYTICS_PATH}
        label="Filter analytics"
        {...(clearHref === undefined ? {} : { clearHref })}
      >
        <SelectField
          id="analytics-source"
          name="source"
          label="Source"
          value={source}
          anyLabel="All sources"
          options={sources.map((value) => ({ value, label: describeSource(value) }))}
        />
        <DateField id="analytics-start" name="start" label="Start date" value={range.startText} />
        <DateField id="analytics-end" name="end" label="End date" value={range.endText} />
      </FilterForm>
      <p id="analytics-range-hint" className="text-xs text-slate-700">
        Give both dates or neither (then the API uses its own latest range). Days are counted in each source&apos;s own
        time zone, and a range may cover at most {MAX_RANGE_DAYS} days.
      </p>
    </div>
  );
}
