# Google Analytics and Search Console: setup and safe handling

- **Status:** current as of 2026-10-29, after ADR-0022.
- **Contains no real identifiers.** The key's location, the GA4 property id, the Search Console
  site and the service account's email belong in your local `.env` and your Google console, never
  here and never in git.

## What this gives you

The app reads **click, impression and page-view data** from Google Analytics 4 and Search Console
so it can show which URLs earn traffic and which do not. It only ever **reads**. Only a property
id and a date range are sent to Google, never your page content.

## 1. One-time setup in Google (about 15 minutes)

1. In the **Google Cloud console**, pick (or create) a project. Use the same project for
   everything below.
2. **APIs and services -> Library:** enable **Google Analytics Data API** and **Google Search
   Console API**.
3. **IAM and admin -> Service accounts -> Create service account.** Name it
   `analytics-reader`. Skip the optional role steps; it needs no project role.
4. Copy the account's **email address**. You need it in steps 6 and 7.
5. Open the account -> **Keys -> Add key -> Create new key -> JSON.** A file downloads.
6. **Google Analytics -> Admin -> Property access management -> +** : add the email with the
   role **Viewer**. Untick "notify".
7. **Search Console -> Settings -> Users and permissions -> Add user:** the same email,
   permission **Full** (Restricted cannot read all the data).
8. Note the **GA4 property id** (Admin -> Property details; a plain number, not `G-XXXX`), the
   property's **time zone**, and the exact **Search Console property** (`sc-domain:example.com`
   or a full URL with its trailing slash).

## 2. Where the key must live

**Outside the repository**, for example `C:\Projects\keys\`. The app refuses to start with a key
path inside the repository, and `.gitignore` also excludes `keys/` as a second line of defence.

### Windows: lock the folder down

A folder created at the root of a drive inherits permissions that let **every account on the
machine** read it. Check, then restrict it to your account and the system:

```powershell
icacls "C:\Projects\keys"                      # look for Users / Authenticated Users
icacls "C:\Projects\keys" /inheritance:r /grant:r "$env:USERDOMAIN\$env:USERNAME`:(OI)(CI)F" "NT AUTHORITY\SYSTEM:(OI)(CI)F"
icacls "C:\Projects\keys"                      # now only you and SYSTEM
```

The app checks this **every time it uses the key** and refuses a file that anyone else can read,
including the Administrators group. The refusal carries no detail by design: it would describe
where a secret lives.

### Linux or macOS

```bash
chmod 600 /path/to/key.json
```

## 3. Configure the app

In your **local `.env`** (it is git-ignored; never put these in `.env.example`):

```
KEN_ANALYTICS_GOOGLE_CREDENTIALS_FILE=<path to the key, outside the repo>
KEN_GA4_PROPERTY_ID=<the plain number>
KEN_GSC_SITE_URL=<exactly as shown in Search Console>
KEN_GA4_TIMEZONE=<the property's time zone, for example Asia/Kolkata>
KEN_ANALYTICS_GOOGLE_ENABLED=false
```

Leave `KEN_ANALYTICS_GOOGLE_ENABLED=false`. Calling Google is an approval-gated action, so turn it
on for **one run** and not permanently:

```powershell
$env:KEN_ANALYTICS_GOOGLE_ENABLED = "true"
# ... run the import ...
Remove-Item Env:KEN_ANALYTICS_GOOGLE_ENABLED
```

## 4. Why every row may show as "unresolved"

The importer matches each Google URL to a page the app already knows. If the app's page inventory
is demo data, or from a different site than the property, **no row matches** and every row is
counted as `unresolved_url`. That is the system working, not a fault. Import your real pages first
(a JSONL export from the CMS, or a crawl), then import analytics.

## 5. If the key may have leaked

A service-account key **does not expire**. If it may have been exposed, revoke it immediately:

**Google Cloud console -> IAM and admin -> Service accounts -> the account -> Keys -> delete the
key.** Create a new one, replace the file, and nothing else needs to change.

Never paste the key's contents into chat, email, a ticket or a document. Share its **path** only.

## 6. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `token_file_invalid` | The key file is readable by someone else, is inside the repo, is not a service-account key, or the path is wrong. Fix the permissions first. |
| `google_auth_failed` | The key was revoked or its account deleted, or this machine's clock is badly wrong. |
| `google_unavailable` | Network, or Google returned an error. Retry later. |
| `google_disabled` | `KEN_ANALYTICS_GOOGLE_ENABLED` is false. Expected by default. |
| HTTP 403 inside the data | The account was not added to the property (step 6) or the site (step 7), or the API is not enabled (step 2). |
