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

A second, **read-only** script (it sends only GET requests) for when the roles API refuses API tokens (HTTP 403). It uses your browser's login token instead.

1. In OneTrust, open **Settings → Roles**. In Burp (or the browser's dev tools), find the `GET /api/access/v2/roles/list?...` request and save its **response body** as `roles.json` next to the script. If the list has more roles than one page holds, resend the request with a larger `size` (for example `size=500`) first. The script warns you when the file holds only one page.
2. From any `/api/` request in Burp, copy the value after `Authorization: Bearer `.
3. Fill in the CONFIGURATION block:
   ```python
   ONETRUST_DOMAIN = "uat-de.onetrust.com"
   ONETRUST_TOKEN = "eyJ..."     # value after "Authorization: Bearer "
   ONETRUST_COOKIE = ""          # only if the request has no Authorization header
   EXTRA_HEADERS = {}            # only if requests fail without other headers Burp shows
   ```
4. Run `python onetrust_role_permissions.py` right away, because browser tokens expire.

For each role it calls `GET /api/access/v2/roles/{roleId}`, the request the web app sends when you open a role, and saves `role_permissions.json`: each role from `roles.json` plus a `details` field holding the full response.

- **Token expires partway:** the script saves what it has and stops. Paste a fresh token and run it again, and roles already saved are skipped. Delete `role_permissions.json` to start over.
- **`[NOTE] None of the responses mention permissions`:** the permissions come from a different request. Open a role's permissions in OneTrust, find that request in Burp, and set `ROLE_URL` to its URL, using `{domain}` and `{role_id}` as placeholders.

Your browser token gives the same access as your login. Don't share it or commit it. `roles.json` and `role_permissions.json` are in `.gitignore`.

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
