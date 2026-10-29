# ADR-0022 — How the analytics importers authenticate to Google

- **Status:** Proposed, 2026-10-29. **Nothing here is implemented.**
- **Decision owner:** the project owner.
- **Amends if accepted:** ADR-0015 section 12.3 (the token is read from a file whose mode must
  be exactly 0600).
- **Authority:** CLAUDE.md — a new dependency and a change to how a credential is handled are
  both ASK actions.

## Context: the credential works, our code cannot use it

The owner supplied a Google service-account key on 2026-10-29. It was verified **outside the
project**, with a standalone script that imports none of this codebase:

| Check | Result |
|---|---|
| Key | valid `service_account`, in the owner's Google Cloud project |
| Principal | a read-only service account (identifiers deliberately omitted from this repository) |
| Access token | minted successfully |
| GA4 Data API `runReport` | **HTTP 200**, real page-view data returned |
| Search Console `searchAnalytics` | **HTTP 200**, real click and impression data returned |

The property id, site, account identifiers and the figures themselves are intentionally not
recorded here: they are not needed to understand the decision, and none of it belongs in a
repository that may be shared.

So the Google side is complete and correct: both APIs enabled, both grants in place. Every
remaining obstacle is in this repository.

### Obstacle 1 — the adapter wants a token it has no way to obtain

`GoogleAnalyticsProvider.read_token` reads `KEN_ANALYTICS_GOOGLE_TOKEN_FILE` and sends its
contents as `Authorization: Bearer <token>`. That is an **OAuth2 access token**, not a
service-account key, and the codebase has no step that turns one into the other. The
verification script had to do it itself (sign a JWT with the key's private key, exchange it at
Google's token endpoint).

Access tokens also expire in about an hour. "Write a token into a file" is therefore not a
workable operating procedure for a scheduled import: the file is stale almost immediately.

### Obstacle 2 — the file-mode control cannot be satisfied on Windows

`read_token` refuses unless the file's mode is exactly `0600`. NTFS has no POSIX permission
bits; `os.chmod` only toggles the read-only flag, so the check always fails and the provider
raises `TOKEN_FILE_INVALID` before any request is built.

**That refusal is the control working, and it was deliberately left intact** when the test
suite was made to pass on Windows (the 16 affected tests are skipped with the reason recorded,
not relaxed). The consequence stands: the Google importers cannot run on Windows today.

## Options

**A. Mint tokens in-process from the service-account key.** Add `google-auth` (Apache-2.0,
free, no paid service) and let the provider hold the key and refresh its own token. The token
never touches disk, so the file-mode control becomes irrelevant to it — a credential that is
never written cannot be world-readable.

**B. Keep the token file, mint tokens outside.** A scheduled job writes a fresh token every
hour. No new dependency, but a moving part that fails silently, a secret on disk, and
Obstacle 2 is untouched.

**C. Do nothing.** Analytics stays unavailable. Phase 7 is then decorative: the dashboards
exist but no real measurement reaches them, and nothing can test whether published links
actually helped.

## Proposed decision (Option A)

1. **Add `google-auth`** to `pyproject.toml` and the lock file. Apache-2.0; it is a client
   library, not a service, and introduces no paid dependency.
2. **`KEN_ANALYTICS_GOOGLE_CREDENTIALS_FILE`** points at the service-account JSON key. The
   provider loads it, caches a token in memory and refreshes before expiry. The key is read at
   the start of a fetch, is never logged, never put in an exception, never in `/meta`, and is
   never written anywhere by us — the same rules the token already has.
3. **The file-permission control is kept and made correct per platform.** On POSIX, the
   existing `0600` check applies to the credentials file unchanged. On Windows, check the DACL
   instead: refuse unless the file grants access only to the owner and SYSTEM. **It is not
   skipped on Windows** — an unreadable-by-others guarantee is the point, and expressing it
   through ACLs is the native equivalent, not a loophole.
4. **`KEN_ANALYTICS_GOOGLE_TOKEN_FILE` stays supported** for an operator who prefers to supply
   a token directly, and keeps its current rules. The two are mutually exclusive, and
   configuring both is a startup error rather than a silent precedence rule.
5. **Nothing about egress policy changes.** `KEN_ANALYTICS_GOOGLE_ENABLED` stays `false` by
   default, the environment must still be `local` or `ci`, only the property id and the dates
   are ever sent, redirects are still not followed and the proxy environment is still ignored.

## Consequences

- One dependency, one settings pair, one provider change, a platform-aware permission check,
  and tests for each — including a negative test per platform that a loosely-permissioned
  credentials file is refused.
- Analytics becomes usable on every developer machine, Windows included, which is what makes
  Phase 7 real rather than decorative.
- The key lives outside the repository (`C:\Projects\keys\` on this machine) and must never
  enter it. Worth a `.gitignore` entry for `*.json` under any `keys/` path as a second line of
  defence.

## Risks

- **A key in memory is still a key.** A crash dump or a debugger can expose it. The mitigation
  is the same as for the token: never log it, never serialise it, keep it out of error paths.
- **A service-account key does not expire.** If it leaks it is valid until revoked, whereas a
  leaked access token dies within the hour. The key must be revocable quickly, and whoever
  holds it should know how: delete it in the Google Cloud console under that service account's
  **Keys** tab.
- **Scope creep.** The grant is read-only on analytics and Search Console. It should stay that
  way; nothing in this project needs write access to either.

## Open questions for the owner

1. Accept Option A, with `google-auth` added as a dependency?
2. Confirm the Windows DACL check as the equivalent of `0600` rather than a skip?
3. Should the credentials path be required to sit outside the repository, enforced at startup?
