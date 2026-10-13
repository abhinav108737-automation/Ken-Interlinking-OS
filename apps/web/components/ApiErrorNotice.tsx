import type { ApiError } from "@/lib/api-client";
import { describeApiErrorKind } from "@/lib/status";

export interface ApiErrorNoticeProps {
  readonly error: ApiError;
  /** Heading level context: the notice renders an h3 beneath a section h2. */
  readonly what: string;
}

/** Accessible, non-crashing rendering of a failed API call. */
export function ApiErrorNotice({ error, what }: ApiErrorNoticeProps) {
  return (
    <div role="alert" className="rounded border border-red-700 bg-red-50 p-4 text-red-950">
      <h3 className="font-semibold">
        <span aria-hidden="true">✕ </span>
        {describeApiErrorKind(error.kind)}: {what} unavailable
      </h3>
      <p className="mt-1">{error.message}</p>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 text-sm">
        <dt className="font-medium">Endpoint</dt>
        <dd className="font-mono">{error.path}</dd>
        {error.status !== undefined && (
          <>
            <dt className="font-medium">HTTP status</dt>
            <dd className="font-mono">{error.status}</dd>
          </>
        )}
        {error.code !== undefined && (
          <>
            <dt className="font-medium">Error code</dt>
            <dd className="font-mono">{error.code}</dd>
          </>
        )}
      </dl>
    </div>
  );
}
