import type { Metadata } from "next";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { AuditNav } from "@/components/audit/AuditNav";
import { EventTable } from "@/components/audit/EventTable";
import { NotPermitted } from "@/components/auth/NotPermitted";
import { FilterForm, SelectField, TextField } from "@/components/inventory/FilterForm";
import { Pagination } from "@/components/inventory/Pagination";
import { listEvents, requireSessionUser } from "@/lib/api";
import { AUDIT_EVENTS_PATH, EVENT_NAMES, describeEventName, parseEventNameParam } from "@/lib/audit";
import { can, permissionsOf } from "@/lib/auth";
import { DEFAULT_PAGE_SIZE, type SearchParams, firstParam, parseLimit, parseOffset } from "@/lib/inventory";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Audit: event log",
};

/** `subject_id` filter limit, as the contract declares it. */
const MAX_SUBJECT = 200;

export default async function AuditEventsPage({ searchParams }: { readonly searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const me = await requireSessionUser();
  const header = (
    <header className="space-y-2">
      <h1 className="text-2xl font-semibold">Audit log: events</h1>
      <p className="max-w-prose text-slate-800">
        The append-only event log, newest first. Payloads are shown as JSON text; credential-like keys are withheld.
        Read-only.
      </p>
      <AuditNav current="events" />
    </header>
  );
  if (!me.ok) {
    return (
      <div className="space-y-6">
        {header}
        <ApiErrorNotice error={me.error} what="Session" />
      </div>
    );
  }
  if (!can(permissionsOf(me.data), "read_audit")) {
    return (
      <div className="space-y-6">
        {header}
        <NotPermitted permission="read_audit" what="The audit log" />
      </div>
    );
  }

  const eventName = parseEventNameParam(params);
  const subject = firstParam(params, "subject_id")?.slice(0, MAX_SUBJECT);
  const limit = parseLimit(params);
  const offset = parseOffset(params);
  const result = await listEvents({ event_name: eventName ?? null, subject_id: subject ?? null, limit, offset });
  const filtered = eventName !== undefined || subject !== undefined;
  const state = { event_name: eventName, subject_id: subject, limit: limit === DEFAULT_PAGE_SIZE ? undefined : limit };

  return (
    <div className="space-y-6">
      {header}
      <FilterForm action={AUDIT_EVENTS_PATH} label="Filter events" {...(filtered ? { clearHref: AUDIT_EVENTS_PATH } : {})}>
        <SelectField
          id="events-name"
          name="event_name"
          label="Event"
          value={eventName}
          options={EVENT_NAMES.map((value) => ({ value, label: `${describeEventName(value)} (${value})` }))}
        />
        <TextField id="events-subject" name="subject_id" label="Subject id" value={subject} maxLength={MAX_SUBJECT} />
      </FilterForm>
      {!result.ok ? (
        <ApiErrorNotice error={result.error} what="Events" />
      ) : (
        <>
          <EventTable events={result.data.items} total={result.data.page.total} />
          <Pagination
            page={result.data.page}
            itemCount={result.data.items.length}
            pathname={AUDIT_EVENTS_PATH}
            params={state}
            label="Events pagination"
          />
        </>
      )}
    </div>
  );
}
