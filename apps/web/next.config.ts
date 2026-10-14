import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

// Repository root. This is an npm workspace, so both file tracing (for the
// standalone server) and Turbopack must resolve from the root, where the single
// package-lock.json and the hoisted node_modules live.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

// Content-Security-Policy (ADR-0013 section 7). Same-origin only: the browser
// talks to this Next.js server and nothing else (the API is reached
// server-side). Next.js streams inline bootstrap scripts, so without per-request
// nonces `script-src` needs 'unsafe-inline'; XSS defence rests on text-only
// rendering (no dangerouslySetInnerHTML) plus this policy blocking every
// foreign origin, plugins, framing, <base> hijacking and off-site form posts.
// 'unsafe-eval' only in `next dev` (React's dev tooling). No
// upgrade-insecure-requests: local development is plain HTTP on 127.0.0.1.
const isDev = process.env.NODE_ENV === "development";
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  outputFileTracingRoot: repoRoot,
  turbopack: { root: repoRoot },
  // The console uses no next/image. Disabling the optimiser lets the standalone
  // server omit `sharp`, whose prebuilt libvips binaries are LGPL-3.0-or-later,
  // so they never ship in the runtime image. Revisit (with a licence review)
  // only if image optimisation is ever needed.
  images: { unoptimized: true },
  outputFileTracingExcludes: {
    // Globs resolve from apps/web; npm workspaces hoist node_modules to the root.
    "*": ["../../node_modules/sharp/**", "../../node_modules/@img/**"],
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
        ],
      },
    ];
  },
};

export default nextConfig;
