"""Content inventory contracts (Phase 1).

FROZEN CONTRACT. Implements IMPLEMENTATION_PLAN.md Phase 1 / PRD.md section 7.1:
crawl, parse, canonical page model, content hashing, existing-link extraction
and page/version persistence.

Four groups, in pipeline order::

    ContentSourceProvider --SourceDocument / SourceIssue-->  normalisation
        --NormalizedPage-->  persistence (pages, page_versions, existing_links)
        --InventoryReader-->  API views (PageSummary, PageDetail, ...)

1. **Sources.** Every content source -- the public web crawler, a JSONL file
   export, a future CMS read API -- yields the same two record types through
   one protocol. Persistence never knows which source produced a document.
2. **Normalisation output.** :class:`NormalizedPage` is the deterministic,
   LLM-free result of parsing one document (TRD.md section 11). Its
   ``content_hash`` decides whether a new immutable page version is written.
3. **File export format.** The documented JSONL interchange format used for
   fixtures, offline bootstrap and future CMS exports.
4. **Read side.** The API depends on :class:`InventoryReader`, never on SQL.

Identity conventions (see also ``ken_contracts.common``):

* ``InventoryPageId`` / ``IngestionRunId`` are UUIDs **we mint**. They are the
  ``page_id`` / ``run_id`` of every inventory API path.
* ``external_id`` is an OPAQUE identifier owned by the CMS or an export (the
  Phase 0 ``PageId``). It is round-tripped, never parsed or generated.
* ``canonical_url`` is the page's identity on the site: unique across pages.

Nothing here is ever sent to a CMS. Phase 1 is read-only with respect to every
content source: no endpoint, provider or service in this module mutates one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from ken_contracts.cms import CMSPage
from ken_contracts.common import AsyncClosable, FrozenModel
from ken_contracts.errors import ListResponse

type InventoryPageId = UUID
"""Internal page identity, minted by us. Stable across URL and content changes."""

type IngestionRunId = UUID
"""Identity of one crawl or import run."""

CONTENT_HASH_VERSION: Final = 1
"""Version of the content-hash algorithm. Stored with every page version, so
the algorithm can change later without making old hashes look like changes."""

SHA256_HEX_PATTERN: Final = r"^[0-9a-f]{64}$"

# Event identity, frozen so every emitter spells it the same way. Payloads are
# always built from the payload models below and stored as model_dump(mode="json").
SUBJECT_PAGE: Final = "page"
"""``subject_type`` of page.* events; ``subject_id`` is ``str(page_id)``."""
SUBJECT_INGESTION_RUN: Final = "ingestion_run"
"""``subject_type`` of crawl.completed; ``subject_id`` is ``str(run_id)``."""
EMITTED_BY_INGEST: Final = "ken-ingest"
"""``emitted_by`` of every inventory event. ``correlation_id`` is the run id."""
CMS_PROTECTED_KEY: Final = "cms_protected"
"""Assertion metadata key: ``"true"`` when the CMS itself protects the page.

Set by the CMS source (and by a CMS export); ingestion mirrors it as a
``source: cms`` protection row on every document (ADR-0010 section 5)."""
MAX_URL_LENGTH: Final = 2048
"""URLs longer than this are rejected by normalisation (and never stored)."""


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class SourceType(StrEnum):
    """Where a document came from. Recorded on every run and page version."""

    PUBLIC_WEB = "public_web"
    FILE_EXPORT = "file_export"
    CMS = "cms"


class PublicationState(StrEnum):
    """Publication state as the source reports it.

    The public web crawler can only observe ``published`` (a page served with
    HTTP 200). Drafts are visible only through an export or a CMS read API.
    ``unknown`` is the honest default when a source does not say.
    """

    PUBLISHED = "published"
    DRAFT = "draft"
    UNPUBLISHED = "unpublished"
    UNKNOWN = "unknown"


class CanonicalStatus(StrEnum):
    """How the page's identity URL was chosen.

    ``self``          declared canonical equals the page's own URL
    ``declared``      a different, in-scope canonical was declared and is used
    ``missing``       none declared; the normalised final URL is used
    ``out_of_scope``  declared canonical is off-site; ignored, final URL used
    ``invalid``       declared canonical is unparseable; ignored, final URL used

    Precedence at persistence (prevents identity flip-flop): a ``self`` or
    ``missing`` document is the AUTHORITY for its identity URL. A ``declared``
    (alias) document may create identity X only while X has no version from an
    authoritative document; once it has, the alias records ``known_urls`` Y -> X
    plus a ``duplicate_canonical`` issue and writes no version. Within one run,
    the authoritative document for X always wins over aliases of X.
    """

    SELF = "self"
    DECLARED = "declared"
    MISSING = "missing"
    OUT_OF_SCOPE = "out_of_scope"
    INVALID = "invalid"


class LinkType(StrEnum):
    """Classification of one ``<a href>`` in a page body."""

    INTERNAL = "internal"
    """http(s) URL whose host is inside the run's site scope."""
    EXTERNAL = "external"
    """http(s) URL on any other host. Recorded, never followed."""
    FRAGMENT = "fragment"
    """Same-page anchor (``#section``)."""
    NON_HTTP = "non_http"
    """``mailto:``, ``tel:``, ``javascript:``, ``data:`` and other schemes."""
    INVALID = "invalid"
    """Empty or unparseable ``href``."""


class LinkResolution(StrEnum):
    """Whether an internal link's destination is known (computed at read time).

    ``resolved``        the destination URL has led to a known, live page (the mapping
                        survives a later transient failure such as a timeout)
    ``broken``          the destination's latest answer was 404/410, or it leads to a gone page
    ``unresolved``      never led to a page: beyond crawl limits, robots-blocked or errored
    ``not_applicable``  the link is not internal
    """

    RESOLVED = "resolved"
    BROKEN = "broken"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


class RunStatus(StrEnum):
    """Lifecycle of one ingestion run.

    ``failed`` means the run itself could not complete (the source could not
    start, or an unexpected error aborted it). Per-URL problems never fail a
    run: they are recorded as issues and give ``completed_with_errors``.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueCode(StrEnum):
    """Stable machine-readable reason for one per-URL or per-record problem.

    Adding a member is additive and non-breaking.
    """

    # Scope and politeness
    ROBOTS_DISALLOWED = "robots_disallowed"
    ROBOTS_UNAVAILABLE = "robots_unavailable"
    OUT_OF_SCOPE = "out_of_scope"
    LIMIT_REACHED = "limit_reached"
    # Redirects
    REDIRECT_OUT_OF_SCOPE = "redirect_out_of_scope"
    REDIRECT_LOOP = "redirect_loop"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    # Fetching
    HTTP_NOT_FOUND = "http_not_found"
    HTTP_GONE = "http_gone"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    NON_HTML = "non_html"
    TOO_LARGE = "too_large"
    # Content
    INVALID_URL = "invalid_url"
    INVALID_RECORD = "invalid_record"
    PARSE_ERROR = "parse_error"
    DUPLICATE_URL = "duplicate_url"
    DUPLICATE_CANONICAL = "duplicate_canonical"
    IDENTITY_CONFLICT = "identity_conflict"
    # Persistence
    PERSIST_ERROR = "persist_error"


class UrlOutcome(StrEnum):
    """What happened the last time a URL was observed (``known_urls``)."""

    PAGE = "page"
    """Served a document that became (or matched) a page."""
    REDIRECT = "redirect"
    """Redirected to a URL that became a page."""
    NOT_FOUND = "not_found"
    GONE = "gone"
    HTTP_ERROR = "http_error"
    FETCH_ERROR = "fetch_error"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    NON_HTML = "non_html"


# --------------------------------------------------------------------------- #
# Site scope and crawl configuration
# --------------------------------------------------------------------------- #
def _normalise_netloc(value: str) -> str:
    """Lower-case ``host[:port]``; reject anything carrying a scheme, path or credentials."""
    text = value.strip().lower().rstrip(".")
    if not text or any(ch in text for ch in "/@?#\\ "):
        raise ValueError("an allowed host is a bare host or host:port, without scheme or path")
    return text


class SiteScope(FrozenModel):
    """The set of hosts that count as "this site".

    Decides whether a link is internal and whether the crawler may fetch a URL.
    ``allowed_hosts`` are network locations (``host`` or ``host:port``) compared
    after URL normalisation, so default ports never matter. ``http`` and
    ``https`` on an allowed host are both in scope. Subdomains are NOT implied:
    ``www.example.com`` must be listed explicitly.
    """

    allowed_hosts: frozenset[str] = Field(min_length=1)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _normalise_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(_normalise_netloc(str(item)) for item in value)
        return value


class CrawlConfig(FrozenModel):
    """Everything that shapes one public-web crawl.

    Stored verbatim as the run's configuration snapshot, so it must never hold
    a secret: the crawler never authenticates, so there is nothing to hold.
    There is deliberately no switch to ignore robots.txt.
    """

    base_url: str = Field(description="Start URL. Its host is always in scope.")
    allowed_hosts: tuple[str, ...] = Field(
        default=(), description="Additional in-scope hosts, e.g. the www. variant."
    )
    user_agent: str = Field(min_length=1, max_length=200)
    delay_s: float = Field(ge=0.0, le=60.0, description="Minimum gap between request starts.")
    concurrency: int = Field(ge=1, le=8)
    max_pages: int = Field(
        ge=1,
        le=100_000,
        description=(
            "Page requests, at most, whatever their outcome. A redirect chain counts as one "
            "request (so at most max_pages x (max_redirects + 1) HTTP requests); robots.txt "
            "is not counted."
        ),
    )
    max_depth: int = Field(ge=0, le=50, description="Link hops from the base URL.")
    timeout_s: float = Field(gt=0.0, le=120.0)
    max_bytes: int = Field(ge=1024, le=50_000_000, description="Per-response body cap.")
    max_redirects: int = Field(ge=0, le=10)
    max_query_variants_per_path: int = Field(
        default=10, ge=1, le=1000, description="Crawler-trap guard: distinct queries per path."
    )
    respect_meta_nofollow: bool = Field(
        default=True, description="Do not follow links from pages marked robots nofollow."
    )

    @field_validator("base_url")
    @classmethod
    def _base_url_is_plain_http(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("base_url must not carry credentials: the crawler never authenticates")
        return value.strip()

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _normalise_allowed(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(sorted({_normalise_netloc(str(item)) for item in value}))
        return value


# --------------------------------------------------------------------------- #
# Source records
# --------------------------------------------------------------------------- #
class SourceAssertions(FrozenModel):
    """Facts a source states explicitly, which take precedence over parsing.

    A JSONL export or a CMS knows the publication state and its own page id;
    HTML parsing can only guess. ``None`` means "the source does not say", in
    which case the value parsed from the HTML (if any) is used.
    """

    external_id: str | None = Field(default=None, max_length=512)
    page_type: str | None = Field(
        default=None,
        max_length=100,
        description="Opaque, source-asserted (e.g. a CMS template). Never inferred in Phase 1.",
    )
    title: str | None = None
    canonical_url: str | None = None
    publication_state: PublicationState | None = None
    language: str | None = Field(default=None, max_length=35)
    meta_description: str | None = None
    published_at: AwareDatetime | None = None
    modified_at: AwareDatetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class SourceDocument(FrozenModel):
    """One successfully obtained HTML document."""

    kind: Literal["document"] = "document"
    source_type: SourceType
    source_id: str = Field(
        min_length=1,
        description="The source's own id: final URL (web), record key (file), page id (CMS).",
    )
    requested_url: str = Field(description="The URL asked for (normalised).")
    final_url: str = Field(description="Where the content was found, after redirects (normalised).")
    redirect_chain: tuple[str, ...] = Field(
        default=(), description="URLs that redirected, in order, excluding final_url."
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = None
    content_language: str | None = Field(
        default=None,
        max_length=100,
        description="The Content-Language response header: the only header kept.",
    )
    fetched_at: AwareDatetime
    depth: int | None = Field(default=None, ge=0, description="Link hops from the seed (crawler).")
    html: str
    assertions: SourceAssertions = Field(default_factory=SourceAssertions)


class SourceIssue(FrozenModel):
    """One per-URL or per-record problem. Recorded; never aborts a run."""

    kind: Literal["issue"] = "issue"
    code: IssueCode
    severity: IssueSeverity
    url: str | None = Field(default=None, max_length=MAX_URL_LENGTH * 2)
    related_url: str | None = Field(
        default=None,
        max_length=MAX_URL_LENGTH * 2,
        description="e.g. the redirect target or the duplicate's canonical URL.",
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    message: str = Field(
        max_length=500,
        description="Human-readable and SAFE: never an exception's text, never a credential.",
    )
    occurred_at: AwareDatetime


type AnchorSource = Literal["text", "img_alt", "aria_label", "title", "none"]
"""Where a link's ``anchor_text`` came from, in order of preference."""

type SourceRecord = Annotated[SourceDocument | SourceIssue, Field(discriminator="kind")]


class SourceSummary(FrozenModel):
    """Totals only the source can know, reported after iteration."""

    discovered: int = Field(
        ge=0, description="Distinct in-scope URLs (web) or records (file, CMS) found."
    )
    skipped: int = Field(
        default=0,
        ge=0,
        description="Discovered but deliberately not fetched: robots, limits, non-HTML.",
    )


@runtime_checkable
class ContentSourceProvider(AsyncClosable, Protocol):
    """One typed interface over every content source.

    Contract for implementations:

    * ``iter_records`` is an async generator, consumed once. It yields documents
      and issues in a DETERMINISTIC order for the same source content.
    * A per-URL failure is yielded as a :class:`SourceIssue`, never raised.
      Raising means the source as a whole cannot continue (missing file, bad
      header); the run is then marked ``failed``.
    * ``site_scope`` is awaited once, before ``iter_records``.
    * ``summary`` is valid after iteration ends (or is abandoned).
    * ``config_snapshot`` contains no secret. It is persisted on the run.
    * Strictly read-only towards the source. No implementation may submit a
      form, authenticate, or send anything but GET (web) / read calls (CMS).
    """

    name: str
    source_type: SourceType

    def config_snapshot(self) -> dict[str, Any]:
        """JSON-serialisable, secret-free description of this source's configuration."""
        ...

    async def site_scope(self) -> SiteScope:
        """The hosts that count as this site: decides internal vs external links.

        Called before iteration. May perform I/O (a file source reads its
        header); raising here fails the run before any record is processed.
        """
        ...

    def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        """Yield every record. See the class docstring for the contract."""
        ...

    async def summary(self) -> SourceSummary:
        """Totals for the run record. Valid after iteration."""
        ...


# --------------------------------------------------------------------------- #
# CMS read-side adapter (no implementation talks to a real CMS in Phase 1)
# --------------------------------------------------------------------------- #
class CMSPageRef(FrozenModel):
    page_id: str = Field(description="Opaque CMS page id.")
    url: str
    version: str = Field(description="Opaque version token.")


class CMSPageRefPage(FrozenModel):
    items: tuple[CMSPageRef, ...]
    next_cursor: str | None = Field(default=None, description="Opaque; null on the last page.")


@runtime_checkable
class CMSInventoryReader(AsyncClosable, Protocol):
    """READ-ONLY access to a CMS's page list, for ``CMSContentSource``.

    Separate from :class:`~ken_contracts.cms.CMSProvider` on purpose: inventory
    needs only listing and reading, so the inventory code never holds an
    object that can mutate content. A real implementation needs the CMS read
    API details and least-privilege read credentials from the user (an ASK).
    """

    name: str

    async def list_pages(self, *, cursor: str | None = None, limit: int = 100) -> CMSPageRefPage:
        """One page of page references."""
        ...

    async def get_page(self, *, page_id: str) -> CMSPage:
        """One page. Raises ``CMSNotFoundError`` if it no longer exists."""
        ...


# --------------------------------------------------------------------------- #
# Normalisation output
# --------------------------------------------------------------------------- #
class Heading(FrozenModel):
    level: int = Field(ge=1, le=6)
    text: str
    block_id: str


class ExtractedLink(FrozenModel):
    """One ``<a href>`` inside the page's body content, in document order.

    The block fields line up with :class:`ken_contracts.cms.LinkLocator`
    (block selector, occurrence index, context hash), which is what a Phase 3+
    placement must later reference.
    """

    ordinal: int = Field(ge=0, description="Position among the page's body links.")
    href: str = Field(description="The raw attribute value, whitespace-trimmed.")
    resolved_url: str | None = Field(
        default=None, description="Normalised absolute URL; null when not http(s)."
    )
    link_type: LinkType
    anchor_text: str = Field(description="Visible text, whitespace-collapsed. May be empty.")
    anchor_source: AnchorSource
    rel: tuple[str, ...] = Field(default=(), description="Lower-cased rel tokens, sorted.")
    block_id: str = Field(description="Stable id of the containing block within this version.")
    block_kind: str = Field(description="Tag name of the containing block, e.g. 'p', 'li'.")
    occurrence_index: int = Field(ge=0, description="Index among links in the same block.")
    context_text: str = Field(description="The containing block's text, truncated.")
    context_hash: str = Field(
        pattern=SHA256_HEX_PATTERN, description="sha256 of the full block text."
    )


class NormalizedPage(FrozenModel):
    """The deterministic result of normalising one :class:`SourceDocument`.

    Same document in -> byte-identical model out. No LLM is involved (TRD.md
    section 11). ``content_hash`` covers exactly the fields that represent the
    page's CONTENT -- title, canonical, description, language, publication
    state, robots directives, headings, body text and every body link -- and
    nothing about the fetch (time, status, redirects, headers).
    """

    source_type: SourceType
    source_id: str
    external_id: str | None = None
    requested_url: str
    url: str = Field(description="Normalised final URL the content was served from.")
    redirect_chain: tuple[str, ...] = ()
    http_status: int | None = None
    fetched_at: AwareDatetime

    canonical_url: str = Field(
        description="Identity URL: the declared canonical if valid and in scope."
    )
    declared_canonical_url: str | None = None
    canonical_status: CanonicalStatus

    title: str | None = None
    meta_description: str | None = None
    language: str | None = None
    publication_state: PublicationState
    page_type: str | None = None
    published_at: AwareDatetime | None = Field(
        default=None, description="Source-asserted. Not part of content_hash."
    )
    modified_at: AwareDatetime | None = Field(
        default=None, description="Source-asserted. Not part of content_hash."
    )
    robots_directives: tuple[str, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)

    body_html: str = Field(description="The selected content region, boilerplate removed.")
    body_text: str = Field(description="Block texts joined by blank lines; NFC; collapsed spaces.")
    word_count: int = Field(ge=0)
    headings: tuple[Heading, ...] = ()
    links: tuple[ExtractedLink, ...] = ()

    content_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    hash_version: int = Field(default=CONTENT_HASH_VERSION, ge=1)


# --------------------------------------------------------------------------- #
# File export format (JSONL), version 1
# --------------------------------------------------------------------------- #
EXPORT_FORMAT: Final = "ken-inventory-export"
EXPORT_FORMAT_VERSION: Final = 1


class ExportHeader(FrozenModel):
    """Line 1 of an export file."""

    format: Literal["ken-inventory-export"]
    version: Literal[1]
    site_url: str = Field(description="Base URL of the exported site; its host defines scope.")
    allowed_hosts: tuple[str, ...] = Field(default=(), description="Additional in-scope hosts.")
    exported_at: AwareDatetime | None = None
    generator: str | None = Field(default=None, max_length=200)


class ExportPageRecord(FrozenModel):
    """Every following line: one page.

    ``url`` and ``body_html`` are required; everything else is an optional
    assertion that overrides what HTML parsing would infer.
    """

    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    body_html: str
    external_id: str | None = Field(default=None, max_length=512)
    page_type: str | None = Field(default=None, max_length=100)
    canonical_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    title: str | None = None
    meta_description: str | None = None
    publication_state: PublicationState | None = None
    language: str | None = Field(default=None, max_length=35)
    published_at: AwareDatetime | None = None
    modified_at: AwareDatetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Event payloads (emitted through the Phase 0 event log)
# --------------------------------------------------------------------------- #
class PageEventPayload(FrozenModel):
    """Payload of ``page.created`` (version 1) and ``page.updated`` (version n>1)."""

    page_id: UUID
    canonical_url: str
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    previous_version: int | None = Field(default=None, ge=1)
    previous_content_hash: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    source_type: SourceType
    run_id: UUID | None = None


class PageDeletedPayload(FrozenModel):
    """Payload of ``page.deleted``: the page's identity URL now answers 404/410.

    An OBSERVATION of the source site. Nothing is deleted anywhere: the page
    and its full version history remain in the inventory, marked gone.

    Emitted only on the TRANSITION (``pages.gone_at`` NULL -> set), never again
    while the URL keeps failing. If the page is later served again, ``gone_at``
    is cleared and ``page.updated`` is emitted only if its hash changed.
    Absence from a file export, a CMS listing or a partial crawl NEVER marks a
    page gone: only a 404/410 on its identity URL does.
    """

    page_id: UUID
    canonical_url: str
    http_status: Literal[404, 410]
    last_version: int = Field(ge=1)
    run_id: UUID | None = None


class RunCounters(FrozenModel):
    """Run accounting. Invariant (checked by tests)::

    pages_fetched == pages_created + pages_changed + pages_unchanged
                     + pages_duplicate + pages_failed
    """

    pages_discovered: int = Field(default=0, ge=0)
    pages_fetched: int = Field(default=0, ge=0)
    pages_created: int = Field(default=0, ge=0)
    pages_changed: int = Field(default=0, ge=0)
    pages_unchanged: int = Field(default=0, ge=0)
    pages_duplicate: int = Field(default=0, ge=0)
    pages_failed: int = Field(default=0, ge=0)
    pages_skipped: int = Field(default=0, ge=0)
    pages_gone: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)


class CrawlCompletedPayload(FrozenModel):
    """Payload of ``crawl.completed``, emitted once per run whatever its source.

    TRD.md section 15 has one run-level event. A file import or CMS read is the
    same act as a crawl -- taking stock of content -- so it emits this event
    too, with ``source_type`` saying which. A failed run also emits it, with
    ``status: failed``; there is no separate failure event to invent.
    """

    run_id: UUID
    source_type: SourceType
    source_name: str
    status: RunStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    counters: RunCounters


# --------------------------------------------------------------------------- #
# API read models
# --------------------------------------------------------------------------- #
class PageSummary(FrozenModel):
    """One row of the page inventory, describing the page's CURRENT version."""

    id: UUID
    canonical_url: str
    external_id: str | None = None
    title: str | None = None
    publication_state: PublicationState
    page_type: str | None = None
    language: str | None = None
    current_version: int = Field(ge=1)
    content_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    word_count: int = Field(ge=0)
    internal_link_count: int = Field(ge=0)
    external_link_count: int = Field(ge=0)
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    last_changed_at: AwareDatetime
    gone_at: AwareDatetime | None = Field(
        default=None, description="Set when the identity URL last answered 404/410."
    )


class PageVersionSummary(FrozenModel):
    page_id: UUID
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=SHA256_HEX_PATTERN)
    hash_version: int = Field(ge=1)
    run_id: UUID | None = None
    source_type: SourceType
    source_id: str
    url: str
    http_status: int | None = None
    title: str | None = None
    publication_state: PublicationState
    word_count: int = Field(ge=0)
    internal_link_count: int = Field(ge=0)
    external_link_count: int = Field(ge=0)
    fetched_at: AwareDatetime
    created_at: AwareDatetime


class PageVersionDetail(PageVersionSummary):
    requested_url: str
    redirect_chain: list[str]
    canonical_url: str
    declared_canonical_url: str | None = None
    canonical_status: CanonicalStatus
    meta_description: str | None = None
    language: str | None = None
    page_type: str | None = None
    published_at: AwareDatetime | None = None
    modified_at: AwareDatetime | None = None
    robots_directives: list[str]
    metadata: dict[str, str]
    headings: list[Heading]
    body_text: str
    body_html: str = Field(
        description="Stored content region. Clients must NEVER render it as HTML."
    )


class PageDetail(PageSummary):
    current: PageVersionDetail


class ExistingLinkView(FrozenModel):
    """One body link as stored for a page version, with read-time resolution."""

    page_id: UUID
    page_version: int = Field(ge=1)
    source_url: str = Field(description="Canonical URL of the page the link is on.")
    ordinal: int = Field(ge=0)
    href: str
    resolved_url: str | None = None
    link_type: LinkType
    resolution: LinkResolution
    target_page_id: UUID | None = None
    anchor_text: str
    anchor_source: AnchorSource
    rel: list[str]
    block_id: str
    block_kind: str
    occurrence_index: int = Field(ge=0)
    context_text: str


class IngestionRunSummary(FrozenModel):
    id: UUID
    source_type: SourceType
    source_name: str
    status: RunStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    counters: RunCounters


class IngestionRunDetail(IngestionRunSummary):
    config_snapshot: dict[str, Any] = Field(description="Secret-free run configuration.")
    failure_reason: str | None = Field(
        default=None, description="Safe summary when status is failed. Never exception text."
    )
    issue_counts: dict[str, int] = Field(
        default_factory=dict, description="Issues stored for this run, by IssueCode."
    )


class IngestionIssueView(FrozenModel):
    id: int
    run_id: UUID
    code: IssueCode
    severity: IssueSeverity
    url: str | None = None
    related_url: str | None = None
    http_status: int | None = None
    message: str
    occurred_at: AwareDatetime


# --------------------------------------------------------------------------- #
# Read-side boundary between the API and persistence
# --------------------------------------------------------------------------- #
class InventoryNotFoundError(Exception):
    """The requested inventory resource does not exist.

    ``resource`` lets the API choose a stable error code without parsing text.
    The message never includes the requested identifier.
    """

    def __init__(self, resource: Literal["page", "page_version", "run"]) -> None:
        super().__init__(f"{resource} not found")
        self.resource = resource


@runtime_checkable
class InventoryReader(Protocol):
    """Everything the inventory API reads. Async, keyword-only, read-only.

    Collections are ``ListResponse`` with offset/limit paging; ``limit`` is
    1..200 (``PageInfo``). Single-item reads raise
    :class:`InventoryNotFoundError`.
    """

    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None = None,
        publication_state: PublicationState | None = None,
    ) -> ListResponse[PageSummary]: ...

    async def get_page(self, *, page_id: UUID) -> PageDetail: ...

    async def list_page_versions(
        self, *, page_id: UUID, limit: int, offset: int
    ) -> ListResponse[PageVersionSummary]: ...

    async def get_page_version(self, *, page_id: UUID, version: int) -> PageVersionDetail: ...

    async def list_page_links(
        self,
        *,
        page_id: UUID,
        limit: int,
        offset: int,
        version: int | None = None,
        link_type: LinkType | None = None,
    ) -> ListResponse[ExistingLinkView]:
        """Body links of one version (default: the current one)."""
        ...

    async def list_links(
        self,
        *,
        limit: int,
        offset: int,
        link_type: LinkType | None = None,
        resolution: LinkResolution | None = None,
        target_page_id: UUID | None = None,
    ) -> ListResponse[ExistingLinkView]:
        """Body links across every page's CURRENT version.

        ``target_page_id`` gives a page's inbound links.
        """
        ...

    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        source_type: SourceType | None = None,
        status: RunStatus | None = None,
    ) -> ListResponse[IngestionRunSummary]: ...

    async def get_run(self, *, run_id: UUID) -> IngestionRunDetail: ...

    async def list_run_issues(
        self,
        *,
        run_id: UUID,
        limit: int,
        offset: int,
        severity: IssueSeverity | None = None,
    ) -> ListResponse[IngestionIssueView]: ...
