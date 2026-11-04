"""Fetch the permissions of every OneTrust role listed in roles.json.

roles.json is the response of GET /api/access/v2/roles/list, saved from Burp next to this script.
For each role, the script calls GET /api/access/v2/roles/{roleId}/accessible-permissions, the request
the OneTrust web app sends when you open a role (Settings > Roles > role > simple view), and saves
the responses to role_permissions.json.

Read-only: every data request is a GET. It never creates, changes or deletes anything in OneTrust.
The only non-GET request is the login POST to /api/access/v1/oauth/token, sent only when you use a
client ID + secret; it just exchanges them for a token.

Authenticate with ONE of: the browser token from Burp, an API key, or a client ID + secret. This API
isn't in OneTrust's public docs and has refused API keys and client credentials with HTTP 403, so the
browser token is the one known to work. OneTrust ties that token to the __Secure-fgpt cookie and
expires it after 5 minutes, so copy the Authorization token AND the Cookie from the same fresh request
in Burp, then run the script right away. When the token expires partway, the script saves what it has
and asks you to paste a fresh token and cookie, then carries on from the same role. (If it can't ask,
for example when not run from a terminal, it stops instead; run it again and saved roles are skipped.)

Standard library only: no pip install needed (Python 3.8+).
"""
import base64
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ============================ CONFIGURATION: fill these in ============================
ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"   # Host header of the Burp request, e.g. app-de.onetrust.com
# Fill in ONE way to log in. If several are filled in, the first one in this list is used.
ONETRUST_TOKEN = "paste-your-token-here"          # browser token: value after "Authorization: Bearer " in Burp
ONETRUST_API_KEY = "paste-your-api-key-here"      # API key (Client Credentials > API Keys tab)
ONETRUST_CLIENT_ID = "paste-your-client-id-here"  # client ID + secret (Client Credentials tab)
ONETRUST_CLIENT_SECRET = "paste-your-client-secret-here"
ONETRUST_COOKIE = ""      # with the browser token: the Cookie header value from the SAME request (has __Secure-fgpt)
EXTRA_HEADERS = {}        # optional: other headers copied from Burp's request, e.g. {"Header-Name": "value"}
# =======================================================================================

ROLES_FILE = "roles.json"               # roles list saved from Burp, next to this script
OUTPUT_FILE = "role_permissions.json"   # written next to this script
PERMISSIONS_URL = "https://{domain}/api/access/v2/roles/{role_id}/accessible-permissions"
TOKEN_URL = "https://{domain}/api/access/v1/oauth/token"      # used only with a client ID + secret
REQUEST_DELAY = 0.5   # seconds between role requests
TIMEOUT = 30          # seconds per HTTP request
MAX_RETRIES = 5       # retries on 429 / 5xx / connection errors
BACKOFF_FACTOR = 1    # waits 1s, 2s, 4s, 8s... between retries (Retry-After header wins when sent)
RETRY_STATUSES = {429, 500, 502, 503, 504}
AUTH_STATUSES = {401, 403}   # token expired or not allowed: stop instead of failing every role


HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = ONETRUST_DOMAIN.replace("https://", "").strip("/")
TOKEN = ONETRUST_TOKEN
API_KEY = ONETRUST_API_KEY
CLIENT_ID = ONETRUST_CLIENT_ID
CLIENT_SECRET = ONETRUST_CLIENT_SECRET
COOKIE = ONETRUST_COOKIE


def is_filled(value):
    return bool(value) and not value.startswith("paste-")


def strip_prefix(value, prefix):
    """Accept a value pasted with or without its header prefix, e.g. "Bearer " or "Cookie: "."""
    value = value.strip()
    return value[len(prefix):].strip() if value.lower().startswith(prefix.lower()) else value


def get_token():
    """Exchange the client ID/secret for an OAuth bearer token (client_credentials grant). None on failure.
    This login POST is the script's only non-GET request; it creates or changes nothing."""
    url = TOKEN_URL.format(domain=DOMAIN)
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]
    except urllib.error.HTTPError as e:
        print(f"[AUTH ERROR] {url}: HTTP {e.code} {e.reason}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, OSError) as e:
        print(f"[AUTH ERROR] {url}: {getattr(e, 'reason', e)}")
    except (ValueError, KeyError):
        print(f"[AUTH ERROR] {url}: response had no access_token")
    return None


def choose_login():
    """Return (login method, bearer token or None for cookie only) from the first credential filled in."""
    if is_filled(TOKEN):
        return "browser token", strip_prefix(TOKEN, "Bearer ")
    if is_filled(API_KEY):
        return "API key", strip_prefix(API_KEY, "Bearer ")  # a OneTrust API key is itself the bearer token
    if is_filled(CLIENT_ID) and is_filled(CLIENT_SECRET):
        print("Requesting access token with the client ID + secret ...")
        token = get_token()
        if not token:
            sys.exit("Could not get an access token. Check the domain, client ID and client secret.")
        return "client ID + secret", token
    return "cookie", None


def token_seconds_left(token):
    """Seconds until a JWT's "exp" claim (negative if expired), or None if the token isn't a readable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"] - time.time())
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def describe_expiry(token):
    left = token_seconds_left(token)
    if left is None:
        return ""
    if left <= 0:
        return f"[WARNING] This token expired {-left}s ago: copy a fresh one from Burp."
    return f"Token valid for {left // 60}m {left % 60:02d}s."


def can_prompt():
    return sys.stdin.isatty()


def ask_for_fresh_login(status):
    """After the browser token is rejected, ask for a fresh token + cookie instead of stopping.
    Returns the new bearer token, or None if the user pressed Enter or the script can't ask."""
    global COOKIE
    if not can_prompt():
        return None
    print(f"\nLogin rejected (HTTP {status}): the browser token has probably expired. Progress is saved.")
    print("Copy the token and the Cookie from a fresh request in Burp and paste them here "
          "(input is hidden). Press Enter on an empty line to stop.")
    token = strip_prefix(getpass.getpass("  Authorization token: "), "Bearer ")
    if not token:
        return None
    cookie = strip_prefix(getpass.getpass("  Cookie: "), "Cookie:")
    COOKIE = cookie or COOKIE
    print(f"  {describe_expiry(token) or 'Got it.'} Continuing ...\n")
    return token


def build_headers(bearer):
    headers = {"Accept": "application/json", **EXTRA_HEADERS}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if COOKIE:
        headers["Cookie"] = strip_prefix(COOKIE, "Cookie:")
    return headers


def load_roles(path):
    """Read the roles list saved from Burp: a page object with "content", or a bare array. Exits on problems."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        sys.exit(f"[INPUT] {path} not found. Save the /api/access/v2/roles/list response body there.")
    except UnicodeDecodeError:
        sys.exit(f"[INPUT] {path} is not UTF-8 text. Save it again as UTF-8.")
    if text.startswith("HTTP/"):  # saved with the HTTP response headers: keep only the body
        body_start = re.search(r"^\s*[\[{]", text, re.MULTILINE)
        text = text[body_start.start():] if body_start else text
    try:
        data = json.loads(text)
    except ValueError as e:
        sys.exit(f"[INPUT] {path} is not valid JSON ({e}). Save the complete response body from Burp.")
    roles = data.get("content") if isinstance(data, dict) else data
    if not isinstance(roles, list):
        sys.exit(f'[INPUT] {path} has no list of roles (expected a "content" array).')
    total = data.get("totalElements") if isinstance(data, dict) else None
    if isinstance(total, int) and total > len(roles):
        print(f"[WARNING] {path} has {len(roles)} of {total} roles (one page only). To get all of them, resend "
              f"the list request in Burp with size={total} and save that response instead.\n")
    return roles


def load_previous(path):
    """Return {role id: record} from an earlier run's output, so those roles are skipped."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            # records without "permissions" come from an older version of this script: fetch them again
            return {r["id"]: r for r in json.load(f) if isinstance(r, dict) and r.get("id") and "permissions" in r}
    except (ValueError, TypeError):
        print(f"[WARNING] {path} could not be read; starting over.\n")
        return {}


def get_json(url, headers):
    """GET a URL. Returns (HTTP status or None, parsed JSON or None), retrying 429/5xx/connection errors.
    Every data request goes through here, and it is always a GET."""
    for attempt in range(MAX_RETRIES + 1):
        retry_after = None
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status, body = resp.status, resp.read().decode("utf-8", "replace")
                content_type = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES:
                print(f"  [ERROR] HTTP {e.code} {e.reason}: {e.read().decode('utf-8', 'replace')[:300]}")
                return e.code, None
            error = f"HTTP {e.code} {e.reason}"
            retry_after = e.headers.get("Retry-After")
        except (urllib.error.URLError, OSError) as e:  # connection refused, DNS failure, timeout
            error = str(getattr(e, "reason", e))
        else:
            try:
                return status, json.loads(body)
            except ValueError:
                print(f"  [ERROR] response was not JSON (Content-Type {content_type}): {body[:200]!r}")
                return status, None

        if attempt == MAX_RETRIES:
            print(f"  [ERROR] {error} (gave up after {MAX_RETRIES} retries)")
            return None, None
        # shortcut: only numeric Retry-After is honored; HTTP-date values fall back to backoff
        delay = float(retry_after) if retry_after and retry_after.isdigit() else BACKOFF_FACTOR * 2 ** attempt
        print(f"  [RETRY] {error}, retrying in {delay:.0f}s ({attempt + 1}/{MAX_RETRIES})")
        time.sleep(delay)


def save(path, roles, results):
    """Write the results in roles.json order."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump([results[r["id"]] for r in roles if r.get("id") in results], f, indent=4, ensure_ascii=False)


def main():
    has_login = (is_filled(TOKEN) or is_filled(API_KEY) or (is_filled(CLIENT_ID) and is_filled(CLIENT_SECRET))
                 or COOKIE)
    if "yourtenant" in DOMAIN or not has_login:
        sys.exit("[CONFIG] Fill in ONETRUST_DOMAIN and one way to log in (ONETRUST_TOKEN, ONETRUST_API_KEY, "
                 "ONETRUST_CLIENT_ID + ONETRUST_CLIENT_SECRET, or ONETRUST_COOKIE) at the top of the script.")
    if is_filled(TOKEN) and "__Secure-fgpt" not in COOKIE:
        print("[WARNING] ONETRUST_TOKEN is set but ONETRUST_COOKIE has no __Secure-fgpt cookie. OneTrust rejects "
              "the browser token without it: copy the Cookie header from the same Burp request.\n")

    roles = load_roles(os.path.join(HERE, ROLES_FILE))
    out_path = os.path.join(HERE, OUTPUT_FILE)
    results = load_previous(out_path)
    login, bearer = choose_login()
    headers = build_headers(bearer)
    todo = [r for r in roles if r.get("id") not in results]
    print(f"Logging in with: {login}")
    if login == "browser token" and describe_expiry(bearer):
        print(describe_expiry(bearer))
    print(f"{len(roles)} role(s) in {ROLES_FILE}; {len(roles) - len(todo)} already saved, {len(todo)} to fetch.\n")

    failed = []
    i = 0
    while i < len(todo):
        role = todo[i]
        role_id, name = role.get("id"), role.get("name", "<unnamed>")
        if not role_id:
            print(f"[{i + 1}/{len(todo)}] SKIP {name}: no id")
            failed.append(name)
            i += 1
            continue
        print(f"[{i + 1}/{len(todo)}] {name} ...")
        status, permissions = get_json(PERMISSIONS_URL.format(domain=DOMAIN, role_id=role_id), headers)
        if status in AUTH_STATUSES:
            save(out_path, roles, results)
            fresh = ask_for_fresh_login(status) if login == "browser token" else None
            if fresh:
                headers = build_headers(fresh)
                continue  # retry this role with the fresh token
            hint = ("browser tokens expire after 5 minutes and only work with their cookie: copy a fresh token AND "
                    "the Cookie from the same Burp request into ONETRUST_TOKEN and ONETRUST_COOKIE, and check "
                    "ONETRUST_DOMAIN matches the request's Host"
                    if login == "browser token" else
                    "this API often refuses API keys and client credentials: use the browser token and Cookie "
                    "from Burp (ONETRUST_TOKEN + ONETRUST_COOKIE) instead")
            sys.exit(f"\nLogin rejected (HTTP {status}) using the {login}. {hint}. Run again and the "
                     f"{len(results)} role(s) saved so far will be skipped.")
        if permissions is None:
            failed.append(name)
        else:
            results[role_id] = {**role, "permissions": permissions}
            save(out_path, roles, results)
        i += 1
        if i < len(todo):
            time.sleep(REQUEST_DELAY)

    save(out_path, roles, results)
    print(f"\nSaved {len(results)}/{len(roles)} role(s) to {out_path}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}. Run again to retry them.")
    if results and not any(r["permissions"] for r in results.values()):
        print("\n[NOTE] Every response was empty. Open a role's permissions in OneTrust, find the request in Burp "
              "that returns them, and put its URL in PERMISSIONS_URL (with {domain} and {role_id}).")


if __name__ == "__main__":
    main()
