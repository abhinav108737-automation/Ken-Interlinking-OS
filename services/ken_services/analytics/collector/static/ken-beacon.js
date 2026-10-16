/*
 * Ken Interlink first-party beacon (docs/adr/ADR-0015-analytics.md, §1 as amended by §12).
 *
 * ES2017, no dependencies, no cookies, no sessionStorage and no landing events (cut by
 * §12.8). The only identifier-like value is `pv`: 16 random bytes, held in memory for ONE
 * page load and never persisted. Opt-out is checked FIRST and makes the script a no-op.
 *
 * It reports two things about the internal links WE deployed: an impression (the link was
 * at least half visible) and a click. Impressions are batched; clicks are sent at once.
 * Every send is `navigator.sendBeacon` with a `text/plain` Blob, which is a CORS-simple
 * request: no preflight, and the collector answers 204 to everything.
 *
 * Bounds come from `window.KEN_BEACON_CONFIG` (published by the collector from the analytics
 * policy) and are clamped here as well.
 */
(function () {
  "use strict";

  var w = window;
  var doc = w.document;
  var nav = w.navigator || {};

  /* ---------------------------------------------------------------- opt-out */
  function optedOut() {
    if (nav.globalPrivacyControl) return true;
    var dnt = nav.doNotTrack || w.doNotTrack || nav.msDoNotTrack;
    if (String(dnt) === "1") return true;
    if (w.kenAnalyticsConsent === false) return true;
    try {
      if (w.localStorage && w.localStorage.getItem("ken_internal") === "1") return true;
    } catch (e) {
      /* storage blocked (private mode, blocked cookies): not an opt-out signal */
    }
    return false;
  }
  if (optedOut()) return;

  /* ------------------------------------------------------- policy and setup */
  var cfg = w.KEN_BEACON_CONFIG || {};
  function bounded(value, fallback, min, max) {
    var n = typeof value === "number" && isFinite(value) ? value : fallback;
    return Math.max(min, Math.min(max, n));
  }
  var FLUSH_MS = bounded(cfg.flushSeconds, 5, 1, 60) * 1000;
  var MAX_EVENTS = bounded(cfg.maxEvents, 50, 1, 50);
  var MAX_HREF_LINKS = bounded(cfg.maxHrefLinks, 50, 1, 50);
  var MAX_PATH = bounded(cfg.maxPathChars, 512, 1, 512);
  var MAX_HREF = bounded(cfg.maxHrefChars, 2048, 1, 2048);
  var MAX_ANCHOR = bounded(cfg.maxAnchorChars, 120, 1, 120);
  var THRESHOLD = 0.5;
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

  /* `document.currentScript` under `defer`, else our own tag by its attribute or its src. */
  var tag =
    doc.currentScript ||
    doc.querySelector("script[data-ken-collector]") ||
    doc.querySelector('script[src*="/k.js"]');
  if (!tag) return;
  var endpoint = tag.getAttribute("data-ken-collector") || "";
  if (!endpoint) {
    var src = tag.getAttribute("src") || "";
    endpoint = /\/k\.js(\?|$)/.test(src) ? src.replace(/\/k\.js(\?.*)?$/, "/e") : "";
  }
  if (!endpoint) return;
  if (!nav.sendBeacon || !w.IntersectionObserver) return;
  var crypto = w.crypto || w.msCrypto;
  if (!crypto || !crypto.getRandomValues) return;

  var path = String((doc.location && doc.location.pathname) || "");
  if (path.charAt(0) !== "/" || path.length > MAX_PATH) return;

  var raw = new Uint8Array(16);
  crypto.getRandomValues(raw);
  var pv = "";
  for (var b = 0; b < raw.length; b++) pv += (raw[b] + 0x100).toString(16).slice(1);

  var mode = (
    tag.getAttribute("data-ken-match") ||
    doc.documentElement.getAttribute("data-ken-match") ||
    "attr"
  ).toLowerCase();
  if (mode !== "href") mode = "attr";

  /* ----------------------------------------------------------------- sending */
  function send(events) {
    if (!events.length) return;
    try {
      var body = JSON.stringify({ v: 1, pv: pv, path: path, events: events });
      nav.sendBeacon(endpoint, new Blob([body], { type: "text/plain" }));
    } catch (e) {
      /* a failed send is never retried and never surfaced */
    }
  }

  var queue = [];
  function flush() {
    while (queue.length) send(queue.splice(0, MAX_EVENTS));
  }
  function push(event) {
    queue.push(event);
    if (queue.length >= MAX_EVENTS) flush();
  }

  /* GA4 bridge: push to an existing dataLayer; never inject gtag. */
  function layer(name, id) {
    if (!id || !w.dataLayer || typeof w.dataLayer.push !== "function") return;
    try {
      w.dataLayer.push({ event: name, ken_link_id: id });
    } catch (e) {
      /* a hostile dataLayer must not break the page */
    }
  }

  /* -------------------------------------------------------------- the links */
  function anchorText(el) {
    return ((el.textContent || "").replace(/\s+/g, " ").trim()).slice(0, MAX_ANCHOR);
  }

  function describe(el) {
    if (mode === "attr") {
      var id = (el.getAttribute("data-ken-link-id") || "").toLowerCase();
      if (!UUID_RE.test(id)) return null;
      return { id: id, event: function (t) { return { t: t, l: id }; } };
    }
    var href = el.getAttribute("href") || ""; /* the RAW attribute (§12.9.7) */
    var text = anchorText(el);
    if (!href || href.length > MAX_HREF || !text) return null;
    return { id: null, event: function (t) { return { t: t, h: href, a: text }; } };
  }

  function contentRoot() {
    var selectors = ["main", "article", "[role=main]", "#content", ".content"];
    for (var i = 0; i < selectors.length; i++) {
      var found = doc.querySelector(selectors[i]);
      if (found) return found;
    }
    return doc.body || doc.documentElement;
  }

  var scope = mode === "attr" ? doc : contentRoot();
  var selector = mode === "attr" ? "a[data-ken-link-id]" : "a[href]";
  var found = scope.querySelectorAll(selector);
  var items = new Map();
  for (var n = 0; n < found.length; n++) {
    if (mode === "href" && items.size >= MAX_HREF_LINKS) break;
    var info = describe(found[n]);
    if (info) items.set(found[n], { info: info, seen: false });
  }
  if (!items.size) return;

  /* --------------------------------------------- impressions, then clicks */
  var observer = new w.IntersectionObserver(
    function (entries) {
      for (var i = 0; i < entries.length; i++) {
        var entry = entries[i];
        if (entry.isIntersecting === false) continue;
        if (!(entry.intersectionRatio >= THRESHOLD)) continue;
        var item = items.get(entry.target);
        if (!item || item.seen) continue;
        item.seen = true; /* once per link per page view */
        observer.unobserve(entry.target);
        push(item.info.event("imp"));
        layer("internal_link_impression", item.info.id);
      }
    },
    { threshold: THRESHOLD }
  );

  items.forEach(function (item, el) {
    observer.observe(el);
    var onClick = function () {
      push(item.info.event("clk"));
      flush(); /* immediately: the page may be unloading */
      layer("internal_link_click", item.info.id);
    };
    el.addEventListener("click", onClick);
    el.addEventListener("auxclick", onClick);
  });

  w.setInterval(flush, FLUSH_MS);
  doc.addEventListener("visibilitychange", function () {
    if (doc.visibilityState === "hidden") flush();
  });
})();
