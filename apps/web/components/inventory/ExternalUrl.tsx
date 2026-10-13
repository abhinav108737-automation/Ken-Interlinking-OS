import { safeExternalHref } from "@/lib/inventory";

export interface ExternalUrlProps {
  /** An untrusted URL from crawled content. */
  readonly url: string;
  /** Render as text even if the URL is http(s). */
  readonly textOnly?: boolean;
}

/**
 * An untrusted URL. Only an absolute http(s) URL becomes a link, and then it
 * opens in a new tab with `noopener noreferrer nofollow`. Anything else
 * (`javascript:`, `data:`, relative, malformed) is plain text: never clickable.
 */
export function ExternalUrl({ url, textOnly = false }: ExternalUrlProps) {
  const href = textOnly ? null : safeExternalHref(url);
  if (href === null) {
    return <span className="break-all font-mono text-xs">{url}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="break-all font-mono text-xs text-blue-800 underline underline-offset-2"
    >
      {url}
      <span className="sr-only"> (external link, opens in a new tab)</span>
    </a>
  );
}
