import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

// Repository root. This is an npm workspace, so both file tracing (for the
// standalone server) and Turbopack must resolve from the root, where the single
// package-lock.json and the hoisted node_modules live.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

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
        ],
      },
    ];
  },
};

export default nextConfig;
