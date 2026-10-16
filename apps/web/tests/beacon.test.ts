/**
 * The first-party beacon, `services/ken_services/analytics/collector/static/ken-beacon.js`
 * (ADR-0015 §1 as amended by §12.8), under jsdom with stubbed `IntersectionObserver`,
 * `navigator.sendBeacon` and storage.
 *
 * The file under test is the SAME static file the collector serves; only the policy prelude
 * (`window.KEN_BEACON_CONFIG`) is published by the server, so it is set here explicitly.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const BEACON = "services/ken_services/analytics/collector/static/ken-beacon.js";

/** The beacon, found by walking up from the working directory to the repository root. */
function beaconSource(): string {
  let directory = resolve(process.cwd());
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = join(directory, BEACON);
    if (existsSync(candidate)) return readFileSync(candidate, "utf8");
    directory = dirname(directory);
  }
  throw new Error(`${BEACON} was not found above ${process.cwd()}`);
}

const SOURCE = beaconSource();
const ENDPOINT = "http://127.0.0.1:4100/e";
const LINK_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_ID = "22222222-2222-4222-8222-222222222222";
const CONFIG = { flushSeconds: 5, maxEvents: 50, maxHrefLinks: 50, maxPathChars: 512, maxHrefChars: 2048, maxAnchorChars: 120 };

type Payload = {
  v: number;
  pv: string;
  path: string;
  events: Array<{ t: string; l?: string; h?: string; a?: string }>;
};

class FakeObserver {
  static instances: FakeObserver[] = [];
  readonly observed: Element[] = [];
  readonly unobserved: Element[] = [];

  constructor(
    readonly callback: (entries: Array<Record<string, unknown>>) => void,
    readonly options: { threshold?: number },
  ) {
    FakeObserver.instances.push(this);
  }

  observe(element: Element): void {
    this.observed.push(element);
  }

  unobserve(element: Element): void {
    this.unobserved.push(element);
  }

  disconnect(): void {}

  /** Report `elements` as at least half visible. */
  intersect(elements: Element[], ratio = 0.5): void {
    this.callback(elements.map((target) => ({ target, isIntersecting: true, intersectionRatio: ratio })));
  }
}

let sent: Array<{ url: string; blob: Blob }> = [];
let path = "/reports/a";
let pages = 0;

/**
 * Each test runs the beacon in the SAME jsdom document, so earlier runs keep their
 * `visibilitychange` listener and interval alive. Every test therefore gets its own page
 * path and reads back only the payloads sent for that path.
 */
function at(next: string): void {
  path = next;
  window.history.replaceState({}, "", next);
}

function sendBeacon(url: string, blob: Blob): boolean {
  sent.push({ url, blob });
  return true;
}

async function payloads(): Promise<Payload[]> {
  const all = await Promise.all(sent.map(async (one) => JSON.parse(await one.blob.text()) as Payload));
  const own = path.replace(/[?#].*$/, "");
  return all.filter((batch) => batch.path === own);
}

/** The window globals the beacon reads, as a bag (they are not in the DOM typings). */
const globals = window as unknown as Record<string, unknown>;

function define(target: object, property: string, value: unknown): void {
  Object.defineProperty(target, property, { value, configurable: true, writable: true });
}

function scriptTag(attributes: Record<string, string> = {}): HTMLScriptElement {
  const tag = document.createElement("script");
  tag.setAttribute("src", "http://127.0.0.1:4100/k.js");
  tag.setAttribute("data-ken-collector", ENDPOINT);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === "") tag.removeAttribute(name);
    else tag.setAttribute(name, value);
  }
  document.head.append(tag);
  return tag;
}

function link(id: string | null, text = "battery storage", href = "/insights/b"): HTMLAnchorElement {
  const anchor = document.createElement("a");
  anchor.setAttribute("href", href);
  if (id !== null) anchor.setAttribute("data-ken-link-id", id);
  anchor.textContent = text;
  return anchor;
}

function page(...nodes: Element[]): HTMLElement {
  const main = document.createElement("main");
  main.append(...nodes);
  document.body.append(main);
  return main;
}

function run(config: Partial<typeof CONFIG> | null = CONFIG): void {
  if (config === null) delete globals["KEN_BEACON_CONFIG"];
  else globals["KEN_BEACON_CONFIG"] = { ...CONFIG, ...config };
  new Function(SOURCE)();
}

function observer(): FakeObserver {
  expect(FakeObserver.instances).toHaveLength(1);
  return FakeObserver.instances[0]!;
}

/** The single payload sent for this test's page path. */
async function only(): Promise<Payload> {
  const batches = await payloads();
  expect(batches).toHaveLength(1);
  return batches[0]!;
}

beforeEach(() => {
  sent = [];
  FakeObserver.instances = [];
  document.head.innerHTML = "";
  document.body.innerHTML = "";
  pages += 1;
  at(`/reports/${pages}`);
  define(window, "IntersectionObserver", FakeObserver);
  define(window.navigator, "sendBeacon", sendBeacon);
  define(window.navigator, "globalPrivacyControl", undefined);
  define(window.navigator, "doNotTrack", null);
  delete globals["kenAnalyticsConsent"];
  delete globals["dataLayer"];
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

// --------------------------------------------------------------------------- //
// Opt-out: every path makes the beacon a no-op
// --------------------------------------------------------------------------- //
describe("opt-out", () => {
  const cases: Array<[string, () => void]> = [
    ["Global Privacy Control", () => define(window.navigator, "globalPrivacyControl", true)],
    ["Do Not Track", () => define(window.navigator, "doNotTrack", "1")],
    ["a CMP that said no", () => (globals["kenAnalyticsConsent"] = false)],
    ["staff self-exclusion", () => window.localStorage.setItem("ken_internal", "1")],
  ];

  for (const [name, optOut] of cases) {
    it(`is a no-op under ${name}`, () => {
      scriptTag();
      page(link(LINK_ID));
      optOut();
      run();
      expect(FakeObserver.instances).toHaveLength(0);
      expect(sent).toHaveLength(0);
    });
  }

  it("still runs when consent is explicitly granted", () => {
    scriptTag();
    page(link(LINK_ID));
    globals["kenAnalyticsConsent"] = true;
    run();
    expect(FakeObserver.instances).toHaveLength(1);
  });

  it("treats blocked storage as no signal at all", () => {
    scriptTag();
    page(link(LINK_ID));
    const original = window.localStorage.getItem;
    define(window.localStorage, "getItem", () => {
      throw new Error("blocked");
    });
    run();
    define(window.localStorage, "getItem", original);
    expect(FakeObserver.instances).toHaveLength(1);
  });
});

// --------------------------------------------------------------------------- //
// Setup requirements
// --------------------------------------------------------------------------- //
describe("setup", () => {
  it("does nothing without its own script tag", () => {
    page(link(LINK_ID));
    run();
    expect(FakeObserver.instances).toHaveLength(0);
  });

  it("falls back to deriving the endpoint from its own src", async () => {
    scriptTag({ "data-ken-collector": "" });
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    observer().intersect([anchor]);
    vi.useFakeTimers();
    document.dispatchEvent(new Event("visibilitychange"));
    define(document, "visibilityState", "hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(sent.map((one) => one.url)).toEqual([ENDPOINT]);
    await expect(payloads()).resolves.toHaveLength(1);
  });

  it("does nothing without sendBeacon or IntersectionObserver", () => {
    scriptTag();
    page(link(LINK_ID));
    define(window.navigator, "sendBeacon", undefined);
    run();
    expect(FakeObserver.instances).toHaveLength(0);
  });

  it("does nothing when the page has no instrumented link", () => {
    scriptTag();
    page(link(null));
    run();
    expect(FakeObserver.instances).toHaveLength(0);
  });

  it("ignores a link whose id is not a uuid", () => {
    scriptTag();
    page(link("not-a-uuid"), link(LINK_ID));
    run();
    expect(observer().observed).toHaveLength(1);
  });

  it("observes at the 0.5 threshold", () => {
    scriptTag();
    page(link(LINK_ID));
    run();
    expect(observer().options.threshold).toBe(0.5);
  });
});

// --------------------------------------------------------------------------- //
// Impressions
// --------------------------------------------------------------------------- //
describe("impressions", () => {
  it("sends one impression per link per page view and flushes on the policy interval", async () => {
    vi.useFakeTimers();
    scriptTag();
    const first = link(LINK_ID);
    const second = link(OTHER_ID, "grid storage");
    page(first, second);
    run();

    observer().intersect([first]);
    observer().intersect([first]); // the same link again: already counted
    expect(sent).toHaveLength(0); // batched, not sent yet

    vi.advanceTimersByTime(5000);
    const batch = await only();
    expect(batch.v).toBe(1);
    expect(batch.path).toBe(path);
    expect(batch.pv).toMatch(/^[0-9a-f]{32}$/);
    expect(batch.events).toEqual([{ t: "imp", l: LINK_ID }]);
    expect(observer().unobserved).toEqual([first]);

    observer().intersect([second]);
    vi.advanceTimersByTime(5000);
    const all = await payloads();
    expect(all[1]!.events).toEqual([{ t: "imp", l: OTHER_ID }]);
    expect(all[1]!.pv).toBe(batch.pv); // one page view
  });

  it("ignores an element that is less than half visible", () => {
    vi.useFakeTimers();
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    observer().callback([{ target: anchor, isIntersecting: false, intersectionRatio: 0.9 }]);
    observer().callback([{ target: anchor, isIntersecting: true, intersectionRatio: 0.49 }]);
    vi.advanceTimersByTime(5000);
    expect(sent).toHaveLength(0);
  });

  it("flushes when the page is hidden", async () => {
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    observer().intersect([anchor]);
    define(document, "visibilityState", "hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    const batch = await only();
    expect(batch.events).toEqual([{ t: "imp", l: LINK_ID }]);
  });

  it("never sends more events than the policy allows per batch", async () => {
    vi.useFakeTimers();
    scriptTag();
    const anchors = Array.from({ length: 120 }, (_, n) =>
      link(`${n.toString(16).padStart(8, "0")}-1111-4111-8111-111111111111`, `anchor ${n}`),
    );
    page(...anchors);
    run();
    observer().intersect(anchors);
    const batches = await payloads();
    expect(batches.map((batch) => batch.events.length)).toEqual([50, 50]);
    vi.advanceTimersByTime(5000);
    const all = await payloads();
    expect(all.map((batch) => batch.events.length)).toEqual([50, 50, 20]);
    expect(new Set(all.flatMap((batch) => batch.events.map((event) => event.l))).size).toBe(120);
  });
});

// --------------------------------------------------------------------------- //
// Clicks
// --------------------------------------------------------------------------- //
describe("clicks", () => {
  for (const type of ["click", "auxclick"]) {
    it(`sends a ${type} immediately`, async () => {
      scriptTag();
      const anchor = link(LINK_ID);
      page(anchor);
      run();
      anchor.dispatchEvent(new Event(type, { bubbles: true }));
      const batch = await only();
      expect(batch.events).toEqual([{ t: "clk", l: LINK_ID }]);
    });
  }

  it("carries a pending impression out with the click", async () => {
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    observer().intersect([anchor]);
    anchor.dispatchEvent(new Event("click", { bubbles: true }));
    const batch = await only();
    expect(batch.events).toEqual([
      { t: "imp", l: LINK_ID },
      { t: "clk", l: LINK_ID },
    ]);
  });
});

// --------------------------------------------------------------------------- //
// The GA4 dataLayer bridge
// --------------------------------------------------------------------------- //
describe("dataLayer bridge", () => {
  it("pushes both event names and never injects gtag", () => {
    const pushed: Array<Record<string, unknown>> = [];
    globals["dataLayer"] = pushed;
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    observer().intersect([anchor]);
    anchor.dispatchEvent(new Event("click", { bubbles: true }));
    expect(pushed).toEqual([
      { event: "internal_link_impression", ken_link_id: LINK_ID },
      { event: "internal_link_click", ken_link_id: LINK_ID },
    ]);
    expect(document.querySelectorAll("script")).toHaveLength(1); // only our own tag
  });

  it("survives a hostile dataLayer", () => {
    globals["dataLayer"] = {
      push: () => {
        throw new Error("boom");
      },
    };
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    expect(() => observer().intersect([anchor])).not.toThrow();
  });
});

// --------------------------------------------------------------------------- //
// href mode (a CMS that strips data-*)
// --------------------------------------------------------------------------- //
describe("href mode", () => {
  it("sends the RAW href attribute and the anchor text", async () => {
    scriptTag({ "data-ken-match": "href" });
    const anchor = link(null, "  battery   storage\n", "/insights/b?utm=1#frag");
    page(anchor);
    run();
    observer().intersect([anchor]);
    define(document, "visibilityState", "hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    const batch = await only();
    expect(batch.events).toEqual([{ t: "imp", h: "/insights/b?utm=1#frag", a: "battery storage" }]);
  });

  it("only looks inside the content root", () => {
    scriptTag({ "data-ken-match": "href" });
    page(link(null, "inside", "/a"));
    const outside = link(null, "outside", "/b");
    document.body.append(outside);
    run();
    expect(observer().observed).toHaveLength(1);
  });

  it("stops at the policy's link limit", () => {
    scriptTag({ "data-ken-match": "href" });
    page(...Array.from({ length: 80 }, (_, n) => link(null, `anchor ${n}`, `/t/${n}`)));
    run({ maxHrefLinks: 50 });
    expect(observer().observed).toHaveLength(50);
  });

  it("skips a link with no anchor text or an oversize href", () => {
    scriptTag({ "data-ken-match": "href" });
    page(link(null, "", "/a"), link(null, "ok", `/${"x".repeat(2100)}`), link(null, "fine", "/b"));
    run();
    expect(observer().observed).toHaveLength(1);
  });

  it("is also readable from the html element", () => {
    document.documentElement.setAttribute("data-ken-match", "href");
    try {
      scriptTag();
      page(link(null, "anchor", "/a"));
      run();
      expect(observer().observed).toHaveLength(1);
    } finally {
      document.documentElement.removeAttribute("data-ken-match");
    }
  });
});

// --------------------------------------------------------------------------- //
// The page-view id and what is never stored
// --------------------------------------------------------------------------- //
describe("the page-view id", () => {
  it("is 16 random bytes of hex, new per page load and never persisted", async () => {
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);

    run();
    observer().intersect([anchor]);
    define(document, "visibilityState", "hidden");
    document.dispatchEvent(new Event("visibilitychange"));

    FakeObserver.instances = [];
    const second = link(OTHER_ID, "grid storage");
    page(second);
    run();
    observer().intersect([second]);
    document.dispatchEvent(new Event("visibilitychange"));

    const [first, later] = await payloads();
    expect(first!.pv).toMatch(/^[0-9a-f]{32}$/);
    expect(later!.pv).toMatch(/^[0-9a-f]{32}$/);
    expect(later!.pv).not.toBe(first!.pv);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("drops the query string and fragment from the path", async () => {
    at("/reports/with-query?utm=x#top");
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    observer().intersect([anchor]);
    define(document, "visibilityState", "hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    const batch = await only();
    expect(batch.path).toBe("/reports/with-query");
  });

  it("sends a text/plain Blob, so the request stays CORS-simple", async () => {
    scriptTag();
    const anchor = link(LINK_ID);
    page(anchor);
    run();
    anchor.dispatchEvent(new Event("click", { bubbles: true }));
    expect(sent[0]!.url).toBe(ENDPOINT);
    expect(sent[0]!.blob.type).toBe("text/plain");
    expect((await only()).v).toBe(1);
  });

  it("clamps a bad configuration instead of trusting it", async () => {
    vi.useFakeTimers();
    scriptTag();
    const anchors = [link(LINK_ID), link(OTHER_ID, "grid storage")];
    page(...anchors);
    run({ flushSeconds: 9999, maxEvents: 9999 });
    observer().intersect(anchors);
    vi.advanceTimersByTime(60_000); // the clamped maximum
    expect((await only()).events).toHaveLength(2);
  });

  it("works without any configuration at all", () => {
    scriptTag();
    page(link(LINK_ID));
    run(null);
    expect(FakeObserver.instances).toHaveLength(1);
  });
});
