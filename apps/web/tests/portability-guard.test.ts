// @vitest-environment node
import { describe, expect, it } from "vitest";

import { guardErrors } from "./eslint-harness";

const LINT_TIMEOUT_MS = 30_000;

/** Files that must stay environment-agnostic (see REUSABLE_CODE in eslint.config.mjs). */
const REUSABLE_SAMPLES = [
  "components/PortabilityProbe.tsx",
  "lib/status.ts",
  "lib/platform/probe.ts",
  "lib/api-client.ts",
  // lib/** is guarded by default: a brand-new file is covered without editing the config.
  "lib/newthing.ts",
  "lib/nested/deep/new-server-helper.ts",
] as const;

const FORBIDDEN: ReadonlyArray<readonly [string, string, string]> = [
  ["Node built-in", `import fs from "fs";\nexport const x = fs;\n`, "Node built-ins"],
  ["node: built-in subpath", `import { readFile } from "node:fs/promises";\nexport const x = readFile;\n`, "Node built-ins"],
  ["path built-in", `import path from "path";\nexport const x = path;\n`, "Node built-ins"],
  ["require() of a built-in", `const cp = require("child_process");\nexport const x = cp;\n`, "Node built-ins"],
  ["process global", `export const x = process.env["X"];\n`, "Node built-ins"],
  ["server-only", `import "server-only";\n`, "server-only"],
  ["pg", `import { Pool } from "pg";\nexport const x = Pool;\n`, "never talks to a database"],
  ["postgres.js", `import postgres from "postgres";\nexport const x = postgres;\n`, "never talks to a database"],
  ["Prisma", `import { PrismaClient } from "@prisma/client";\nexport const x = PrismaClient;\n`, "never talks to a database"],
  ["drizzle subpath", `import { drizzle } from "drizzle-orm/node-postgres";\nexport const x = drizzle;\n`, "never talks to a database"],
  ["mysql2", `import mysql from "mysql2/promise";\nexport const x = mysql;\n`, "never talks to a database"],
  ["dynamic import of a DB client", `export const load = () => import("knex");\n`, "never talks to a database"],
  ["next/headers", `import { cookies } from "next/headers";\nexport const x = cookies;\n`, "Server-only Next.js APIs"],
  ["next/server", `import { NextResponse } from "next/server";\nexport const x = NextResponse;\n`, "Server-only Next.js APIs"],
  ["next/cache", `import { revalidatePath } from "next/cache";\nexport const x = revalidatePath;\n`, "Server-only Next.js APIs"],
  ["next/dist/server internals", `import x from "next/dist/server/request/cookies";\nexport const y = x;\n`, "Server-only Next.js APIs"],
  ["dynamic import of next/headers", `export const load = () => import("next/headers");\n`, "Server-only Next.js APIs"],
  ["globalThis.process", `export const x = globalThis.process.env["X"];\n`, "Node built-ins"],
  ["window.process", `export const x = window.process;\n`, "Node built-ins"],
  ["computed self[\"Buffer\"]", `export const x = self["Buffer"];\n`, "Node built-ins"],
  ["globalThis.require", `export const x = globalThis.require("fs");\n`, "Node built-ins"],
  ["env-reading server API module", `import { getReady } from "@/lib/api";\nexport const x = getReady;\n`, "@/lib/api-client"],
];

describe("portability guard for reusable code", () => {
  describe.each(REUSABLE_SAMPLES)("in %s", (filePath) => {
    it.each(FORBIDDEN)(
      "rejects %s",
      async (_label, code, expectedMessage) => {
        const errors = await guardErrors(filePath, code);
        expect(errors.length).toBeGreaterThan(0);
        expect(errors.map((e) => e.message).join("\n")).toContain(expectedMessage);
      },
      LINT_TIMEOUT_MS,
    );

    it(
      "allows portable imports",
      async () => {
        const code = [
          `import type { ApiError } from "@/lib/api-client";`,
          `import { describeApiErrorKind } from "@/lib/status";`,
          `import { platform } from "@/lib/platform";`,
          `export const a = describeApiErrorKind;`,
          `export const b = platform;`,
          `export type C = ApiError;`,
          "",
        ].join("\n");
        expect(await guardErrors(filePath, code)).toEqual([]);
      },
      LINT_TIMEOUT_MS,
    );
  });

  it.each(["lib/api.ts", "app/system/page.tsx", "next.config.ts"])(
    "does not apply to server-side file %s",
    async (filePath) => {
      const code = [
        `import path from "node:path";`,
        `import { getReady } from "@/lib/api";`,
        `export const a = path.sep;`,
        `export const b = getReady;`,
        `export const c = process.env["KEN_API_BASE_URL"];`,
        "",
      ].join("\n");
      expect(await guardErrors(filePath, code)).toEqual([]);
    },
    LINT_TIMEOUT_MS,
  );

  it.each(["app/system/page.tsx", "app/api/probe/route.ts"])(
    "allows server-only Next.js APIs and globalThis.process in %s",
    async (filePath) => {
      const code = [
        `import { cookies } from "next/headers";`,
        `import { NextResponse } from "next/server";`,
        `import { revalidatePath } from "next/cache";`,
        `export const a = [cookies, NextResponse, revalidatePath];`,
        `export const b = globalThis.process.env["KEN_API_BASE_URL"];`,
        "",
      ].join("\n");
      expect(await guardErrors(filePath, code)).toEqual([]);
    },
    LINT_TIMEOUT_MS,
  );

  it(
    "closes the laundering route: a new lib file cannot pull in Node code for a component",
    async () => {
      // The would-be server helper is itself rejected, so components cannot reach Node through it.
      const helper = `import { readFileSync } from "node:fs";\nexport const read = readFileSync;\n`;
      const errors = await guardErrors("lib/newserverthing.ts", helper);
      expect(errors.map((e) => e.message).join("\n")).toContain("Node built-ins");
    },
    LINT_TIMEOUT_MS,
  );

  it(
    "still blocks Electron in server-side files",
    async () => {
      const errors = await guardErrors("lib/api.ts", `import "electron";\n`);
      expect(errors.map((e) => e.message).join("\n")).toContain("ADR-0008");
    },
    LINT_TIMEOUT_MS,
  );
});
