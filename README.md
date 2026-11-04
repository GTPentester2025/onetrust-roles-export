# OneTrust Roles Export

Exports every role from a OneTrust tenant, with the organizations each role is assigned in, into one JSON file.

The script runs on your own computer and calls the OneTrust web API over HTTPS. Nothing is installed in OneTrust.

- Uses only the Python standard library, so no `pip install` is needed (Python 3.8+)
- Reads roles from the documented SCIM Groups API (`GET /api/scim/v2/Groups`), following its pagination (25 per page)
- Retries HTTP 429, 500, 502, 503, 504 and connection errors, waiting 1s, 2s, 4s, 8s... (or the `Retry-After` header when OneTrust sends one in seconds)
- Waits 0.5s between page requests to stay under rate limits

**Permissions are not exported.** OneTrust has no public API that returns a role's permission set, so the export contains role names, organizations and SCIM group IDs only.

## How OneTrust exposes roles

In OneTrust, a SCIM group is one role in one organization, named `{Role} - {Organization}`. A tenant with 20 roles and 2 organizations has 40 groups. The script lists all groups, then merges them into one entry per role.

## What you need from OneTrust

1. **Your tenant domain:** the host in your browser's address bar when logged in, for example `app.onetrust.com`, `app-eu.onetrust.com`, `app-de.onetrust.com` or `app-uk.onetrust.com`. Leave out `https://`.
2. **A Client ID and Client Secret with the `SCIM` scope:** a OneTrust admin creates these under **Global Settings → Access Management → Client Credentials → Client Credentials tab**, and selects the **SCIM** scope. The secret is shown only once when it's created, so save it then.

The script exchanges the Client ID and Secret for an access token (OAuth 2.0 client credentials, via `POST /api/access/v1/oauth/token`) and sends it as `Authorization: Bearer <token>` on every API call.

**API keys:** OneTrust also offers API keys (the **API Keys** tab on the same page), but OneTrust's docs say SCIM endpoints are authorized with an OAuth 2.0 access token, so this script uses the Client ID and Secret and doesn't ask for an API key.

## Setup

1. Clone the repo:
   ```bash
   git clone <this-repo-url>
   cd onetrust-roles-export
   ```
2. Open `onetrust_roles_export.py` and fill in the CONFIGURATION block at the top. All settings live in the script; there's no separate config file.
   ```python
   ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"      # host you log in to, without https://
   ONETRUST_CLIENT_ID = "paste-your-client-id-here"    # client credential with the SCIM scope
   ONETRUST_CLIENT_SECRET = "paste-your-client-secret-here"
   ```

   > **Warning:** once your real Client Secret is in the script, don't commit or push it. Anyone who can read the repo or its history could use the secret, and deleting it later doesn't remove it from git history. Keep your filled-in copy local.

## Run

With Python installed:
```bash
python onetrust_roles_export.py
```

With [uv](https://docs.astral.sh/uv/) instead of a Python install:
```bash
uv run --no-project python onetrust_roles_export.py
```

Example output:
```
Requesting access token ...
Fetching roles from https://app-de.onetrust.com/api/scim/v2/Groups ...
  25/40 group(s)
  40/40 group(s)

Saved 20 role(s) from 40 role/organization pair(s) to /path/to/onetrust_roles.json
```

The result is written to `onetrust_roles.json` in the folder you run the command from. It's a JSON array with one object per role, sorted by name:

```json
[
    {
        "role": "Site Admin",
        "organizations": [
            {"organization": "Azure AD", "groupId": "444fd8af-...:6e051cbf-..."},
            {"organization": "Test Org", "groupId": "444fd8af-...:4a3227b7-..."}
        ]
    }
]
```

This file is in `.gitignore`, because it contains your tenant's data.

Role and organization names are split from the group's `{Role} - {Organization}` name, using the organization OneTrust reports in the group's `category` field when there is one. If a group has no category and its organization name itself contains ` - `, the split can be wrong.

## Settings

These constants are at the top of the script:

| Setting | Default | Meaning |
|---|---|---|
| `REQUEST_DELAY` | `0.5` | Seconds between page requests |
| `PAGE_SIZE` | `25` | Groups per page (25 is the API maximum) |
| `TIMEOUT` | `30` | Seconds per HTTP request |
| `MAX_RETRIES` | `5` | Retries on 429 / 5xx / connection errors |
| `BACKOFF_FACTOR` | `1` | Base wait in seconds; doubles each retry |
| `OUTPUT_FILE` | `onetrust_roles.json` | Output file name |

## Troubleshooting

- **`[CONFIG] Fill in ONETRUST_DOMAIN ...`:** one of the three values in the CONFIGURATION block still has its placeholder. Replace all three and save the file.
- **`[AUTH ERROR] ... HTTP 400` or `401`:** the Client ID or Secret is wrong, or the credential was revoked or expired. The response text from OneTrust is printed after the error. Also check the domain is the one you log in to (for example `app-eu.onetrust.com` for EU tenants).
- **Role requests return HTTP 401 or 403:** login worked, but the client credential doesn't have the **SCIM** scope. Add it, or create a new client credential with SCIM selected.
- **`response was not JSON`:** OneTrust answered with a web page instead of API data. Check the domain, and that the request URL in the message is `/api/scim/v2/Groups`. Earlier versions of this script called `/api/v2/useraccesslevels`, which isn't a public API and returns this error.
- **`python` opens the Microsoft Store (Windows):** Python isn't installed. Install it from python.org with "Add python.exe to PATH" ticked, or use the uv command above.
