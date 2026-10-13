// @vitest-environment node
import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { WEB_ROOT, guardErrors, lintAs } from "./eslint-harness";

// Linting loads the full Next.js ESLint config; allow for a cold start.
const LINT_TIMEOUT_MS = 30_000;

const SAMPLE_PATHS = [
  "components/ElectronProbe.tsx",
  "lib/electron-probe.ts",
  "app/electron-probe/page.tsx",
] as const;

const FORBIDDEN: ReadonlyArray<readonly [string, string]> = [
  ["static import", `import { ipcRenderer } from "electron";\nexport const x = ipcRenderer;\n`],
  ["side-effect import", `import "electron";\n`],
  ["import type", `import type { IpcRenderer } from "electron";\nexport type X = IpcRenderer;\n`],
  ["subpath import", `import { app } from "electron/main";\nexport const x = app;\n`],
  ["@electron scope import", `import { notarize } from "@electron/notarize";\nexport const x = notarize;\n`],
  ["re-export", `export { shell } from "electron";\n`],
  ["require()", `const electron = require("electron");\nexport const x = electron;\n`],
  ["require() subpath", `const r = require("electron/renderer");\nexport const x = r;\n`],
  ["require() template literal", "const e = require(`electron`);\nexport const x = e;\n"],
  ["dynamic import()", `export const load = () => import("electron");\n`],
  ["dynamic import() subpath", `export const load = () => import("electron/renderer");\n`],
  ["TS import-equals", `import electron = require("electron");\nexport const x = electron;\n`],
  ["typeof import() type query", `export type E = typeof import("electron");\n`],
  ["window.require()", `const e = window.require("electron");\nexport const x = e;\n`],
  ["window.require() of any module", `export const fs = window.require("fs");\n`],
  ["globalThis.require()", `export const fs = globalThis.require("fs");\n`],
  ["computed window[\"require\"]()", `export const e = window["require"]("electron");\n`],
];

describe("Electron import guard (ADR-0008)", () => {
  describe.each(SAMPLE_PATHS)("in %s", (filePath) => {
    it.each(FORBIDDEN)(
      "rejects %s",
      async (_label, code) => {
        const errors = await guardErrors(filePath, code);
        expect(errors.length).toBeGreaterThan(0);
        expect(errors.map((e) => e.message).join("\n")).toContain("ADR-0008");
      },
      LINT_TIMEOUT_MS,
    );
  });

  it(
    "allows ordinary imports and non-Electron names that merely start with 'electron'",
    async () => {
      const code = [
        `import { useState } from "react";`,
        `import { platform } from "@/lib/platform";`,
        `import type { PlatformBridge } from "@/lib/platform";`,
        `export const a = useState;`,
        `export const b: PlatformBridge = platform;`,
        `export const label = "electron";`,
        `export const load = () => import("electronics-catalog");`,
        "",
      ].join("\n");
      for (const filePath of SAMPLE_PATHS) {
        expect(await guardErrors(filePath, code)).toEqual([]);
      }
    },
    LINT_TIMEOUT_MS,
  );

  it(
    "reports no lint errors at all for a clean component",
    async () => {
      const code = `export function Ok() {\n  return <p>ok</p>;\n}\n`;
      expect(await lintAs("components/Ok.tsx", code)).toEqual([]);
    },
    LINT_TIMEOUT_MS,
  );
});

describe("Electron is not installed", () => {
  const REPO_ROOT = path.resolve(WEB_ROOT, "..", "..");

  function isElectronPackage(name: string): boolean {
    // electron-to-chromium is a browserslist data table, not Electron or a build tool.
    if (name === "electron-to-chromium") return false;
    return (
      name === "electron" ||
      name.startsWith("electron-") ||
      name.startsWith("@electron/") ||
      name.startsWith("@electron-forge/")
    );
  }

  function readJson(file: string): Record<string, unknown> {
    return JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
  }

  function declaredDependencies(manifest: Record<string, unknown>): string[] {
    return [
      "dependencies",
      "devDependencies",
      "optionalDependencies",
      "peerDependencies",
    ].flatMap((field) => Object.keys((manifest[field] as Record<string, string> | undefined) ?? {}));
  }

  it("the predicate recognises Electron tooling", () => {
    for (const name of [
      "electron",
      "electron-builder",
      "electron-forge",
      "electron-packager",
      "electron-updater",
      "@electron/rebuild",
      "@electron-forge/cli",
    ]) {
      expect(isElectronPackage(name)).toBe(true);
    }
    expect(isElectronPackage("electron-to-chromium")).toBe(false);
    expect(isElectronPackage("react")).toBe(false);
  });

  it.each([
    ["apps/web/package.json", path.join(WEB_ROOT, "package.json")],
    ["root package.json", path.join(REPO_ROOT, "package.json")],
  ])("%s declares no Electron package", (_label, file) => {
    expect(declaredDependencies(readJson(file)).filter(isElectronPackage)).toEqual([]);
  });

  it("root package-lock.json has no top-level Electron package", () => {
    const lock = readJson(path.join(REPO_ROOT, "package-lock.json"));
    const packages = Object.keys((lock["packages"] as Record<string, unknown> | undefined) ?? {});
    expect(packages.length).toBeGreaterThan(0);
    const topLevel = packages
      .filter((key) => key.startsWith("node_modules/") && !key.slice("node_modules/".length).includes("node_modules/"))
      .map((key) => key.slice("node_modules/".length));
    expect(topLevel.filter(isElectronPackage)).toEqual([]);
    // The root workspace entry ("") must not declare Electron either.
    const root = (lock["packages"] as Record<string, Record<string, unknown>>)[""] ?? {};
    expect(declaredDependencies(root).filter(isElectronPackage)).toEqual([]);
  });
});
