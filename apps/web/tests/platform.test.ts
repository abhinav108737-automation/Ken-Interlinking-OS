import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type PlatformBridge,
  browserPlatform,
  isPlatformBridge,
  platform,
  resolvePlatform,
} from "@/lib/platform";

afterEach(() => {
  vi.unstubAllGlobals();
});

function fakeDesktopBridge(kind: unknown = "desktop") {
  return {
    getPlatform: vi.fn(async (): Promise<unknown> => kind),
    showNotification: vi.fn<PlatformBridge["showNotification"]>(async () => undefined),
  };
}

describe("browserPlatform", () => {
  it("reports the web platform", async () => {
    await expect(browserPlatform.getPlatform()).resolves.toBe("web");
  });

  it("is a no-op when the Notification API is unavailable", async () => {
    vi.stubGlobal("Notification", undefined);
    Reflect.deleteProperty(globalThis, "Notification");
    expect("Notification" in window).toBe(false);
    await expect(browserPlatform.showNotification("t", "b")).resolves.toBeUndefined();
  });

  describe("with the Notification API", () => {
    function installNotification(permission: NotificationPermission) {
      const constructed: Array<[string, NotificationOptions | undefined]> = [];
      const requestPermission = vi.fn(async () => "granted" as NotificationPermission);
      class FakeNotification {
        static permission = permission;
        static requestPermission = requestPermission;
        constructor(title: string, options?: NotificationOptions) {
          constructed.push([title, options]);
        }
      }
      vi.stubGlobal("Notification", FakeNotification);
      expect("Notification" in window).toBe(true);
      return { constructed, requestPermission };
    }

    it.each(["default", "denied"] as const)(
      "does nothing and never requests permission when permission is %s",
      async (permission) => {
        const { constructed, requestPermission } = installNotification(permission);
        await browserPlatform.showNotification("Title", "Body");
        expect(constructed).toEqual([]);
        expect(requestPermission).not.toHaveBeenCalled();
      },
    );

    it("shows a notification when permission is already granted", async () => {
      const { constructed, requestPermission } = installNotification("granted");
      await browserPlatform.showNotification("Title", "Body");
      expect(constructed).toEqual([["Title", { body: "Body" }]]);
      expect(requestPermission).not.toHaveBeenCalled();
    });

    it("resolves even if the Notification constructor throws", async () => {
      class ThrowingNotification {
        static permission: NotificationPermission = "granted";
        constructor() {
          throw new TypeError("Illegal constructor");
        }
      }
      vi.stubGlobal("Notification", ThrowingNotification);
      await expect(browserPlatform.showNotification("t", "b")).resolves.toBeUndefined();
    });
  });
});

describe("platform export", () => {
  it("is the browser implementation when no desktop bridge is exposed", async () => {
    expect(window.kenDesktop).toBeUndefined();
    expect(platform).toBe(browserPlatform);
    await expect(platform.getPlatform()).resolves.toBe("web");
  });
});

describe("isPlatformBridge", () => {
  it("accepts an object with every bridge method as a function", () => {
    expect(isPlatformBridge(fakeDesktopBridge())).toBe(true);
    expect(isPlatformBridge(browserPlatform)).toBe(true);
  });

  const NOT_BRIDGES: ReadonlyArray<readonly [string, unknown]> = [
    ["undefined", undefined],
    ["null", null],
    ["a string", "kenDesktop"],
    ["a function", () => undefined],
    ["an empty object", {}],
    ["a missing method", { getPlatform: async () => "desktop" }],
    ["a non-function method", { getPlatform: async () => "desktop", showNotification: "nope" }],
  ];

  it.each(NOT_BRIDGES)("rejects %s", (_label, value) => {
    expect(isPlatformBridge(value)).toBe(false);
  });

  it("rejects, without throwing, an object whose getter throws", () => {
    const hostile = {
      getPlatform: async () => "desktop",
      get showNotification(): never {
        throw new Error("boom");
      },
    };
    expect(() => isPlatformBridge(hostile)).not.toThrow();
    expect(isPlatformBridge(hostile)).toBe(false);
  });
});

describe("resolvePlatform", () => {
  it("picks a valid desktop bridge and delegates to it", async () => {
    const bridge = fakeDesktopBridge();
    const resolved: PlatformBridge = resolvePlatform({ kenDesktop: bridge });
    expect(resolved).not.toBe(browserPlatform);

    await expect(resolved.getPlatform()).resolves.toBe("desktop");
    await resolved.showNotification("Deployed", "3 links");
    expect(bridge.getPlatform).toHaveBeenCalledTimes(1);
    expect(bridge.showNotification).toHaveBeenCalledWith("Deployed", "3 links");
  });

  it("reads window.kenDesktop by default", async () => {
    const bridge = fakeDesktopBridge();
    window.kenDesktop = bridge;
    try {
      const resolved = resolvePlatform();
      await resolved.getPlatform();
      expect(bridge.getPlatform).toHaveBeenCalledTimes(1);
    } finally {
      delete window.kenDesktop;
    }
  });

  it("validates the platform kind returned across the bridge", async () => {
    await expect(resolvePlatform({ kenDesktop: fakeDesktopBridge("web") }).getPlatform()).resolves.toBe("web");
    await expect(
      resolvePlatform({ kenDesktop: fakeDesktopBridge({ evil: true }) }).getPlatform(),
    ).resolves.toBe("desktop");
  });

  it("is unaffected by later mutation of the exposed object", async () => {
    const bridge = fakeDesktopBridge();
    const resolved = resolvePlatform({ kenDesktop: bridge });
    const replacement = vi.fn(async () => "web");
    (bridge as { getPlatform: unknown }).getPlatform = replacement;
    await expect(resolved.getPlatform()).resolves.toBe("desktop");
    expect(replacement).not.toHaveBeenCalled();
  });

  it.each([
    ["missing", {}],
    ["null", { kenDesktop: null }],
    ["a primitive", { kenDesktop: 42 }],
    ["missing a method", { kenDesktop: { getPlatform: async () => "desktop" } }],
    ["with a non-function method", { kenDesktop: { getPlatform: 1, showNotification: 2 } }],
  ])("falls back to the browser implementation when the bridge is %s", (_label, host) => {
    expect(resolvePlatform(host)).toBe(browserPlatform);
  });

  it("falls back when reading the bridge throws", () => {
    const host = {
      get kenDesktop(): unknown {
        throw new Error("blocked");
      },
    };
    expect(() => resolvePlatform(host)).not.toThrow();
    expect(resolvePlatform(host)).toBe(browserPlatform);
  });

  it("falls back when no host is available", () => {
    expect(resolvePlatform(undefined)).toBe(browserPlatform);
  });
});
