import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DependencyTable } from "@/components/DependencyTable";
import type { DependencyCheck } from "@/lib/status";

const CHECKS: DependencyCheck[] = [
  {
    name: "postgres",
    kind: "database",
    required: true,
    status: "ok",
    latency_ms: 3.2,
    detail: { host: "localhost", port: 5433 },
    error: null,
  },
  {
    name: "ollama",
    kind: "model_server",
    required: false,
    status: "unavailable",
    latency_ms: 2001,
    detail: {},
    error: "connection refused",
  },
  {
    name: "pgvector",
    kind: "extension",
    required: false,
    status: "skipped",
    latency_ms: 0,
    detail: {},
    error: null,
  },
];

describe("DependencyTable", () => {
  it("is an accessible table with a caption and column headers", () => {
    render(<DependencyTable checks={CHECKS} />);
    const table = screen.getByRole("table", { name: /dependency checks/i });
    expect(table).toBeInTheDocument();
    expect(within(table).getByText("(3)")).toBeInTheDocument();

    const columnHeaders = within(table).getAllByRole("columnheader");
    expect(columnHeaders.map((h) => h.textContent)).toEqual([
      "Name",
      "Kind",
      "Requirement",
      "Status",
      "Latency",
      "Error / detail",
    ]);
    for (const header of columnHeaders) {
      expect(header).toHaveAttribute("scope", "col");
    }
  });

  it("renders one row per check, headed by the check name", () => {
    render(<DependencyTable checks={CHECKS} />);
    const rowHeaders = screen.getAllByRole("rowheader");
    expect(rowHeaders.map((h) => h.textContent)).toEqual(["postgres", "ollama", "pgvector"]);
  });

  it("labels required and optional dependencies in text", () => {
    render(<DependencyTable checks={CHECKS} />);
    const postgres = screen.getByRole("row", { name: /postgres/ });
    const ollama = screen.getByRole("row", { name: /ollama/ });
    expect(within(postgres).getByText("Required")).toBeInTheDocument();
    expect(within(ollama).getByText("Optional")).toBeInTheDocument();
  });

  it("conveys status as text, not colour alone", () => {
    render(<DependencyTable checks={CHECKS} />);
    expect(within(screen.getByRole("row", { name: /postgres/ })).getByText("OK")).toBeInTheDocument();
    expect(
      within(screen.getByRole("row", { name: /ollama/ })).getByText("Unavailable"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("row", { name: /pgvector/ })).getByText("Skipped"),
    ).toBeInTheDocument();
  });

  it("shows kind labels, latency, error text and detail", () => {
    render(<DependencyTable checks={CHECKS} />);
    const ollama = screen.getByRole("row", { name: /ollama/ });
    expect(within(ollama).getByText("Model server")).toBeInTheDocument();
    expect(within(ollama).getByText("2.00 s")).toBeInTheDocument();
    expect(within(ollama).getByText(/connection refused/)).toBeInTheDocument();

    const postgres = screen.getByRole("row", { name: /postgres/ });
    expect(within(postgres).getByText("3 ms")).toBeInTheDocument();
    expect(within(postgres).getByText("host: localhost; port: 5433")).toBeInTheDocument();

    const pgvector = screen.getByRole("row", { name: /pgvector/ });
    expect(within(pgvector).getByText("None")).toBeInTheDocument();
  });

  it("renders an unknown status as explicit text", () => {
    render(
      <DependencyTable
        checks={[
          { name: "cms", kind: "cms", required: true, status: "melted" as never, latency_ms: 1, detail: {}, error: null },
        ]}
      />,
    );
    expect(screen.getByText("Unknown (melted)")).toBeInTheDocument();
  });

  it("states when there are no checks", () => {
    render(<DependencyTable checks={[]} />);
    expect(screen.getByText(/reported no dependency checks/i)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /dependency checks/i })).toBeInTheDocument();
  });
});
