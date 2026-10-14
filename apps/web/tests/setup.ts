import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { resetRequestContext } from "./request-context";

// Server code (lib/api.ts) reads the session cookie and request headers through
// next/headers, which only works inside a real Next.js request. Every test gets
// an in-memory request scope instead (tests/request-context.ts).
vi.mock("next/headers", async () => (await import("./request-context")).nextHeadersModule);

// `globals` is off, so Testing Library cannot register its own cleanup hook.
afterEach(() => {
  cleanup();
  resetRequestContext();
});
