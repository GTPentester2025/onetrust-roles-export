"""Fetch the details (including permissions) of every OneTrust role listed in roles.json.

roles.json is the response of GET /api/access/v2/roles/list, saved from Burp next to this script.
For each role, the script calls GET /api/access/v2/roles/{roleId}, the request the OneTrust web app
sends when you open a role, and saves the responses to role_permissions.json.

Read-only: the script sends only GET requests. It never creates, changes or deletes anything in OneTrust.

That API isn't in OneTrust's public docs and refuses API client tokens (HTTP 403), so give the script
the token your browser sends: in Burp, the value after "Authorization: Bearer " on any /api/ request.
If the token expires partway, the script saves what it has and stops. Paste a fresh token and run it
again; roles already saved are skipped.

Standard library only: no pip install needed (Python 3.8+).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ============================ CONFIGURATION: fill these in ============================
ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"   # host you log in to, without https://
ONETRUST_TOKEN = "paste-your-token-here"          # value after "Authorization: Bearer " in Burp
ONETRUST_COOKIE = ""      # optional: the Cookie header value, if Burp's request has no Authorization header
EXTRA_HEADERS = {}        # optional: other headers copied from Burp's request, e.g. {"Header-Name": "value"}
# =======================================================================================

ROLES_FILE = "roles.json"               # roles list saved from Burp, next to this script
OUTPUT_FILE = "role_permissions.json"   # written next to this script
ROLE_URL = "https://{domain}/api/access/v2/roles/{role_id}"   # change if Burp shows permissions at another URL
REQUEST_DELAY = 0.5   # seconds between role requests
TIMEOUT = 30          # seconds per HTTP request
MAX_RETRIES = 5       # retries on 429 / 5xx / connection errors
BACKOFF_FACTOR = 1    # waits 1s, 2s, 4s, 8s... between retries (Retry-After header wins when sent)
RETRY_STATUSES = {429, 500, 502, 503, 504}
AUTH_STATUSES = {401, 403}   # token expired or not allowed: stop instead of failing every role


HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = ONETRUST_DOMAIN.replace("https://", "").strip("/")
TOKEN = ONETRUST_TOKEN
COOKIE = ONETRUST_COOKIE


def is_filled(value):
    return bool(value) and not value.startswith("paste-")


def build_headers():
    headers = {"Accept": "application/json", **EXTRA_HEADERS}
    if is_filled(TOKEN):
        token = TOKEN.strip()
        if token.lower().startswith("bearer "):  # pasted with the "Bearer " prefix
            token = token[7:].strip()
        headers["Authorization"] = f"Bearer {token}"
    if COOKIE:
        headers["Cookie"] = COOKIE.strip()
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
            return {r["id"]: r for r in json.load(f) if isinstance(r, dict) and r.get("id")}
    except (ValueError, TypeError):
        print(f"[WARNING] {path} could not be read; starting over.\n")
        return {}


def get_json(url, headers):
    """GET a URL. Returns (HTTP status or None, parsed JSON or None), retrying 429/5xx/connection errors.
    This is the script's only network call, and it is always a GET."""
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
    if "yourtenant" in DOMAIN or not (is_filled(TOKEN) or COOKIE):
        sys.exit("[CONFIG] Fill in ONETRUST_DOMAIN and ONETRUST_TOKEN (or ONETRUST_COOKIE) "
                 "in the CONFIGURATION block at the top of the script.")

    roles = load_roles(os.path.join(HERE, ROLES_FILE))
    out_path = os.path.join(HERE, OUTPUT_FILE)
    results = load_previous(out_path)
    headers = build_headers()
    todo = [r for r in roles if r.get("id") not in results]
    print(f"{len(roles)} role(s) in {ROLES_FILE}; {len(roles) - len(todo)} already saved, {len(todo)} to fetch.\n")

    failed = []
    for i, role in enumerate(todo, 1):
        role_id, name = role.get("id"), role.get("name", "<unnamed>")
        if not role_id:
            print(f"[{i}/{len(todo)}] SKIP {name}: no id")
            failed.append(name)
            continue
        print(f"[{i}/{len(todo)}] {name} ...")
        status, detail = get_json(ROLE_URL.format(domain=DOMAIN, role_id=role_id), headers)
        if status in AUTH_STATUSES:
            save(out_path, roles, results)
            sys.exit(f"\nToken rejected (HTTP {status}): it has expired, or isn't allowed to call this API "
                     f"(API keys get 403 here). Copy a fresh token from Burp into ONETRUST_TOKEN and run again; "
                     f"the {len(results)} role(s) saved so far will be skipped.")
        if detail is None:
            failed.append(name)
        else:
            results[role_id] = {**role, "details": detail}
            save(out_path, roles, results)
        if i < len(todo):
            time.sleep(REQUEST_DELAY)

    save(out_path, roles, results)
    print(f"\nSaved {len(results)}/{len(roles)} role(s) to {out_path}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}. Run again to retry them.")
    if results and "permission" not in json.dumps([r["details"] for r in results.values()]).lower():
        print("\n[NOTE] None of the responses mention permissions. Open a role's permissions in OneTrust, find the "
              "request in Burp that returns them, and put its URL in ROLE_URL (with {domain} and {role_id}).")


if __name__ == "__main__":
    main()
