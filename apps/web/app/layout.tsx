import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { logoutAction } from "@/app/auth/actions";
import { PrimaryNav } from "@/components/PrimaryNav";
import { SessionHeader } from "@/components/auth/SessionHeader";
import { getSessionUser, isUnauthenticatedError } from "@/lib/api";
import { permissionsOf } from "@/lib/auth";
import { navLinksFor } from "@/lib/nav";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Ken Interlink OS",
    template: "%s · Ken Interlink OS",
  },
  description: "Ken Research Agentic Interlinking OS — operator console.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  // Never redirects: the sign-in screen renders inside this layout. Pages that
  // need a session redirect on their own API 401s (lib/api.ts).
  const me = await getSessionUser();
  const user = me.ok ? me.data : null;
  // Signed out: no nav. API unreachable: the nav still shows (System status helps then).
  const signedOut = !me.ok && isUnauthenticatedError(me.error);
  const links = navLinksFor(permissionsOf(user));
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
          <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-3">
            <p className="text-lg font-semibold">
              <Link href="/" className="focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700">
                Ken Interlink OS
              </Link>
            </p>
            <SessionHeader user={user} logoutAction={logoutAction} />
          </div>
          {!signedOut && (
            <div className="mx-auto max-w-6xl px-4 pb-3">
              <PrimaryNav links={links} />
            </div>
          )}
        </header>
        <main id="main" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-8 focus:outline-none">
          {children}
        </main>
      </body>
    </html>
  );
}
