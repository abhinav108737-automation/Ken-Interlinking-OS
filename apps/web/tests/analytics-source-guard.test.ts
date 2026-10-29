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
    // POSIX separators: `join` yields backslashes on Windows and every expectation
    // below is written with "/". `read` accepts either.
    return /\.tsx?$/.test(entry) ? [path.split("\\").join("/")] : [];
  });
}

/** Every file the analytics work owns or touched, plus the panels' host screens. */
const ANALYTICS_SOURCES = [
  ...sourceFiles("app/analytics"),
  ...sourceFiles("components/analytics"),
  "lib/analytics.ts",
  "app/recommendations/[planId]/page.tsx",
  "app/deployments/[deploymentId]/page.tsx",
  "app/inventory/pages/[pageId]/page.tsx",
];

function read(file: string): string {
  return readFileSync(join(WEB_ROOT, file), "utf8");
}

describe("analytics sources", () => {
  it("scans the real files", () => {
    expect(ANALYTICS_SOURCES.length).toBeGreaterThan(12);
    expect(ANALYTICS_SOURCES).toContain("app/analytics/page.tsx");
    expect(ANALYTICS_SOURCES).toContain("components/analytics/Sparkline.tsx");
  });

  it("never injects HTML: no dangerouslySetInnerHTML and no innerHTML anywhere", () => {
    for (const file of ANALYTICS_SOURCES) {
      const source = read(file);
      expect(source, file).not.toContain("dangerouslySetInnerHTML");
      expect(source, file).not.toContain("innerHTML");
    }
  });

  it("adds no dependency: every import is React, Next, a local module or node:url in tests", () => {
    const allowed = /^(?:react|react-dom|next(?:\/[\w-]+)?|@\/[\w./[\]-]+|\.{1,2}\/[\w./[\]-]+)$/;
    for (const file of [...sourceFiles("app/analytics"), ...sourceFiles("components/analytics"), "lib/analytics.ts"]) {
      for (const match of read(file).matchAll(/from "([^"]+)"/g)) {
        expect(allowed.test(match[1]!), `${file} imports ${match[1]}`).toBe(true);
      }
    }
  });

  it("keeps the session token out of the client: only lib/api.ts is imported for data", () => {
    // Screens (app/**) call lib/api.ts on the server; reusable code must not.
    for (const file of sourceFiles("components/analytics")) {
      expect(read(file), file).not.toMatch(/from "@\/lib\/api"/);
      expect(read(file), file).not.toContain("next/headers");
      expect(read(file), file).not.toContain("process.env");
    }
    for (const file of sourceFiles("app/analytics")) {
      const source = read(file);
      expect(source, file).not.toContain("use client");
      expect(source, file).not.toContain("process.env");
      expect(source, file).not.toMatch(/\bfetch\s*\(/);
    }
  });

  it("the analytics screens are server-rendered and uncached", () => {
    for (const file of sourceFiles("app/analytics").filter((name) => name.endsWith("page.tsx"))) {
      expect(read(file), file).toContain('export const dynamic = "force-dynamic"');
    }
  });

  it("the analytics routes are GET-only: no server action and no mutation call", () => {
    for (const file of [...sourceFiles("app/analytics"), ...sourceFiles("components/analytics")]) {
      const source = read(file);
      expect(source, file).not.toContain("use server");
      expect(source, file).not.toMatch(/\b(?:approvePlan|rejectPlan|createDeployment|rollbackDeployment|verifyDeployment)\b/);
      expect(source, file).not.toMatch(/method="post"/i);
    }
  });
});
