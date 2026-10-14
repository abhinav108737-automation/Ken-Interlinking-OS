/**
 * A stand-in for the Next.js request scope (`next/headers`), installed for
 * every test file by tests/setup.ts. Tests set request cookies/headers and
 * inspect the cookies the code wrote, including their attributes.
 */
export interface CookieWrite {
  readonly name: string;
  readonly value: string;
  readonly options: Readonly<Record<string, unknown>>;
}

export const requestContext = {
  cookies: new Map<string, string>(),
  headers: new Map<string, string>(),
  writes: [] as CookieWrite[],
};

export function resetRequestContext(): void {
  requestContext.cookies.clear();
  requestContext.headers.clear();
  requestContext.writes.length = 0;
}

function cookieStore() {
  return {
    get: (name: string) => {
      const value = requestContext.cookies.get(name);
      return value === undefined ? undefined : { name, value };
    },
    has: (name: string) => requestContext.cookies.has(name),
    getAll: () => [...requestContext.cookies].map(([name, value]) => ({ name, value })),
    set: (name: string, value: string, options: Readonly<Record<string, unknown>> = {}) => {
      requestContext.writes.push({ name, value, options });
      if (options["maxAge"] === 0) requestContext.cookies.delete(name);
      else requestContext.cookies.set(name, value);
    },
    delete: (name: string) => {
      requestContext.writes.push({ name, value: "", options: { maxAge: 0 } });
      requestContext.cookies.delete(name);
    },
  };
}

export const nextHeadersModule = {
  cookies: async () => cookieStore(),
  headers: async () => new Headers([...requestContext.headers]),
};
