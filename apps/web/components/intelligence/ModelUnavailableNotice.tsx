import Link from "next/link";

import type { ApiError } from "@/lib/api-client";

/**
 * Semantic search's declared 503 (`model_unavailable`): the API is up but the
 * embedding model cannot be reached. An expected operating state, explained in
 * plain text with what to check — not a crash and not a generic API failure.
 */
export function ModelUnavailableNotice({ error }: { readonly error: ApiError }) {
  return (
    <div role="alert" className="rounded border border-amber-700 bg-amber-50 p-4 text-amber-950">
      <h3 className="font-semibold">
        <span aria-hidden="true">! </span>
        Semantic search is unavailable: the embedding model cannot be reached
      </h3>
      <p className="mt-1">
        Searching needs the embedding model to embed your query. Nothing was searched and nothing was
        stored. Check that the local model server is running and the embedding model is pulled, then try
        again. Stored analysis, relationships and similar pages remain available.
      </p>
      <p className="mt-2 text-sm">
        See{" "}
        <Link href="/system" className="text-blue-800 underline underline-offset-4">
          system status
        </Link>{" "}
        for the model server check.
      </p>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 text-sm">
        <dt className="font-medium">Endpoint</dt>
        <dd className="font-mono">{error.path}</dd>
        <dt className="font-medium">Error code</dt>
        <dd className="font-mono">{error.code ?? "model_unavailable"}</dd>
        {error.requestId !== undefined && (
          <>
            <dt className="font-medium">Request id</dt>
            <dd className="font-mono">{error.requestId}</dd>
          </>
        )}
      </dl>
    </div>
  );
}
