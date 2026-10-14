/** The target of a Next.js `redirect()` thrown by `run`, or a failure if it did not redirect. */
export async function redirectTarget(run: () => Promise<unknown>): Promise<string> {
  try {
    await run();
  } catch (error) {
    const digest = (error as { digest?: unknown }).digest;
    if (typeof digest === "string" && digest.startsWith("NEXT_REDIRECT;")) {
      return digest.split(";")[2] ?? "";
    }
    throw error;
  }
  throw new Error("expected a redirect, but the call returned");
}
