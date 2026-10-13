import { builtinModules } from "node:module";

import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// ---------------------------------------------------------------------------
// Architecture guards (ADR-0008). Proven by tests/electron-guard.test.ts and
// tests/portability-guard.test.ts — change those tests with these rules.
//
// ESLint flat config REPLACES (does not merge) a rule's options when several
// config objects match a file, so the reusable-code block below re-includes
// the Electron restrictions alongside its own.
// ---------------------------------------------------------------------------

const ELECTRON_MESSAGE =
  "Electron must not be imported by apps/web (ADR-0008). Use the typed platform bridge in " +
  "@/lib/platform; Electron code belongs under apps/desktop.";

/** Module names an Electron-specific import starts with. */
const ELECTRON_SPECIFIER = String.raw`^(?:electron(?:$|\x2F)|@electron(?:-forge)?\x2F)`;

const electronImports = {
  paths: [{ name: "electron", message: ELECTRON_MESSAGE }],
  patterns: [
    { group: ["electron/*", "@electron/*", "@electron-forge/*"], message: ELECTRON_MESSAGE },
  ],
};

/**
 * `no-restricted-imports` sees only static import/export declarations. These
 * selectors cover the other ways to load a module: require(), dynamic import(),
 * TypeScript `import x = require()` and `typeof import()` type queries.
 * esquery regexes cannot contain a literal "/", hence \x2F.
 */
function moduleLoadSelectors(specifierRegex, message) {
  const literal = `Literal[value=/${specifierRegex}/]`;
  const template = `TemplateLiteral[expressions.length=0] > TemplateElement[value.cooked=/${specifierRegex}/]`;
  return [
    `CallExpression[callee.type='Identifier'][callee.name='require'] > ${literal}`,
    `CallExpression[callee.type='Identifier'][callee.name='require'] > ${template}`,
    `ImportExpression > ${literal}`,
    `ImportExpression > ${template}`,
    `TSExternalModuleReference > ${literal}`,
    `TSImportType ${literal}`,
  ].map((selector) => ({ selector, message }));
}

/** Hosts through which renderer code reaches Node globals when nodeIntegration is (wrongly) on. */
const GLOBAL_HOSTS = "/^(?:window|globalThis|self|global)$/";

const electronSyntax = [
  ...moduleLoadSelectors(ELECTRON_SPECIFIER, ELECTRON_MESSAGE),
  // `window.require("electron")` / `globalThis["require"](...)` — the nodeIntegration
  // pattern from Electron tutorials. Banned for any argument.
  ...[
    `CallExpression[callee.type='MemberExpression'][callee.object.name=${GLOBAL_HOSTS}][callee.property.name='require']`,
    `CallExpression[callee.type='MemberExpression'][callee.object.name=${GLOBAL_HOSTS}][callee.property.value='require']`,
  ].map((selector) => ({ selector, message: ELECTRON_MESSAGE })),
];

// --- Portability guard for reusable code -----------------------------------
// Components and pure libraries must run unchanged in a browser, a Next.js
// server and a future Electron renderer (contextIsolation, no Node). They get
// data from FastAPI over HTTP, never from a database or the filesystem.

// Deny by default: every components/ and lib/ file is reusable unless it is a
// deliberate server entry listed in SERVER_ENTRIES. app/** (server components,
// route handlers) and root config files are outside this scope.
const REUSABLE_CODE = ["components/**", "lib/**"];

/** Deliberate server-only entry points inside lib/. Keep this list tiny. */
const SERVER_ENTRIES = ["lib/api.ts"];

const NODE_MESSAGE =
  "Reusable UI code must stay environment-agnostic (no Node built-ins). Do Node work in a " +
  "server-only module (e.g. lib/api.ts or an app/ server component) and pass plain data in.";
const SERVER_ONLY_MESSAGE =
  "Reusable UI code must not be server-only. Keep server-only modules out of components/ and pure lib/ files.";
const DB_MESSAGE =
  "The UI never talks to a database. All data access goes through the FastAPI control API over HTTP.";
const NEXT_SERVER_MESSAGE =
  "Server-only Next.js APIs (next/headers, next/server, next/cache) break in a browser or desktop " +
  "renderer. Use them only in app/** server components or route handlers.";
const SERVER_API_MESSAGE =
  "lib/api.ts reads server environment variables. Import types/clients from @/lib/api-client instead.";

const NODE_BUILTINS = [...new Set(builtinModules.filter((name) => !name.startsWith("_")))].sort();

const NEXT_SERVER_MODULES = ["next/headers", "next/server", "next/cache"];

const DB_CLIENTS = [
  "pg",
  "pg-native",
  "pg-pool",
  "pg-promise",
  "postgres",
  "slonik",
  "@neondatabase/serverless",
  "@vercel/postgres",
  "@electric-sql/pglite",
  "mysql",
  "mysql2",
  "mariadb",
  "sqlite",
  "sqlite3",
  "better-sqlite3",
  "mongodb",
  "mongoose",
  "redis",
  "ioredis",
  "knex",
  "typeorm",
  "sequelize",
  "drizzle-orm",
  "kysely",
  "@mikro-orm/core",
  "@supabase/supabase-js",
];

const reusableImports = {
  paths: [
    ...electronImports.paths,
    ...NODE_BUILTINS.map((name) => ({ name, message: NODE_MESSAGE })),
    { name: "server-only", message: SERVER_ONLY_MESSAGE },
    ...NEXT_SERVER_MODULES.map((name) => ({ name, message: NEXT_SERVER_MESSAGE })),
    ...DB_CLIENTS.map((name) => ({ name, message: DB_MESSAGE })),
  ],
  patterns: [
    ...electronImports.patterns,
    { group: ["node:*"], message: NODE_MESSAGE },
    {
      group: [
        "pg-*",
        "mysql*",
        "@prisma/*",
        "drizzle-orm/*",
        "@mikro-orm/*",
        "typeorm/*",
        "sequelize/*",
        "knex/*",
        "kysely/*",
        "mongodb/*",
      ],
      message: DB_MESSAGE,
    },
    { group: ["next/dist/server/*"], message: NEXT_SERVER_MESSAGE },
    { group: ["**/lib/api"], message: SERVER_API_MESSAGE },
  ],
};

const escapeRegex = (name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\//g, "\\x2F");
const NODE_SPECIFIER = `^(?:node:|(?:${NODE_BUILTINS.map(escapeRegex).join("|")})(?:$|\\x2F))`;
const DB_SPECIFIER = `^(?:(?:${DB_CLIENTS.map(escapeRegex).join("|")})(?:$|\\x2F)|pg-|mysql|@prisma\\x2F)`;

const NEXT_SERVER_SPECIFIER = String.raw`^next\x2F(?:(?:headers|server|cache)$|dist\x2Fserver\x2F)`;

const reusableSyntax = [
  ...electronSyntax,
  ...moduleLoadSelectors(NEXT_SERVER_SPECIFIER, NEXT_SERVER_MESSAGE),
  // `globalThis.process.env.X`, `window.Buffer`, `self["process"]` — Node globals
  // reached through a host object, which no-restricted-globals cannot see.
  ...[
    `MemberExpression[object.name=${GLOBAL_HOSTS}][property.name=/^(?:process|Buffer|require|module)$/]`,
    `MemberExpression[object.name=${GLOBAL_HOSTS}][property.value=/^(?:process|Buffer|require|module)$/]`,
  ].map((selector) => ({ selector, message: NODE_MESSAGE })),
  ...moduleLoadSelectors(NODE_SPECIFIER, NODE_MESSAGE),
  ...moduleLoadSelectors("^server-only$", SERVER_ONLY_MESSAGE),
  ...moduleLoadSelectors(DB_SPECIFIER, DB_MESSAGE),
];

// Flat config. `next lint` was removed in Next.js 16; the ESLint CLI is used directly.
export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    name: "ken/electron-import-guard",
    rules: {
      "no-restricted-imports": ["error", electronImports],
      "no-restricted-syntax": ["error", ...electronSyntax],
    },
  },
  {
    name: "ken/reusable-code-portability-guard",
    files: REUSABLE_CODE,
    ignores: SERVER_ENTRIES,
    rules: {
      "no-restricted-imports": ["error", reusableImports],
      "no-restricted-syntax": ["error", ...reusableSyntax],
      "no-restricted-globals": [
        "error",
        ...["process", "Buffer", "__dirname", "__filename", "require", "module", "global"].map(
          (name) => ({ name, message: NODE_MESSAGE }),
        ),
      ],
    },
  },
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Generated from contracts/openapi/openapi.json by `npm run gen:types`. Never hand-edited.
    "lib/types.gen.ts",
  ]),
]);
