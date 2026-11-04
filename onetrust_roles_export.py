"""Export all OneTrust roles, with the organizations each one is assigned in, to JSON.

Roles come from the documented SCIM Groups API, where each group is one "{Role} - {Organization}" pair.
OneTrust has no public API that returns a role's permissions, so permissions are not exported.

Standard library only: no pip install needed (Python 3.8+).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ============================ CONFIGURATION: fill these in ============================
ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"      # host you log in to, without https://
ONETRUST_CLIENT_ID = "paste-your-client-id-here"    # client credential with the SCIM scope
ONETRUST_CLIENT_SECRET = "paste-your-client-secret-here"
# =======================================================================================

REQUEST_DELAY = 0.5   # seconds between page requests
PAGE_SIZE = 25        # groups per page; 25 is the SCIM API maximum
TIMEOUT = 30          # seconds per HTTP request
MAX_RETRIES = 5       # retries on 429 / 5xx / connection errors
BACKOFF_FACTOR = 1    # waits 1s, 2s, 4s, 8s... between retries (Retry-After header wins when sent)
RETRY_STATUSES = {429, 500, 502, 503, 504}
OUTPUT_FILE = "onetrust_roles.json"
ONETRUST_GROUP_EXT = "urn:ietf:params:scim:schemas:onetrust:Group"


DOMAIN = ONETRUST_DOMAIN.replace("https://", "").strip("/")
CLIENT_ID = ONETRUST_CLIENT_ID
CLIENT_SECRET = ONETRUST_CLIENT_SECRET
TOKEN_URL = f"https://{DOMAIN}/api/access/v1/oauth/token"
GROUPS_URL = f"https://{DOMAIN}/api/scim/v2/Groups"
HEADERS = {"Accept": "application/json"}  # Authorization is added once the token is fetched


def get_token():
    """Exchange the client ID/secret for an OAuth bearer token (client_credentials grant). None on failure."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]
    except urllib.error.HTTPError as e:
        print(f"[AUTH ERROR] {TOKEN_URL}: HTTP {e.code} {e.reason}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, OSError) as e:
        print(f"[AUTH ERROR] {TOKEN_URL}: {getattr(e, 'reason', e)}")
    except (ValueError, KeyError):
        print(f"[AUTH ERROR] {TOKEN_URL}: response had no access_token")
    return None


def get_json(url, params=None):
    """GET a URL and return parsed JSON, retrying 429/5xx/connection errors with backoff. None on failure."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(MAX_RETRIES + 1):
        retry_after = None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
                content_type = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES:
                print(f"  [ERROR] {url}: HTTP {e.code} {e.reason}: {e.read().decode('utf-8', 'replace')[:300]}")
                return None
            error = f"HTTP {e.code} {e.reason}"
            retry_after = e.headers.get("Retry-After")
        except (urllib.error.URLError, OSError) as e:  # connection refused, DNS failure, timeout
            error = str(getattr(e, "reason", e))
        else:
            try:
                return json.loads(body)
            except ValueError:
                print(f"  [ERROR] {url}: response was not JSON (Content-Type {content_type}): {body[:200]!r}")
                return None

        if attempt == MAX_RETRIES:
            print(f"  [ERROR] {url}: {error} (gave up after {MAX_RETRIES} retries)")
            return None
        # shortcut: only numeric Retry-After is honored; HTTP-date values fall back to backoff
        delay = float(retry_after) if retry_after and retry_after.isdigit() else BACKOFF_FACTOR * 2 ** attempt
        print(f"  [RETRY] {error}, retrying in {delay:.0f}s ({attempt + 1}/{MAX_RETRIES})")
        time.sleep(delay)


def fetch_groups():
    """Return every SCIM group, following startIndex/count pagination. None if any page fails."""
    groups, start = [], 1
    while True:
        data = get_json(GROUPS_URL, {"startIndex": start, "count": PAGE_SIZE})
        if data is None:
            return None
        page = data.get("Resources", [])
        groups.extend(page)
        start += len(page)
        print(f"  {len(groups)}/{data.get('totalResults', len(groups))} group(s)")
        if not page or start > data.get("totalResults", 0):
            return groups
        time.sleep(REQUEST_DELAY)


def split_display_name(group):
    """Split a group's "{Role} - {Organization}" displayName into (role, organization)."""
    name = group.get("displayName", "")
    org = (group.get(ONETRUST_GROUP_EXT) or {}).get("category")
    # OneTrust reports the organization as the category; matching it keeps " - " inside names intact
    if org and name.endswith(f" - {org}"):
        return name[:-len(org) - 3], org
    # shortcut: without a category, assume the organization name itself has no " - "
    role, sep, org = name.rpartition(" - ")
    return (role, org) if sep else (name, "")


def build_roles(groups):
    """Merge the role/organization groups into one entry per role, sorted by role name."""
    roles = {}
    for group in groups:
        role, org = split_display_name(group)
        roles.setdefault(role, []).append({"organization": org, "groupId": group.get("id")})
    return [{"role": role, "organizations": orgs} for role, orgs in sorted(roles.items(), key=lambda r: r[0].lower())]


def main():
    if "yourtenant" in DOMAIN or CLIENT_ID.startswith("paste-") or CLIENT_SECRET.startswith("paste-"):
        sys.exit("[CONFIG] Fill in ONETRUST_DOMAIN, ONETRUST_CLIENT_ID and ONETRUST_CLIENT_SECRET "
                 "in the CONFIGURATION block at the top of the script.")

    print("Requesting access token ...")
    token = get_token()
    if not token:
        sys.exit("Could not get an access token. Check the domain, client ID and client secret.")
    # shortcut: token is fetched once; if exports ever outlast its lifetime, refresh on 401
    HEADERS["Authorization"] = f"Bearer {token}"

    print(f"Fetching roles from {GROUPS_URL} ...")
    groups = fetch_groups()
    if groups is None:
        sys.exit("Could not list roles. If the error above is HTTP 401 or 403, the client credential needs the "
                 "SCIM scope (Global Settings > Access Management > Client Credentials).")
    if not groups:
        sys.exit("No roles returned.")

    roles = build_roles(groups)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(roles, f, indent=4, ensure_ascii=False)
    print(f"\nSaved {len(roles)} role(s) from {len(groups)} role/organization pair(s) to {os.path.abspath(OUTPUT_FILE)}")


if __name__ == "__main__":
    main()
