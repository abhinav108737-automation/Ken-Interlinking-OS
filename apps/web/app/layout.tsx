import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Ken Interlink OS",
    template: "%s · Ken Interlink OS",
  },
  description: "Ken Research Agentic Interlinking OS — operator console.",
  robots: { index: false, follow: false },
};

const NAV_LINKS = [
  { href: "/", label: "Home" },
  { href: "/system", label: "System" },
] as const;

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-white font-sans text-slate-900 antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:outline focus:outline-2 focus:outline-blue-700"
        >
          Skip to main content
        </a>
        <header className="border-b border-slate-300 bg-slate-50">
          <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-4 px-4 py-3">
            <p className="text-lg font-semibold">
              <Link href="/" className="focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700">
                Ken Interlink OS
              </Link>
            </p>
            <nav aria-label="Primary">
              <ul className="flex gap-4">
                {NAV_LINKS.map((link) => (
                  <li key={link.href}>
                    <Link
                      href={link.href}
                      className="rounded px-1 text-blue-800 underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700"
                    >
                      {link.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          </div>
        </header>
        <main id="main" tabIndex={-1} className="mx-auto max-w-5xl px-4 py-8 focus:outline-none">
          {children}
        </main>
      </body>
    </html>
  );
}
