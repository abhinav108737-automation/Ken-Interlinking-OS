// @vitest-environment node
import { describe, expect, it } from "vitest";

import { browserPlatform, platform, resolvePlatform } from "@/lib/platform";

// Mirrors Next.js server rendering: there is no `window`.
describe("platform bridge on the server", () => {
  it("has no window in this environment", () => {
    expect(typeof window).toBe("undefined");
  });

  it("resolves to the browser implementation", async () => {
    expect(resolvePlatform()).toBe(browserPlatform);
    expect(platform).toBe(browserPlatform);
    await expect(platform.getPlatform()).resolves.toBe("web");
  });

  it("makes showNotification a no-op", async () => {
    await expect(browserPlatform.showNotification("t", "b")).resolves.toBeUndefined();
  });
});
