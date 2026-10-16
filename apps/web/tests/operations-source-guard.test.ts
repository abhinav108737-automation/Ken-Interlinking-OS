// @vitest-environment node
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));

function sourceFiles(relative: string): string[] {
  return readdirSync(join(WEB_ROOT, relative)).flatMap((entry) => {
    const path = join(relative, entry);
    if (statSync(join(WEB_ROOT, path)).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry) ? [path] : [];
  });
}

function read(file: string): string {
  return readFileSync(join(WEB_ROOT, file), "utf8");
}

/** Every file the autonomy-first operations control plane owns. */
const OPERATIONS_SOURCES = [
  ...sourceFiles("app/autonomy"),
  ...sourceFiles("app/learning"),
  ...sourceFiles("components/autonomy"),
  ...sourceFiles("components/learning"),
  "components/recommendations/GraphSignalsPanel.tsx",
  "lib/autonomy.ts",
  "lib/learning.ts",
];

const REUSABLE = [
  ...sourceFiles("components/autonomy"),
  ...sourceFiles("components/learning"),
  "components/recommendations/GraphSignalsPanel.tsx",
  "lib/autonomy.ts",
  "lib/learning.ts",
];

const SCREENS = [...sourceFiles("app/autonomy"), ...sourceFiles("app/learning")];

describe("operations sources", () => {
  it("scans the real files", () => {
    expect(OPERATIONS_SOURCES.length).toBeGreaterThan(15);
    expect(OPERATIONS_SOURCES).toContain("app/autonomy/page.tsx");
    expect(OPERATIONS_SOURCES).toContain("app/autonomy/blocked/[planId]/page.tsx");
    expect(OPERATIONS_SOURCES).toContain("components/autonomy/AutonomyModeBanner.tsx");
    expect(OPERATIONS_SOURCES).toContain("components/learning/CorpusBanner.tsx");
  });

  it("never injects HTML: no dangerouslySetInnerHTML and no innerHTML anywhere", () => {
    for (const file of OPERATIONS_SOURCES) {
      const source = read(file);
      expect(source, file).not.toContain("dangerouslySetInnerHTML");
      expect(source, file).not.toContain("innerHTML");
    }
  });

  it("adds no dependency: every import is React, Next or a local module", () => {
    const allowed = /^(?:react|react-dom|next(?:\/[\w-]+)?|@\/[\w./[\]-]+|\.{1,2}\/[\w./[\]-]+)$/;
    for (const file of OPERATIONS_SOURCES) {
      for (const match of read(file).matchAll(/from "([^"]+)"/g)) {
        expect(allowed.test(match[1]!), `${file} imports ${match[1]}`).toBe(true);
      }
    }
  });

  // ADR-0013 §2: the session token lives in lib/api.ts, on the server, and must
  // never reach a client bundle. Reusable code therefore never imports it.
  it("keeps the session token out of the client bundle", () => {
    for (const file of REUSABLE) {
      const source = read(file);
      expect(source, file).not.toMatch(/from "@\/lib\/api"/);
      expect(source, file).not.toContain("next/headers");
      expect(source, file).not.toContain("process.env");
      expect(source, file).not.toContain("KEN_API_BASE_URL");
      expect(source, file).not.toMatch(/\bAuthorization\b/);
    }
  });

  // ADR-0008 / directive §23: apps/web stays canonical and no React component
  // may import Electron. FastAPI is the only boundary, so no component or
  // screen may reach a database or the network directly.
  it("imports no Electron and reaches no database", () => {
    for (const file of OPERATIONS_SOURCES) {
      const source = read(file);
      expect(source.toLowerCase(), file).not.toContain("electron");
      expect(source, file).not.toMatch(/from "(?:pg|postgres|mysql2?|knex|drizzle-orm|@prisma\/client)(?:\/|")/);
      expect(source, file).not.toMatch(/from "node:/);
    }
  });

  it("the client components do no data access of their own", () => {
    const clients = REUSABLE.filter((file) => read(file).includes('"use client"'));
    // The override panel and the promotion panel are the client components here.
    expect(clients.length).toBeGreaterThan(0);
    for (const file of clients) {
      const source = read(file);
      expect(source, file).not.toMatch(/\bfetch\s*\(/);
      expect(source, file).not.toMatch(/from "@\/lib\/api"/);
    }
  });

  it("every operations screen is server-rendered and uncached", () => {
    for (const file of SCREENS.filter((name) => name.endsWith("page.tsx"))) {
      expect(read(file), file).toContain('export const dynamic = "force-dynamic"');
    }
  });

  it("no operations screen is a client component or reads the environment", () => {
    for (const file of SCREENS) {
      const source = read(file);
      expect(source, file).not.toContain('"use client"');
      expect(source, file).not.toContain("process.env");
      expect(source, file).not.toMatch(/\bfetch\s*\(/);
    }
  });

  // The ONLY mutations in this workstream are the operator override and the
  // proposal advance, and each lives in its own `"use server"` module.
  it("mutations live only in the two server-action modules", () => {
    const actions = SCREENS.filter((file) => read(file).includes("use server"));
    expect(actions.sort()).toEqual(["app/autonomy/actions.ts", "app/learning/actions.ts"]);
    for (const file of [...REUSABLE, ...SCREENS.filter((name) => !actions.includes(name))]) {
      expect(read(file), file).not.toContain("use server");
    }
  });

  // THE PHASE 6 LESSON, asserted at the source level: each mutation sends the
  // form's operation id, and each form mints exactly one.
  it("each mutation reuses the form's operation id, and each form mints exactly one", () => {
    for (const file of ["app/autonomy/actions.ts", "app/learning/actions.ts"]) {
      const source = read(file);
      expect(source, file).toContain("operationIdFrom(form)");
      expect(source, file).toMatch(/isUuid\(id\)/);
    }
    for (const file of ["components/autonomy/OverridePanel.tsx", "components/learning/PromotionPanel.tsx"]) {
      const source = read(file);
      // One mint, in a `useState` initialiser, so a re-render never changes it.
      expect([...source.matchAll(/crypto\.randomUUID\(\)/g)], file).toHaveLength(1);
      expect(source, file).toContain("useState(() => crypto.randomUUID())");
      // The form is re-keyed on a concluded operation, which is what re-mints it.
      expect(source, file).toMatch(/key=\{\w+\.round\}/);
    }
  });

  // ADR-0018 §2 and ADR-0016 §16.1: the two banners are not dismissible, so
  // neither carries any control that could hide it.
  it("the autonomy-mode and corpus banners have no dismiss control", () => {
    for (const file of ["components/autonomy/AutonomyModeBanner.tsx", "components/learning/CorpusBanner.tsx"]) {
      const source = read(file);
      expect(source, file).not.toContain("<button");
      expect(source, file).not.toContain("onClick");
      expect(source, file).not.toContain("localStorage");
    }
  });

  // Charts stay hand-written SVG with a table of the same numbers; this
  // workstream added no chart at all, so it must have added no SVG either.
  it("adds no charting library and no new chart", () => {
    for (const file of OPERATIONS_SOURCES) {
      const source = read(file);
      expect(source, file).not.toMatch(/recharts|chart\.js|d3|plotly|victory|nivo/i);
      expect(source, file).not.toContain("<svg");
    }
  });
});
