import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import GraphPage from "@/app/graph/page";
import GraphPagesPage from "@/app/graph/pages/page";
import type { ApiError, ApiResult } from "@/lib/api-client";
import { createApiClient, isGraphClusterList, isGraphOverview, isGraphPageList } from "@/lib/api-client";
import {
  GRAPH_DEFINITIONS,
  clusterLabel,
  clusterPagesHref,
  describeGraphPosition,
  graphPositionOf,
  parseFlag,
  parseOrdinal,
} from "@/lib/graph";

import { graphCluster, graphOverview, graphPageRow } from "./fixtures/graph";
import { OTHER_PAGE_ID, PAGE_ID, errorBody, jsonResponse, list } from "./fixtures/inventory";
import { TOKEN } from "./fixtures/review";

const api = vi.hoisted(() => ({
  getGraphOverview: vi.fn(),
  listGraphClusters: vi.fn(),
  listGraphPages: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, status: 200 };
}
function fail(error: Partial<ApiError> = {}): ApiResult<never> {
  return { ok: false, error: { kind: "network", path: "/graph/overview", message: "API unreachable.", ...error } };
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.getGraphOverview.mockResolvedValue(ok(graphOverview()));
  api.listGraphClusters.mockResolvedValue(ok([graphCluster()]));
  api.listGraphPages.mockResolvedValue(ok(list([graphPageRow()])));
});

describe("graph presentation", () => {
  it("separates the entry page from true orphans", () => {
    const entry = graphPageRow({ is_entry_page: true }, { is_orphan: true, in_degree: 0, crawl_depth: 0 });
    const orphan = graphPageRow({}, { is_orphan: true, in_degree: 0, crawl_depth: 3 });
    expect(graphPositionOf(entry)).toBe("entry_page");
    expect(describeGraphPosition("entry_page").label).toBe("Entry page");
    expect(describeGraphPosition("entry_page").description).toMatch(/not a page to recover/i);
    expect(graphPositionOf(orphan)).toBe("true_orphan");
    expect(graphPositionOf(graphPageRow({}, { is_nearly_orphan: true }))).toBe("nearly_orphan");
    expect(graphPositionOf(graphPageRow())).toBe("linked");
  });

  it("defines orphan, nearly-orphan, bridge and entry page", () => {
    expect(GRAPH_DEFINITIONS.map(([term]) => term)).toEqual([
      "Orphan page",
      "Nearly-orphan page",
      "Bridge page",
      "Entry page",
    ]);
  });

  it("parses ordinals and flags strictly", () => {
    expect(parseOrdinal({ cluster_id: "0" }, "cluster_id")).toBe(0);
    expect(parseOrdinal({ cluster_id: "12" }, "cluster_id")).toBe(12);
    for (const bad of ["-1", "1.5", "abc", "1000001", "../x", ""]) {
      expect(parseOrdinal({ cluster_id: bad }, "cluster_id"), bad).toBeUndefined();
    }
    expect(parseFlag({ orphans_only: "1" }, "orphans_only")).toBe(true);
    expect(parseFlag({ orphans_only: "yes" }, "orphans_only")).toBe(false);
    expect(clusterLabel(null)).toBe("No topic");
    expect(clusterPagesHref(null)).toBeNull();
    expect(clusterPagesHref(0)).toBe("/graph/pages?cluster_id=0");
  });
});

describe("graph client", () => {
  it("guards the three response shapes", () => {
    expect(isGraphOverview(graphOverview())).toBe(true);
    expect(isGraphOverview({ ...graphOverview(), true_orphans: "3" })).toBe(false);
    expect(isGraphClusterList([graphCluster()])).toBe(true);
    expect(isGraphClusterList({ items: [] })).toBe(false);
    expect(isGraphPageList(list([graphPageRow()]))).toBe(true);
    expect(isGraphPageList(list([{ ...graphPageRow(), is_entry_page: "no" }]))).toBe(false);
  });

  it("calls the contract URLs with the filters and the token", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(list([graphPageRow()]), 200));
    const client = createApiClient({ baseUrl: "http://api.test:8000", fetch: fetchMock, token: TOKEN });
    await client.listGraphPages({ cluster_id: 3, orphans_only: true, bridges_only: false, limit: 50, offset: 0 });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/graph/pages");
    expect(url.searchParams.get("cluster_id")).toBe("3");
    expect(url.searchParams.get("orphans_only")).toBe("true");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(init.method).toBe("GET");
  });

  it("maps 403 and 422, and refuses a malformed page id without sending", async () => {
    const fetchMock = vi.fn();
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("permission_denied"), 403));
    fetchMock.mockResolvedValueOnce(jsonResponse(errorBody("validation_failed"), 422));
    const client = createApiClient({ baseUrl: "http://api.test:8000", fetch: fetchMock, token: TOKEN });
    expect(await client.getGraphOverview()).toMatchObject({ ok: false, error: { status: 403 } });
    expect(await client.listGraphPages({ limit: 500 })).toMatchObject({ ok: false, error: { status: 422 } });
    expect(await client.getGraphPage("../evil")).toMatchObject({ ok: false, error: { kind: "invalid_argument" } });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("/graph", () => {
  it("headlines TRUE orphans and explains the raw count includes the entry page", async () => {
    render(await GraphPage());
    const overview = screen.getByRole("heading", { level: 2, name: "Overview" }).closest("section")!;
    expect(within(overview).getByText("True orphans").nextElementSibling).toHaveTextContent("3");
    expect(within(overview).getByText("Entry pages").nextElementSibling).toHaveTextContent("1");
    expect(within(overview).getByText("Bridge pages")).toBeInTheDocument();
    expect(within(overview).getByText("Components")).toBeInTheDocument();
    expect(overview).toHaveTextContent("includes the crawl entry page");
    expect(overview).toHaveTextContent("3 true orphans plus 1 entry page");
    expect(overview).toHaveTextContent("graph-alg1");
    // The raw orphan count never appears as a tile.
    expect(within(overview).queryByText("Orphans")).toBeNull();
    // The definitions are on the screen.
    expect(screen.getByText("Orphan page").nextElementSibling).toHaveTextContent(/no internal link pointing at it/i);
    expect(screen.getByText("Nearly-orphan page").nextElementSibling).toHaveTextContent(/barely/i);
    expect(screen.getByText("Bridge page").nextElementSibling).toHaveTextContent(/cut vertex/i);
  });

  it("lists clusters with a link to their pages and to the most central page", async () => {
    render(await GraphPage());
    const table = screen.getByRole("table", { name: /Topical clusters/ });
    expect(within(table).getByRole("link", { name: "Cluster #3" })).toHaveAttribute("href", "/graph/pages?cluster_id=3");
    expect(within(table).getByRole("link", { name: /lithium-ion-battery-market/ })).toHaveAttribute(
      "href",
      `/inventory/pages/${OTHER_PAGE_ID}`,
    );
  });

  it("says so when metrics were never computed, and shows an empty cluster list", async () => {
    api.getGraphOverview.mockResolvedValue(ok(graphOverview({ metrics_version: null, computed_at: null })));
    api.listGraphClusters.mockResolvedValue(ok([]));
    render(await GraphPage());
    expect(screen.getByText(/never been computed/i)).toBeInTheDocument();
    expect(screen.getByText(/No cluster\./)).toBeInTheDocument();
  });

  it("renders each failed call as a notice without blanking the page", async () => {
    api.getGraphOverview.mockResolvedValue(fail());
    api.listGraphClusters.mockResolvedValue(fail({ path: "/graph/clusters" }));
    render(await GraphPage());
    const alerts = screen.getAllByRole("alert").map((alert) => alert.textContent ?? "");
    expect(alerts.some((text) => text.includes("Graph overview unavailable"))).toBe(true);
    expect(alerts.some((text) => text.includes("Graph clusters unavailable"))).toBe(true);
    expect(screen.getByRole("heading", { level: 1, name: "Link graph" })).toBeInTheDocument();
  });
});

describe("/graph/pages", () => {
  it("sends only valid filters, as the contract's types", async () => {
    render(
      await GraphPagesPage({
        searchParams: Promise.resolve({ cluster_id: "3", component_id: "abc", orphans_only: "1", bridges_only: "x" }),
      }),
    );
    expect(api.listGraphPages).toHaveBeenCalledWith({
      cluster_id: 3,
      component_id: null,
      orphans_only: true,
      bridges_only: false,
      limit: 50,
      offset: 0,
    });
    expect(screen.getByLabelText("Cluster id")).toHaveValue("3");
    expect(screen.getByLabelText("Orphans")).toHaveValue("1");
  });

  it("labels the entry page as an entry page in the orphan view, and a true orphan as an orphan", async () => {
    api.listGraphPages.mockResolvedValue(
      ok(
        list([
          graphPageRow({ is_entry_page: true, title: "Home" }, { page_id: PAGE_ID, is_orphan: true, in_degree: 0, crawl_depth: 0 }),
          graphPageRow({ title: "Lost page", canonical_url: "https://www.example.com/lost" }, { page_id: OTHER_PAGE_ID, is_orphan: true, in_degree: 0, crawl_depth: 4, is_bridge: true }),
        ]),
      ),
    );
    render(await GraphPagesPage({ searchParams: Promise.resolve({ orphans_only: "1" }) }));
    expect(screen.getByRole("note")).toHaveTextContent("includes the crawl entry page");
    const rows = within(screen.getByRole("table", { name: /Pages in the link graph/ })).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Entry page");
    expect(rows[0]).not.toHaveTextContent(/\bOrphan\b/);
    expect(rows[1]).toHaveTextContent("Orphan");
    expect(rows[1]).toHaveTextContent("Bridge page");
    expect(within(rows[1]!).getByRole("link", { name: "Lost page" })).toHaveAttribute("href", `/inventory/pages/${OTHER_PAGE_ID}`);
  });

  it("renders a page title containing markup as text", async () => {
    api.listGraphPages.mockResolvedValue(ok(list([graphPageRow({ title: "<img src=x onerror=alert(1)>" })])));
    const { container } = render(await GraphPagesPage({ searchParams: Promise.resolve({}) }));
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("table", { name: /Pages in the link graph/ })).toHaveTextContent("<img src=x");
  });

  it("shows an empty state and a failed state", async () => {
    api.listGraphPages.mockResolvedValue(ok(list([])));
    const view = render(await GraphPagesPage({ searchParams: Promise.resolve({ bridges_only: "1" }) }));
    expect(screen.getByText("No page matches these filters.")).toBeInTheDocument();
    view.unmount();
    api.listGraphPages.mockResolvedValue(fail({ path: "/graph/pages" }));
    render(await GraphPagesPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Graph pages unavailable");
  });
});
