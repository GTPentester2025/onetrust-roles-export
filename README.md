# OneTrust Roles Export

Exports every role from a OneTrust tenant, with each role's full details (including permissions), into one JSON file.

The script runs on your own computer and calls the OneTrust web API over HTTPS. Nothing is installed in OneTrust.

- Uses only the Python standard library, so no `pip install` is needed (Python 3.8+)
- Works with a OneTrust **API key** or a **Client ID + Client Secret**
- Follows pagination, retries HTTP 429/500/502/503/504 and connection errors with backoff (or the `Retry-After` header), and waits 0.5s between requests
- Saves the roles that succeeded even if some fail, and lists the failures at the end

## Where the roles come from

1. **Roles API (role details and permissions).** The same API the OneTrust web app calls on **Settings → Roles**:
   - `GET /api/access/v2/roles/list?page=0&size=50`: all roles, 50 per page
   - `GET /api/access/v2/roles/{roleId}`: one role's full record

   This API is **not in OneTrust's public docs**. It may refuse API tokens or change without notice.
2. **SCIM fallback (role names only).** If the roles API refuses the token, the script prints `[FALLBACK]` and uses the documented SCIM Groups API (`GET /api/scim/v2/Groups`). Each SCIM group is one `{Role} - {Organization}` pair, merged into one entry per role. This gives role names, organizations and group IDs, **without permissions**. It needs the **SCIM** scope.

## What you need from OneTrust

1. **Your tenant domain:** the host in your browser's address bar when logged in, for example `app.onetrust.com`, `app-eu.onetrust.com`, `app-de.onetrust.com` or `uat-de.onetrust.com`. Leave out `https://`.
2. **One of these,** created under **Global Settings → Access Management → Client Credentials**. Secrets and keys are shown only once, so save them when they're created. UAT and production are separate tenants, so create the key or credential in the tenant you're exporting from.
   - **An API key** (API Keys tab). The script sends it as `Authorization: Bearer <key>`.
   - **A Client ID and Client Secret** (Client Credentials tab). The script exchanges them for an access token via `POST /api/access/v1/oauth/token` and sends `Authorization: Bearer <token>`.

If both are filled in, **only the API key is used**. If the API key doesn't work, set it back to its placeholder so the Client ID and Secret are used.

## Setup

1. Clone the repo:
   ```bash
   git clone <this-repo-url>
   cd onetrust-roles-export
   ```
2. Open `onetrust_roles_export.py` and fill in the CONFIGURATION block at the top. All settings live in the script; there's no separate config file.
   ```python
   ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"      # host you log in to, without https://
   # Fill in EITHER the API key OR the client ID + secret (the API key wins if both are set).
   ONETRUST_API_KEY = "paste-your-api-key-here"
   ONETRUST_CLIENT_ID = "paste-your-client-id-here"
   ONETRUST_CLIENT_SECRET = "paste-your-client-secret-here"
   ```
   Leave the method you don't use as its `paste-...` placeholder.

   > **Warning:** once your real API key or Client Secret is in the script, don't commit or push it. Anyone who can read the repo or its history could use it, and deleting it later doesn't remove it from git history. Keep your filled-in copy local.

## Run

With Python installed:
```bash
python onetrust_roles_export.py
```

With [uv](https://docs.astral.sh/uv/) instead of a Python install:
```bash
uv run --no-project python onetrust_roles_export.py
```

Example output when the roles API works:
```
Requesting access token ...
Fetching roles from https://uat-de.onetrust.com/api/access/v2/roles/list ...
Found 3 role(s).

[1/3] Fetching Privacy Admin (a1b2...) ...
[2/3] Fetching Viewer (c3d4...) ...
  [RETRY] HTTP 429 Too Many Requests, retrying in 1s (1/5)
[3/3] Fetching Auditor (e5f6...) ...

Saved 3/3 role(s) with their details to /path/to/onetrust_roles.json
```

The result is written to `onetrust_roles.json` in the folder you run the command from:
- **Roles API:** a JSON array with each role's record exactly as OneTrust returns it.
- **SCIM fallback:** a JSON array with one `{"role": ..., "organizations": [{"organization": ..., "groupId": ...}]}` entry per role.

This file is in `.gitignore`, because it contains your tenant's data.

## Role permissions from a saved roles list (`onetrust_role_permissions.py`)

A second, **read-only** script. Every data request is a GET. The only other request is the login POST to `/api/access/v1/oauth/token`, which is sent only when you use a Client ID + Secret and just gets a token.

It accepts **one** of these logins. If several are filled in, the first in this list is used:
- **Browser token + its cookie:** the value after `Authorization: Bearer ` and the whole `Cookie` header value, both from the **same** request in Burp. This is the one known to work, because the roles API has refused API keys and client credentials with HTTP 403. OneTrust ties the token to the `__Secure-fgpt` cookie and expires it after **5 minutes**, so copy both from a fresh request and run the script right away. The browser fetches new tokens all the time, which is why every request in Burp shows a different one.
- **API key:** from the API Keys tab.
- **Client ID + Client Secret:** from the Client Credentials tab.
- **Cookie only:** if Burp's requests have no `Authorization` header.

1. In OneTrust, open **Settings → Roles**. In Burp (or the browser's dev tools), find the `GET /api/access/v2/roles/list?...` request and save its **response body** as `roles.json` next to the script. If the list has more roles than one page holds, resend the request with a larger `size` (for example `size=500`) first. The script warns you when the file holds only one page.
2. Fill in the CONFIGURATION block. Leave the logins you don't use as their `paste-...` placeholders:
   ```python
   ONETRUST_DOMAIN = "app-de.onetrust.com"           # the Host header of the Burp request
   ONETRUST_TOKEN = "paste-your-token-here"          # browser token: value after "Authorization: Bearer " in Burp
   ONETRUST_API_KEY = "paste-your-api-key-here"      # API key (Client Credentials > API Keys tab)
   ONETRUST_CLIENT_ID = "paste-your-client-id-here"  # client ID + secret (Client Credentials tab)
   ONETRUST_CLIENT_SECRET = "paste-your-client-secret-here"
   ONETRUST_COOKIE = ""      # with the browser token: the Cookie header value from the SAME request
   EXTRA_HEADERS = {}        # only if requests fail without other headers Burp shows
   ```
   `ONETRUST_DOMAIN` must be the host the token came from. A token from `app-de` won't work on `uat-de`.
3. Run `python onetrust_role_permissions.py` within 5 minutes of copying a browser token.

If the API key or the Client ID + Secret gets HTTP 401 or 403, the script stops after the first request and tells you to use the browser token.

For each role it calls `GET /api/access/v2/roles/{roleId}/accessible-permissions`, the request the web app sends when you open a role's simple view, and saves `role_permissions.json`: each role from `roles.json` plus a `permissions` field holding the full response.

- **Token time left:** with a browser token, the script prints how long the token is still valid when it starts.
- **Token expires partway:** the script saves what it has, then asks for a fresh token and cookie. In Burp, copy both from a new request and paste each at its prompt. Nothing shows while you paste, which is expected. It retries the same role and carries on. Press Enter on an empty prompt to stop instead. If it isn't run from a terminal, it stops. Run it again and the roles already saved are skipped. Delete `role_permissions.json` to start over.
- **`[NOTE] Every response was empty`:** the permissions come from a different request. Open a role's permissions in OneTrust, find that request in Burp, and set `PERMISSIONS_URL` to its URL, using `{domain}` and `{role_id}` as placeholders.

Your browser token and cookie give the same access as your login. Don't share them, paste them into chats, or commit them. `roles.json` and `role_permissions.json` are in `.gitignore`.

## Browser-driven export, no token copying (`onetrust_browser_export.py`)

A third script that removes the copy-a-token-every-5-minutes problem entirely. It opens a real browser, **you log in once** (SSO/MFA and all), and from then on it reads the live session's own bearer token straight from the web app's traffic and makes the API calls through the browser's own context — so the `__Secure-fgpt` cookie and every other session cookie are sent automatically. When a token is about to expire or a request is refused, it quietly reloads the app so OneTrust mints a fresh one and carries on from the same role. If your SSO session itself expires mid-run, it pauses and waits for you to log in again in the window, then resumes.

This is the script to use if the API refuses API keys/client credentials (HTTP 403) and the manual browser-token flow is too fiddly across many roles.

**Read-only:** every data request is a GET; it changes nothing in OneTrust.

### Why SSO works here

You log in inside the browser window exactly as you normally would — the IdP redirect, MFA, "remember this device" all happen in that window and you drive them. The script never needs your password and never automates the login; it only *reuses* the session afterwards. By default it drives your **installed Google Chrome** (so a managed/Conditional-Access browser policy is more likely to accept it) and falls back to bundled Chromium if Chrome isn't present.

### Everything stays local

At run time the **only** network destination is your own OneTrust tenant over HTTPS — no analytics, no telemetry, no third-party calls (Chromium is launched with background networking disabled). The one-time `playwright install chromium` download (only needed if Chrome isn't installed) happens at setup, carries none of your data, and never recurs. Output and the login profile are written next to the script and are git-ignored.

### Setup

One-time, needs Playwright (not stdlib-only like the other two scripts):
```bash
pip install playwright
playwright install chromium     # only needed if Google Chrome isn't already installed
```

Then set your tenant host at the top of the script:
```python
ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"   # e.g. app-de.onetrust.com (no https://)
```
No credentials go in the script — you log in in the browser.

### Run

```bash
python onetrust_browser_export.py
```
A browser window opens on your tenant. Log in (the first run only, unless your IdP session expires; the login is remembered in a local `.onetrust_profile/` folder). The script then lists every role and, for each, fetches its full detail record and its accessible-permissions, merged into one record per role, saving to `onetrust_roles_full.json`. It saves after every role, so an interruption keeps progress and a rerun skips roles already saved.

If a request keeps getting HTTP 403 even with the live session, the API may expect an extra header the web app sends. Find that header in dev tools / Burp and add it to `EXTRA_HEADERS` at the top of the script.

The pure logic (token parsing, roles-list handling, record merging) has a standalone test that needs no browser:
```bash
python test_onetrust_browser_export.py
```

## Discover a module's real API endpoints (`onetrust_discover.py`)

For exporting things the roles scripts don't cover — such as **PIA/DPIA assessment templates** (the Templates, Archive and Notifications sub-tabs) — this finds the real endpoints the way this repo always has: by watching the web app, not by guessing URLs.

It is **fully passive and safe to run on production**: it launches the browser (reusing your saved login) and only *listens* to the API calls the OneTrust web app makes as you click around. It sends **no requests of its own**, so it cannot create, change or delete anything.

```bash
python onetrust_discover.py        # set ONETRUST_DOMAIN at the top first
```

Open the module you want (e.g. PIA → assessment templates), click through its tabs, open a record or two, then **close the browser window**. It writes to `discovery/` (git-ignored):

- `discovery/endpoints.json` — the de-duplicated map of every endpoint seen (method, path, params, status)
- `discovery/responses/*.json` — the JSON bodies the app loaded, with a `_manifest.json`

**Masking:** the live `Authorization` and `Cookie` headers are never recorded, and everything written or printed is run through a masker that scrubs bearer tokens, cookies and secret-shaped values. Captured response bodies keep your real data but have any credential-shaped substrings masked.

Once you have `discovery/endpoints.json`, it tells you exactly which endpoints to build a read-only exporter against.

## Export PIA/DPIA assessment templates (`onetrust_assessment_export.py`)

Exports every PIA/DPIA assessment template with its full definition, questionnaire content, actions, translations and notifications. **Read-only** (every request is a GET) and browser-driven: it reuses the logged-in session from `onetrust_browser_export.py` (same local `.onetrust_profile`, same token capture and auto-refresh), so no token pasting. The endpoints were found with `onetrust_discover.py`, not guessed:

```
GET /api/template/v1/settings
GET /api/template/v1/templates/details?type=PIA                         (the Templates/Archive list; PIA covers DPIA)
  then per template {id}:
GET /api/template/v1/templates/{id}/details                             (full definition)
GET /api/template/v1/templates/rootversion/{rootVersionId}?version={n}  (questionnaire content)
GET /api/template/v1/templates/{id}/actions                             (logic/actions)
GET /api/template/v1/templates/{id}/translations                        (translations)
GET /api/template/v1/custom-notifications/templates/{id}?...            (Notifications tab)
```

```bash
pip install cryptography                     # for the encrypted output (one-time)
python onetrust_assessment_export.py         # set ONETRUST_DOMAIN at the top first
```

**Encrypted at rest (default).** It asks for a passphrase and writes a single encrypted file, `assessment_templates_export.enc` — the template data is never on disk in plaintext. Encryption uses the `cryptography` library's Fernet (AES‑128‑CBC + HMAC) with a PBKDF2‑derived key; a random salt is stored per file. One record per template (plus module settings) is saved after each template, so a rerun resumes (it re‑reads the encrypted file with your passphrase). Keep the passphrase safe — without it the file can't be read.

Read it back on demand:
```bash
python onetrust_decrypt.py                   # prompts for the passphrase
```
This writes the readable `assessment_templates_full.json`, one file per template under `assessment_templates/`, and `assessment_templates_settings.json`. Those decrypted files are plaintext and git‑ignored — delete them when done.

To skip encryption and write plaintext directly, set `ENCRYPT_OUTPUT = False` at the top of the exporter.

**Diagnostics are privacy‑safe:** if the list looks wrong, the script writes `assessment_templates_list_structure.json` — field names, types and counts only, with **no values, IDs, emails or GUIDs** — which is safe to share. Raw response bodies are never written unless you set `SAVE_RAW = True`.

- **Archived templates:** the Templates and Archive tabs call the same list endpoint, so archived rows should come back in the list. If they don't appear in the output, add an archive filter to `LIST_QUERIES` at the top (templates are de-duplicated by id across queries).
- **Questionnaire content:** needs each template's `rootVersionId` and `version`, read from the list/details response. If a template reports no content, the script says so — send one `/details` response and the field names can be mapped.

## Sanitize data before sharing it (`onetrust_sanitize.py`)

When you need to share a response or an export with someone (for debugging), run it through the sanitizer first. This is **not** the at-rest encryption above — it produces a masked copy with every sensitive **value** removed, while keeping enough structure to be useful. Local and read-only: it only transforms JSON/text you already have.

```bash
python onetrust_sanitize.py response.json                 # -> response.sanitized.json
python onetrust_sanitize.py discovery/responses/          # sanitize every *.json in a folder
python onetrust_sanitize.py assessment_templates_export.enc   # decrypts in memory, then sanitizes
python onetrust_sanitize.py - < in.json > out.json        # stdin -> stdout
python onetrust_sanitize.py --keep-text response.json     # keep free text; mask only ids/secrets
```

What it does to each value:

| Value | Becomes |
|---|---|
| GUIDs/UUIDs | `<GUID_1>`, `<GUID_2>` … — **consistent**, so the same id is the same token everywhere (relationships stay visible) |
| Emails | `<EMAIL_1>` … (same scheme) |
| Bearer tokens / JWTs / API keys / secrets | `<secret>` |
| Free text (names, descriptions, questions) | `<str:N>` — length only (default; use `--keep-text` to keep it) |
| Dates/timestamps | `<date>` |
| **Kept as-is** | JSON keys, booleans, numbers, `null`, enum tokens (`PIA`, `ACTIVE`, `IN_PROGRESS`), locales (`en-US`), short digit strings (versions/counts) |

The token↔value mapping is held only in memory and is **never written out**, so a `*.sanitized.json` file can't be reversed. (ALL_CAPS tokens are kept because they're almost always status/type codes; if you want *zero* values, use the structure-only dump from the assessment exporter instead.) Sanitized files are git-ignored.

## Split the export into one file per role (`split_roles.py`)

After an export, this splits `onetrust_roles_full.json` into a subfolder with one JSON file per role. Standard library only, and it never touches the network — it just reads one local file and writes local files.

```bash
python split_roles.py                      # roles/  from onetrust_roles_full.json
python split_roles.py INPUT.json           # custom input, into roles/
python split_roles.py INPUT.json OUT_DIR   # custom input and output folder
```

Each file is named after the role's name (illegal filename characters replaced, Windows reserved names escaped), with the role's id appended only when two roles share a name or a name is empty. Each file holds that role's full record unchanged. The `roles/` folder is in `.gitignore` because it contains your tenant's data.

## Settings

These constants are at the top of `onetrust_roles_export.py`:

| Setting | Default | Meaning |
|---|---|---|
| `REQUEST_DELAY` | `0.5` | Seconds between requests |
| `ROLES_PAGE_SIZE` | `50` | Roles per page when listing |
| `SCIM_PAGE_SIZE` | `25` | Groups per page in the SCIM fallback (25 is the API maximum) |
| `TIMEOUT` | `30` | Seconds per HTTP request |
| `MAX_RETRIES` | `5` | Retries on 429 / 5xx / connection errors |
| `BACKOFF_FACTOR` | `1` | Base wait in seconds; doubles each retry |
| `OUTPUT_FILE` | `onetrust_roles.json` | Output file name |

## Troubleshooting

- **`[CONFIG] Fill in ONETRUST_DOMAIN ...`:** the domain, or both the API key and the Client ID/Secret, still have their placeholders.
- **`[AUTH ERROR] ... HTTP 400` or `401`:** the Client ID or Secret is wrong, revoked, or from a different tenant (for example, made in production but used on UAT).
- **`[FALLBACK]` and you get role names only:** the roles API refused your token. The error above `[FALLBACK]` shows why. Try the other auth method (API key vs Client ID/Secret).
- **HTTP 401 or 403 on both the roles API and SCIM:** the key or credential is wrong, from another tenant, or missing the scopes it needs. Add the **SCIM** scope at least.
- **`response was not JSON`:** OneTrust answered with a web page instead of API data. Check the domain.
- **`python` opens the Microsoft Store (Windows):** Python isn't installed. Install it from python.org with "Add python.exe to PATH" ticked, or use the uv command above.
