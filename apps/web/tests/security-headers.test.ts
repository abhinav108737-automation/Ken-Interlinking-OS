// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import nextConfig from "@/next.config";
import { proxy } from "@/proxy";
import { REQUEST_PATH_HEADER } from "@/lib/session";

describe("Content-Security-Policy", () => {
  it("is sent on every route and allows only this origin", async () => {
    const rules = await nextConfig.headers!();
    const all = rules.find((rule) => rule.source === "/:path*")!;
    const csp = all.headers.find((header) => header.key === "Content-Security-Policy")?.value ?? "";
    for (const directive of [
      "default-src 'self'",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "connect-src 'self'",
    ]) {
      expect(csp).toContain(directive);
    }
    expect(csp).not.toMatch(/https?:\/\//);
    expect(csp).not.toContain("unsafe-eval");
  });
});

describe("proxy", () => {
  it("only records the requested path, overwriting any client value; it never redirects", () => {
    const request = new NextRequest("http://127.0.0.1:3000/recommendations?status=all", {
      headers: { [REQUEST_PATH_HEADER]: "//evil.example", cookie: "ken_session=x" },
    });
    const response = proxy(request);
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get(`x-middleware-request-${REQUEST_PATH_HEADER}`)).toBe("/recommendations?status=all");
  });
});
