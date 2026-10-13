import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PrimaryNav } from "@/components/PrimaryNav";
import { ExternalUrl } from "@/components/inventory/ExternalUrl";
import { IssueTable } from "@/components/inventory/IssueTable";
import { KeyValueTable } from "@/components/inventory/KeyValueTable";
import { LinkTable } from "@/components/inventory/LinkTable";
import { BodyText, HeadingsOutline } from "@/components/inventory/PageContent";
import { PageInventoryTable } from "@/components/inventory/PageInventoryTable";
import { Pagination } from "@/components/inventory/Pagination";
import { RunTable } from "@/components/inventory/RunTable";

import { OTHER_PAGE_ID, PAGE_ID, RUN_ID, issue, link, pageInfo, pageSummary, runSummary } from "./fixtures/inventory";

const pathname = vi.hoisted(() => ({ current: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => pathname.current }));

describe("ExternalUrl", () => {
  it("links an http(s) URL in a new tab with safe rel", () => {
    render(<ExternalUrl url="https://example.com/a" />);
    const anchor = screen.getByRole("link", { name: /https:\/\/example\.com\/a/ });
    expect(anchor).toHaveAttribute("href", "https://example.com/a");
    expect(anchor).toHaveAttribute("target", "_blank");
    expect(anchor).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(anchor).toHaveTextContent(/opens in a new tab/);
  });

  it.each(["javascript:alert(1)", "data:text/html,x", "/relative"])("renders %s as text only", (url) => {
    const { container } = render(<ExternalUrl url={url} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText(url)).toBeInTheDocument();
  });

  it("can force text rendering", () => {
    render(<ExternalUrl url="https://example.com" textOnly />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("PageInventoryTable", () => {
  it("is a captioned table with column headers and row headers", () => {
    render(<PageInventoryTable pages={[pageSummary()]} total={7} />);
    const table = screen.getByRole("table", { name: /pages/i });
    expect(within(table).getByText("(7)")).toBeInTheDocument();
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual([
      "Title",
      "Canonical URL",
      "Publication",
      "Language",
      "Version",
      "Words",
      "Internal links",
      "External links",
      "Last changed",
    ]);
    headers.forEach((h) => expect(h).toHaveAttribute("scope", "col"));
    expect(within(table).getAllByRole("rowheader")).toHaveLength(1);
  });

  it("links the title to the page detail and shows facts as text", () => {
    render(<PageInventoryTable pages={[pageSummary()]} total={1} />);
    const row = screen.getByRole("row", { name: /widgets market report/i });
    expect(within(row).getByRole("link", { name: "Widgets market report" })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    expect(within(row).getByText("Published")).toBeInTheDocument();
    expect(within(row).getByText("1,534")).toBeInTheDocument();
    expect(within(row).getByText("2026-10-12 09:30:00 UTC")).toBeInTheDocument();
    expect(within(row).queryByText("Gone")).toBeNull();
    // The canonical URL is text, not an outbound link.
    expect(within(row).getAllByRole("link")).toHaveLength(1);
  });

  it("falls back to the canonical URL as the title and shows a Gone badge", () => {
    render(
      <PageInventoryTable
        pages={[pageSummary({ title: null, gone_at: "2026-10-13T00:00:00Z", publication_state: "weird" as never })]}
        total={1}
      />,
    );
    const rowHeader = screen.getByRole("rowheader");
    expect(within(rowHeader).getByRole("link")).toHaveTextContent("https://www.example.com/reports/widgets");
    expect(within(rowHeader).getByText("Gone")).toBeInTheDocument();
    expect(screen.getByText("Unknown (weird)")).toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("renders prev/next links preserving filters", () => {
    render(
      <Pagination
        page={pageInfo({ total: 120, offset: 50, has_more: true })}
        itemCount={50}
        pathname="/inventory"
        params={{ q: "a b", publication_state: "draft" }}
        label="Pages pagination"
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Pages pagination" });
    expect(within(nav).getByText("Showing 51–100 of 120")).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /previous/i })).toHaveAttribute(
      "href",
      "/inventory?q=a+b&publication_state=draft",
    );
    expect(within(nav).getByRole("link", { name: /next/i })).toHaveAttribute(
      "href",
      "/inventory?q=a+b&publication_state=draft&offset=100",
    );
  });

  it("omits links at the edges and uses a custom offset parameter", () => {
    render(
      <Pagination
        page={pageInfo({ total: 3 })}
        itemCount={3}
        pathname="/p"
        params={{}}
        offsetParam="links_offset"
        label="Links"
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Showing 1–3 of 3")).toBeInTheDocument();
  });
});

describe("LinkTable", () => {
  it("shows resolution as text with a link to the target page", () => {
    render(<LinkTable links={[link()]} total={1} caption="Body links" emptyMessage="none" />);
    const table = screen.getByRole("table", { name: /body links/i });
    expect(within(table).getByText("Resolved")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Target page" })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
    expect(within(table).getByText("Internal")).toBeInTheDocument();
    expect(within(table).getByText("/reports/gadgets").tagName).toBe("CODE");
  });

  it("never makes a javascript: href clickable", () => {
    const { container } = render(
      <LinkTable
        links={[
          link({
            href: "javascript:alert(document.cookie)",
            link_type: "non_http",
            resolution: "not_applicable",
            resolved_url: "javascript:alert(document.cookie)",
            target_page_id: null,
          }),
        ]}
        total={1}
        caption="Body links"
        emptyMessage="none"
      />,
    );
    for (const anchor of Array.from(container.querySelectorAll("a"))) {
      expect(anchor.getAttribute("href") ?? "").not.toMatch(/^\s*javascript:/i);
    }
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(screen.getAllByText("javascript:alert(document.cookie)")).toHaveLength(2);
    expect(screen.getByText("Not applicable")).toBeInTheDocument();
    expect(screen.getByText("Non-HTTP")).toBeInTheDocument();
  });

  it("labels broken and unresolved links in text", () => {
    render(
      <LinkTable
        links={[
          link({ ordinal: 1, resolution: "broken", target_page_id: null }),
          link({ ordinal: 2, resolution: "unresolved", target_page_id: null, resolved_url: null }),
        ]}
        total={2}
        caption="Body links"
        emptyMessage="none"
      />,
    );
    expect(screen.getByText("Broken")).toBeInTheDocument();
    expect(screen.getByText("Unresolved")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Target page" })).toBeNull();
  });

  it("shows the linking page for inbound links and an empty message", () => {
    const { unmount } = render(
      <LinkTable links={[link()]} total={1} caption="Inbound links" showSource emptyMessage="none" />,
    );
    expect(screen.getByRole("columnheader", { name: "Linking page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://www.example.com/reports/widgets" })).toHaveAttribute(
      "href",
      `/inventory/pages/${PAGE_ID}`,
    );
    unmount();
    render(<LinkTable links={[]} total={0} caption="Inbound links" emptyMessage="Nothing links here." />);
    expect(screen.getByText("Nothing links here.")).toBeInTheDocument();
  });
});

describe("BodyText and HeadingsOutline", () => {
  it("renders markup-looking text as inert text", () => {
    const { container } = render(<BodyText text={"<b>bold</b><script>alert(1)</script>"} label="Body" />);
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByRole("region", { name: "Body" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("<b>bold</b><script>alert(1)</script>")).toBeInTheDocument();
  });

  it("states when there is no text or no headings", () => {
    render(<BodyText text="  " label="Body" />);
    expect(screen.getByText(/no body text/i)).toBeInTheDocument();
    render(<HeadingsOutline headings={[]} />);
    expect(screen.getByText(/no headings/i)).toBeInTheDocument();
  });

  it("lists headings with their level", () => {
    render(<HeadingsOutline headings={[{ block_id: "b1", level: 2, text: "Market size" }]} />);
    const item = screen.getByRole("listitem");
    expect(item).toHaveTextContent("H2Market size");
  });
});

describe("RunTable, IssueTable, KeyValueTable", () => {
  it("shows run status as text, duration and counters", () => {
    render(<RunTable runs={[runSummary()]} total={1} />);
    const row = screen.getByRole("row", { name: /www\.example\.com/ });
    expect(within(row).getByText("Completed with errors")).toBeInTheDocument();
    expect(within(row).getByText("2 min 05 s")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "https://www.example.com/" })).toHaveAttribute(
      "href",
      `/inventory/runs/${RUN_ID}`,
    );
    expect(screen.getAllByRole("columnheader")).toHaveLength(16);
  });

  it("says a running run is in progress", () => {
    render(<RunTable runs={[runSummary({ status: "running", completed_at: null })]} total={1} />);
    expect(screen.getByText("In progress")).toBeInTheDocument();
  });

  it("shows issue URLs as text, never as links", () => {
    const { container } = render(
      <IssueTable issues={[issue({ related_url: "https://x.test/r" })]} total={1} emptyMessage="none" />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("https://www.example.com/missing")).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("http_not_found")).toBeInTheDocument();
  });

  it("renders key/value rows as text with row headers", () => {
    render(
      <KeyValueTable caption="Config" rows={[{ key: "a", value: "<i>x</i>" }]} emptyMessage="empty" />,
    );
    expect(screen.getByRole("rowheader", { name: "a" })).toBeInTheDocument();
    expect(screen.getByText("<i>x</i>")).toBeInTheDocument();
  });
});

describe("PrimaryNav", () => {
  it.each([
    ["/inventory/pages/abc", "Inventory"],
    ["/inventory/runs/abc", "Runs"],
    ["/system", "System"],
  ])("marks the current section for %s", (path, label) => {
    pathname.current = path;
    render(<PrimaryNav />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const current = within(nav).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page");
    expect(current.map((a) => a.textContent)).toEqual([label]);
  });
});
