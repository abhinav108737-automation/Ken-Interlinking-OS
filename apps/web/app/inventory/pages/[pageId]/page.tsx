import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { StatusBadge } from "@/components/StatusBadge";
import { PageIntelligencePanel } from "@/components/intelligence/PageIntelligencePanel";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { FilterForm, SelectField } from "@/components/inventory/FilterForm";
import { KeyValueTable } from "@/components/inventory/KeyValueTable";
import { LinkTable } from "@/components/inventory/LinkTable";
import { BodyText, HeadingsOutline } from "@/components/inventory/PageContent";
import { pageHref } from "@/components/inventory/PageInventoryTable";
import { PageVersionTable } from "@/components/inventory/PageVersionTable";
import { Pagination } from "@/components/inventory/Pagination";
import { runHref } from "@/components/inventory/RunTable";
import { RecommendationTable } from "@/components/recommendations/RecommendationTable";
import {
  type ApiError,
  getInventoryPage,
  getInventoryPageVersion,
  getPageIntelligence,
  getTaxonomy,
  isNotFoundError,
  listInventoryLinks,
  listInventoryPageLinks,
  listInventoryPageVersions,
  listLinkPlans,
  listPageRelationships,
  listSimilarPages,
} from "@/lib/api";
import { formatCount, formatDateTime, shortHash, toDisplayEntries } from "@/lib/format";
import { SIMILAR_DEFAULT_LIMIT, taxonomyLabels } from "@/lib/intelligence";
import {
  DEFAULT_PAGE_SIZE,
  GONE_PRESENTATION,
  LINK_TYPES,
  type SearchParams,
  buildHref,
  describeCanonicalStatus,
  describeLinkType,
  describePublicationState,
  describeSourceType,
  pageDisplayTitle,
  parseEnumParam,
  parseOffset,
  parsePositiveInt,
} from "@/lib/inventory";
import { ALL_STATUSES, PAGE_PLANS_LIMIT, RECOMMENDATIONS_PATH } from "@/lib/recommendations";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Page detail",
};

/** A missing page, or an id the API (or client) rejects as malformed: both mean "no such page". */
function isMissing(error: ApiError): boolean {
  return (
    isNotFoundError(error) ||
    error.kind === "invalid_argument" ||
    (error.kind === "api_error" && error.status === 422)
  );
}

function Fact({ term, children }: { readonly term: string; readonly children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="min-w-0 text-slate-900">{children}</dd>
    </>
  );
}

function Optional({ value }: { readonly value: string | null }) {
  return value === null || value.trim() === "" ? <span className="text-slate-600">—</span> : <>{value}</>;
}

function When({ iso }: { readonly iso: string | null }) {
  return iso === null ? (
    <span className="text-slate-600">—</span>
  ) : (
    <time dateTime={iso}>{formatDateTime(iso)}</time>
  );
}

const FACTS = "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm";

export default async function PageDetailPage({
  params,
  searchParams,
}: {
  readonly params: Promise<{ pageId: string }>;
  readonly searchParams: Promise<SearchParams>;
}) {
  const [{ pageId }, query] = await Promise.all([params, searchParams]);
  const requestedVersion = parsePositiveInt(query, "version");
  const linkType = parseEnumParam(query, "link_type", LINK_TYPES);
  const linksOffset = parseOffset(query, "links_offset");
  const inboundOffset = parseOffset(query, "inbound_offset");
  const versionsOffset = parseOffset(query, "versions_offset");
  const relatedOffset = parseOffset(query, "related_offset");

  const [
    pageResult,
    selectedResult,
    versionsResult,
    inboundResult,
    intelligenceResult,
    relationshipsResult,
    similarResult,
    taxonomyResult,
    plansFromResult,
    plansToResult,
  ] = await Promise.all([
    getInventoryPage(pageId),
    requestedVersion === undefined ? Promise.resolve(null) : getInventoryPageVersion(pageId, requestedVersion),
    listInventoryPageVersions(pageId, { limit: DEFAULT_PAGE_SIZE, offset: versionsOffset }),
    listInventoryLinks({ target_page_id: pageId, limit: DEFAULT_PAGE_SIZE, offset: inboundOffset }),
    getPageIntelligence(pageId),
    listPageRelationships(pageId, { limit: DEFAULT_PAGE_SIZE, offset: relatedOffset }),
    listSimilarPages(pageId, { limit: SIMILAR_DEFAULT_LIMIT }),
    getTaxonomy(),
    listLinkPlans({ source_page_id: pageId, limit: PAGE_PLANS_LIMIT }),
    listLinkPlans({ target_page_id: pageId, limit: PAGE_PLANS_LIMIT }),
  ]);

  if (!pageResult.ok) {
    if (isMissing(pageResult.error)) {
      notFound();
    }
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold">Page detail</h1>
        <ApiErrorNotice error={pageResult.error} what="Page" />
        <p>
          <Link href="/inventory" className="text-blue-800 underline underline-offset-4">
            Back to the inventory
          </Link>
        </p>
      </div>
    );
  }

  const page = pageResult.data;
  const shown = selectedResult?.ok ? selectedResult.data : page.current;
  const isCurrent = shown.version === page.current_version;
  // Links of the version actually shown; `null` means the current version.
  const linksVersion = isCurrent ? null : shown.version;
  const linksResult = await listInventoryPageLinks(pageId, {
    version: linksVersion,
    link_type: linkType ?? null,
    limit: DEFAULT_PAGE_SIZE,
    offset: linksOffset,
  });

  const self = pageHref(page.id);
  /** Query state to preserve across this page's links and forms. */
  const state = {
    version: isCurrent ? undefined : shown.version,
    link_type: linkType,
    links_offset: linksOffset || undefined,
    inbound_offset: inboundOffset || undefined,
    versions_offset: versionsOffset || undefined,
    related_offset: relatedOffset || undefined,
  };
  const metadataRows = toDisplayEntries(shown.metadata);

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm">
          <Link href="/inventory" className="text-blue-800 underline underline-offset-4">
            ← Content inventory
          </Link>
        </p>
        <h1 className="break-words text-2xl font-semibold">{pageDisplayTitle(page)}</h1>
        <p className="flex flex-wrap items-center gap-2">
          <StatusBadge presentation={describePublicationState(page.publication_state)} />
          {page.gone_at !== null && <StatusBadge presentation={GONE_PRESENTATION} />}
          <span className="text-sm text-slate-700">
            Version {page.current_version} · {formatCount(page.word_count)} words ·{" "}
            {formatCount(page.internal_link_count)} internal / {formatCount(page.external_link_count)} external
            links
          </span>
        </p>
      </header>

      <section aria-labelledby="identity-heading" className="space-y-3">
        <h2 id="identity-heading" className="text-xl font-semibold">
          Identity
        </h2>
        <dl className={FACTS}>
          <Fact term="Canonical URL">
            <ExternalUrl url={page.canonical_url} />
          </Fact>
          <Fact term="Declared canonical">
            {shown.declared_canonical_url === null ? (
              <span className="text-slate-600">None declared</span>
            ) : (
              <ExternalUrl url={shown.declared_canonical_url} />
            )}
          </Fact>
          <Fact term="Canonical status">{describeCanonicalStatus(shown.canonical_status)}</Fact>
          <Fact term="External id">
            <Optional value={page.external_id} />
          </Fact>
          <Fact term="Page type">
            <Optional value={page.page_type} />
          </Fact>
          <Fact term="Language">
            <Optional value={page.language} />
          </Fact>
          <Fact term="Publication state">{describePublicationState(page.publication_state).label}</Fact>
          <Fact term="Published">
            <When iso={shown.published_at} />
          </Fact>
          <Fact term="Modified">
            <When iso={shown.modified_at} />
          </Fact>
          <Fact term="Gone since">
            {page.gone_at === null ? <span className="text-slate-600">Not gone</span> : <When iso={page.gone_at} />}
          </Fact>
          <Fact term="First seen">
            <When iso={page.first_seen_at} />
          </Fact>
          <Fact term="Last seen">
            <When iso={page.last_seen_at} />
          </Fact>
          <Fact term="Last changed">
            <When iso={page.last_changed_at} />
          </Fact>
          <Fact term="Page id">
            <code className="text-xs">{page.id}</code>
          </Fact>
        </dl>
      </section>

      <PageIntelligencePanel
        intelligence={intelligenceResult}
        relationships={relationshipsResult}
        similar={similarResult}
        labels={taxonomyLabels(taxonomyResult.ok ? taxonomyResult.data : null)}
        pagination={{ pathname: self, params: state, offsetParam: "related_offset" }}
      />

      <section aria-labelledby="recommendations-heading" className="space-y-4">
        <h2 id="recommendations-heading" className="text-xl font-semibold">
          Recommendations
        </h2>
        <p className="text-sm text-slate-700">
          Proposed links involving this page, in any status, highest score first. Read-only.
        </p>
        {(
          [
            ["from", "Recommendations from this page", plansFromResult, "source_page_id"],
            ["to", "Recommendations to this page", plansToResult, "target_page_id"],
          ] as const
        ).map(([variant, title, plansResult, filter]) => (
          <div key={variant} className="space-y-2">
            <h3 id={`recommendations-${variant}-heading`} className="text-lg font-semibold">
              {title}
            </h3>
            {plansResult.ok ? (
              <>
                <RecommendationTable
                  plans={plansResult.data.items}
                  total={plansResult.data.page.total}
                  caption={title}
                  variant={variant}
                  emptyMessage={
                    variant === "from"
                      ? "No recommendations place a link on this page."
                      : "No recommendations link to this page."
                  }
                />
                {plansResult.data.page.total > plansResult.data.items.length && (
                  <p className="text-sm">
                    <Link
                      href={buildHref(RECOMMENDATIONS_PATH, { status: ALL_STATUSES, [filter]: page.id })}
                      className="text-blue-800 underline underline-offset-4"
                    >
                      View all {formatCount(plansResult.data.page.total)}{" "}
                      <span className="sr-only">{title.toLowerCase()}</span>
                    </Link>
                  </p>
                )}
              </>
            ) : (
              <ApiErrorNotice error={plansResult.error} what={title} />
            )}
          </div>
        ))}
      </section>

      <section aria-labelledby="version-heading" className="space-y-3">
        <h2 id="version-heading" className="text-xl font-semibold">
          {isCurrent ? `Current version (v${shown.version})` : `Version ${shown.version} (not current)`}
        </h2>
        {selectedResult !== null && !selectedResult.ok && (
          <ApiErrorNotice
            error={selectedResult.error}
            what={`Version ${requestedVersion ?? ""}`.trim()}
          />
        )}
        {!isCurrent && (
          <p className="text-sm">
            You are inspecting an older version.{" "}
            <Link href={self} className="text-blue-800 underline underline-offset-4">
              Show the current version (v{page.current_version})
            </Link>
          </p>
        )}
        <dl className={FACTS}>
          <Fact term="Version">{shown.version}</Fact>
          <Fact term="Content hash">
            <code className="text-xs" title={shown.content_hash}>
              {shortHash(shown.content_hash)}
            </code>
          </Fact>
          <Fact term="Fetched">
            <When iso={shown.fetched_at} />
          </Fact>
          <Fact term="Source">
            {describeSourceType(shown.source_type)} · <code className="text-xs">{shown.source_id}</code>
          </Fact>
          <Fact term="Run">
            {shown.run_id === null ? (
              <span className="text-slate-600">—</span>
            ) : (
              <Link href={runHref(shown.run_id)} className="font-mono text-xs text-blue-800 underline">
                {shown.run_id}
              </Link>
            )}
          </Fact>
          <Fact term="HTTP status">{shown.http_status ?? "—"}</Fact>
          <Fact term="URL">
            <ExternalUrl url={shown.url} />
          </Fact>
          <Fact term="Requested URL">
            <ExternalUrl url={shown.requested_url} />
          </Fact>
          <Fact term="Redirect chain">
            {shown.redirect_chain.length === 0 ? (
              <span className="text-slate-600">No redirects</span>
            ) : (
              <ol className="list-decimal pl-5">
                {shown.redirect_chain.map((hop, index) => (
                  <li key={`${index}:${hop}`}>
                    <ExternalUrl url={hop} textOnly />
                  </li>
                ))}
              </ol>
            )}
          </Fact>
          <Fact term="Title">
            <Optional value={shown.title} />
          </Fact>
          <Fact term="Meta description">
            <Optional value={shown.meta_description} />
          </Fact>
          <Fact term="Robots directives">
            {shown.robots_directives.length === 0 ? (
              <span className="text-slate-600">None</span>
            ) : (
              <code className="text-xs">{shown.robots_directives.join(", ")}</code>
            )}
          </Fact>
        </dl>
      </section>

      <section aria-labelledby="headings-heading" className="space-y-3">
        <h2 id="headings-heading" className="text-xl font-semibold">
          Headings
        </h2>
        <HeadingsOutline headings={shown.headings} />
      </section>

      <section aria-labelledby="metadata-heading" className="space-y-3">
        <h2 id="metadata-heading" className="text-xl font-semibold">
          Metadata
        </h2>
        <KeyValueTable caption="Page metadata" rows={metadataRows} emptyMessage="No metadata was recorded." />
      </section>

      <section aria-labelledby="body-heading" className="space-y-3">
        <h2 id="body-heading" className="text-xl font-semibold">
          Body text
        </h2>
        <p className="text-sm text-slate-700">
          Extracted plain text of version {shown.version}. Stored HTML is never rendered.
        </p>
        <BodyText text={shown.body_text} label={`Body text of version ${shown.version}`} />
      </section>

      <section aria-labelledby="versions-heading" className="space-y-3">
        <h2 id="versions-heading" className="text-xl font-semibold">
          Version history
        </h2>
        {versionsResult.ok ? (
          <>
            <PageVersionTable
              pageId={page.id}
              versions={versionsResult.data.items}
              total={versionsResult.data.page.total}
              shownVersion={shown.version}
            />
            <Pagination
              page={versionsResult.data.page}
              itemCount={versionsResult.data.items.length}
              pathname={self}
              params={state}
              offsetParam="versions_offset"
              label="Version history pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={versionsResult.error} what="Version history" />
        )}
      </section>

      <section aria-labelledby="links-heading" className="space-y-3">
        <h2 id="links-heading" className="text-xl font-semibold">
          Links in version {shown.version}
        </h2>
        <FilterForm
          action={self}
          label="Filter links"
          hidden={{ version: state.version }}
          {...(linkType !== undefined ? { clearHref: buildHref(self, { version: state.version }) } : {})}
        >
          <SelectField
            id="links-link-type"
            name="link_type"
            label="Link type"
            value={linkType}
            options={LINK_TYPES.map((value) => ({ value, label: describeLinkType(value) }))}
          />
        </FilterForm>
        {linksResult.ok ? (
          <>
            <LinkTable
              links={linksResult.data.items}
              total={linksResult.data.page.total}
              caption="Body links"
              emptyMessage={linkType === undefined ? "This version has no body links." : "No links of this type."}
            />
            <Pagination
              page={linksResult.data.page}
              itemCount={linksResult.data.items.length}
              pathname={self}
              params={state}
              offsetParam="links_offset"
              label="Body links pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={linksResult.error} what="Body links" />
        )}
      </section>

      <section aria-labelledby="inbound-heading" className="space-y-3">
        <h2 id="inbound-heading" className="text-xl font-semibold">
          Inbound links
        </h2>
        <p className="text-sm text-slate-700">
          Links in other pages&apos; current versions that resolve to this page.
        </p>
        {inboundResult.ok ? (
          <>
            <LinkTable
              links={inboundResult.data.items}
              total={inboundResult.data.page.total}
              caption="Inbound links"
              showSource
              emptyMessage="No known page links to this page."
            />
            <Pagination
              page={inboundResult.data.page}
              itemCount={inboundResult.data.items.length}
              pathname={self}
              params={state}
              offsetParam="inbound_offset"
              label="Inbound links pagination"
            />
          </>
        ) : (
          <ApiErrorNotice error={inboundResult.error} what="Inbound links" />
        )}
      </section>
    </div>
  );
}
