import Link from "next/link";
import type { ReactNode } from "react";

/**
 * A search/filter form submitted with GET: it only changes the query string,
 * so it is navigation, not a mutation. Works without JavaScript.
 */
export function FilterForm({
  action,
  label,
  hidden = {},
  clearHref,
  clearLabel = "Clear filters",
  submitLabel = "Apply",
  children,
}: {
  readonly action: string;
  readonly label: string;
  readonly clearLabel?: string;
  readonly submitLabel?: string;
  /** Query parameters to carry through the submit (e.g. the selected version). */
  readonly hidden?: Readonly<Record<string, string | number | undefined>>;
  readonly clearHref?: string;
  readonly children: ReactNode;
}) {
  return (
    <form
      method="get"
      action={action}
      role="search"
      aria-label={label}
      className="flex flex-wrap items-end gap-3 rounded border border-slate-300 bg-slate-50 p-3 text-sm"
    >
      {Object.entries(hidden).map(([name, value]) =>
        value === undefined ? null : <input key={name} type="hidden" name={name} value={String(value)} />,
      )}
      {children}
      <button
        type="submit"
        className="rounded border border-blue-800 bg-blue-800 px-3 py-1 font-medium text-white hover:bg-blue-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700"
      >
        {submitLabel}
      </button>
      {clearHref !== undefined && (
        <Link href={clearHref} className="py-1 text-blue-800 underline underline-offset-4">
          {clearLabel}
        </Link>
      )}
    </form>
  );
}

export function SelectField({
  id,
  name,
  label,
  value,
  options,
  anyLabel = "Any",
}: {
  readonly id: string;
  readonly name: string;
  readonly label: string;
  readonly value: string | undefined;
  readonly options: readonly { readonly value: string; readonly label: string }[];
  readonly anyLabel?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="font-medium text-slate-800">
        {label}
      </label>
      <select
        id={id}
        name={name}
        defaultValue={value ?? ""}
        className="rounded border border-slate-400 bg-white px-2 py-1"
      >
        <option value="">{anyLabel}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

export function TextField({
  id,
  name,
  label,
  value,
  maxLength,
  minLength,
  placeholder,
  describedBy,
}: {
  readonly id: string;
  readonly name: string;
  readonly label: string;
  readonly value: string | undefined;
  readonly maxLength?: number;
  readonly minLength?: number;
  readonly placeholder?: string;
  /** Id of an element that describes the field (e.g. a length hint). */
  readonly describedBy?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="font-medium text-slate-800">
        {label}
      </label>
      <input
        id={id}
        name={name}
        type="search"
        defaultValue={value ?? ""}
        maxLength={maxLength}
        minLength={minLength}
        placeholder={placeholder}
        aria-describedby={describedBy}
        className="w-64 rounded border border-slate-400 bg-white px-2 py-1"
      />
    </div>
  );
}
