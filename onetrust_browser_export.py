"""Export every OneTrust role, with full details and permissions, by driving a real browser.

You log in to OneTrust ONCE in the browser window this opens (SSO/MFA and all). From then on the
script reads the live session's own bearer token straight from the web app's traffic and makes the
API calls through the browser's own context, so the __Secure-fgpt cookie and every other session
cookie ride along automatically. No more copying a token and cookie out of Burp every 5 minutes:
when a token is about to expire or a request is refused, the script quietly reloads the app to let
OneTrust mint a fresh token, then carries on from the same role. If your SSO session itself expires
mid-run, it pauses and waits for you to log in again in the window, then resumes.

Everything stays on your computer. At run time the ONLY network destination is your own OneTrust
tenant over HTTPS: no analytics, no telemetry, no third-party calls (Chromium is launched with
background networking disabled). The one-time `playwright install chromium` download happens at
setup, carries none of your data, and never recurs. Output and the login profile are written to
local disk next to this script and are git-ignored.

Read-only: every data request is a GET. It never creates, changes or deletes anything in OneTrust.

What one run produces: lists every role via /api/access/v2/roles/list (following pagination), then
for each role fetches both the full detail record (/roles/{id}) and its accessible-permissions,
merged into one record per role, saved to onetrust_roles_full.json. It saves after every role, so an
interruption keeps progress and a rerun skips roles already saved.

Requires Playwright (a one-time setup, see the README):
    pip install playwright
    playwright install chromium        # only needed if Chrome isn't installed
Then: python onetrust_browser_export.py
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse

# ============================ CONFIGURATION: fill this in ============================
ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"   # host you log in to, e.g. app-de.onetrust.com (no https://)
EXTRA_HEADERS = {}        # optional: extra headers the web app sends that the API requires, {"Header": "value"}
# =====================================================================================

OUTPUT_FILE = "onetrust_roles_full.json"   # written next to this script
PROFILE_DIR = ".onetrust_profile"          # local browser profile so you stay logged in between runs
BROWSER_CHANNEL = "chrome"                  # use installed Google Chrome; falls back to bundled Chromium
HEADLESS = False                            # must stay False so you can log in in the window

ROLES_PAGE_SIZE = 50   # roles per page when listing (what the web app uses)
REQUEST_DELAY = 0.5    # seconds between role requests
TIMEOUT = 30           # seconds per HTTP request / navigation
MAX_RETRIES = 5        # retries on 429 / 5xx / connection errors
BACKOFF_FACTOR = 1     # waits 1s, 2s, 4s... between retries (Retry-After header wins when sent)
RETRY_STATUSES = {429, 500, 502, 503, 504}
AUTH_STATUSES = {401, 403}   # token expired or refused: refresh the token and retry
TOKEN_SKEW = 30        # refresh the token when fewer than this many seconds are left
AUTH_MAX_REFRESH = 3   # token refreshes per request before pausing for a fresh login
REFRESH_WAIT = 20      # seconds to wait for a fresh token after reloading the app
LOGIN_TIMEOUT = 600    # seconds to wait for you to finish logging in

# Chromium flags that keep it from phoning home for anything but your tenant.
LAUNCH_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-breakpad",
    "--no-first-run",
    "--no-default-browser-check",
]

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = ONETRUST_DOMAIN.replace("https://", "").replace("http://", "").strip("/")

# Reassigned to playwright.sync_api.Error inside main(); a bare Exception keeps the module importable
# (and its pure helpers testable) without Playwright installed.
PlaywrightError = Exception


# ------------------------------ pure helpers (no browser; unit-tested) ------------------------------

# Scrubs credentials out of anything we print, so error logs are safe to paste or share. Nothing we
# log is meant to contain a token or cookie (we never print self.token, the Authorization header or
# the Cookie header), but error bodies and exception text are the one place something could slip in,
# so every such string goes through here first.
_REDACTIONS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "[REDACTED-JWT]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    # header- or query-style name: value / name=value for anything credential-shaped
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_-]?key|password|client[_-]?secret|"
                r"access[_-]?token|refresh[_-]?token|id[_-]?token)\b(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
    # cookie / token values carried as name=value pairs (e.g. __Secure-fgpt=...)
    (re.compile(r"(?i)\b([A-Za-z0-9_\-]*(?:token|session|secret|fgpt|jsessionid)[A-Za-z0-9_\-]*)=[^;,&\s\"']+"),
     r"\1=[REDACTED]"),
]


def redact(text):
    """Return text with any token-, cookie- or secret-shaped substrings replaced by a placeholder."""
    text = str(text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def jwt_exp(token):
    """Return a JWT's "exp" claim (epoch seconds) as an int, or None if the token isn't a readable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def normalize_roles_list(data):
    """Pull the list of roles out of a roles/list response (a page object with "content", or a bare array).
    Returns (roles or None, totalPages or None)."""
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, list):
            total = data.get("totalPages")
            return content, total if isinstance(total, int) else None
    return None, None


def merge_role(summary, detail, permissions):
    """Build one record per role: the full detail record (or the list summary if detail failed),
    plus the role's accessible-permissions under "accessible_permissions"."""
    record = dict(detail) if isinstance(detail, dict) else dict(summary)
    record.setdefault("id", summary.get("id"))
    record["accessible_permissions"] = permissions
    return record


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


def save(path, roles, results):
    """Write the merged records in roles-list order."""
    ordered = [results[r["id"]] for r in roles if r.get("id") in results]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=4, ensure_ascii=False)


# ------------------------------ browser-driven exporter ------------------------------

class BrowserExporter:
    """Drives the logged-in browser: captures the live bearer token and makes the API GETs through
    the browser's own context (so session cookies are sent automatically)."""

    def __init__(self, playwright):
        self._pw = playwright
        self.domain = DOMAIN
        self.base = f"https://{DOMAIN}"
        self.profile_dir = os.path.join(HERE, PROFILE_DIR)
        self.context = None
        self.page = None
        self.token = None       # freshest bearer token seen in the web app's traffic
        self.token_exp = None   # its "exp" (epoch seconds), when readable

    # ----- lifecycle -----

    def start(self):
        common = dict(user_data_dir=self.profile_dir, headless=HEADLESS, args=LAUNCH_ARGS,
                      no_viewport=True, accept_downloads=False)
        try:
            self.context = self._pw.chromium.launch_persistent_context(channel=BROWSER_CHANNEL, **common)
        except PlaywrightError as e:
            print(f"[WARNING] Could not launch '{BROWSER_CHANNEL}' ({redact(e)}); using bundled Chromium instead.")
            self.context = self._pw.chromium.launch_persistent_context(**common)
        self.context.set_default_timeout(TIMEOUT * 1000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.context.on("request", self._on_request)

    def close(self):
        if self.context:
            try:
                self.context.close()
            except PlaywrightError:
                pass

    # ----- token capture -----

    def _on_request(self, request):
        """Record the freshest bearer token the web app sends to your tenant's API."""
        url = request.url
        if self.domain not in url or "/api/" not in url:
            return
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        token = auth[7:].strip()
        if not token:
            return
        exp = jwt_exp(token)
        # keep only a strictly fresher token, so replaying our own request never regresses it
        if exp is None:
            if self.token is None:
                self.token = token
        elif self.token_exp is None or exp >= self.token_exp:
            self.token, self.token_exp = token, exp

    def _token_seconds_left(self):
        return None if self.token_exp is None else self.token_exp - time.time()

    def _headers(self):
        headers = {"Accept": "application/json", **EXTRA_HEADERS}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _sleep(self, seconds):
        # wait_for_timeout keeps the event loop pumping so token capture keeps working during waits
        self.page.wait_for_timeout(int(seconds * 1000))

    # ----- login / refresh -----

    def _probe(self):
        """Ask the API for one role. Returns True only on a 200 with a JSON body (not an HTML login page)."""
        url = f"{self.base}/api/access/v2/roles/list?page=0&size=1"
        try:
            resp = self.context.request.get(url, headers=self._headers(), timeout=TIMEOUT * 1000)
        except PlaywrightError:
            return False
        if resp.status != 200:
            return False
        try:
            resp.json()
            return True
        except (ValueError, UnicodeDecodeError):
            return False  # a logged-out session redirects to an HTML login page

    def wait_for_login(self, reason="", initial=False):
        """Open the tenant and wait until the session can actually call the API. Works for a persisted
        login (returns at once), a fresh SSO/MFA login, and a session that expired mid-run."""
        if reason:
            print(f"\n[LOGIN] {reason}")
        try:
            self.page.goto(self.base + "/", wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
        except PlaywrightError:
            pass
        verb = "Waiting for your OneTrust session" if initial else "Waiting for you to log in again"
        print(f"{verb} in the browser window ... (log in there; this can take a minute)")
        deadline = time.time() + LOGIN_TIMEOUT
        last_note = 0.0
        while time.time() < deadline:
            self._sleep(2)
            if self._probe():
                print("  Session is live. Continuing ...\n")
                return True
            if time.time() - last_note > 15:
                print(f"  ... still waiting ({int(deadline - time.time())}s left). Finish logging in in the window.")
                last_note = time.time()
        print("  [TIMEOUT] No working session detected.")
        return False

    def refresh_token(self, force=False):
        """Reload the app so OneTrust mints a fresh token, and wait until we hold one with life left.
        Returns False if no fresh token appears (likely logged out) so the caller can pause for login."""
        left = self._token_seconds_left()
        if not force and left is not None and left > TOKEN_SKEW:
            return True
        old_exp = self.token_exp or 0
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
        except PlaywrightError:
            pass
        deadline = time.time() + REFRESH_WAIT
        while time.time() < deadline:
            self._sleep(0.5)
            left = self._token_seconds_left()
            if left is not None and left > TOKEN_SKEW and self.token_exp >= old_exp:
                return True
        # no token at all (cookie-only tenant) is fine; only a stale/expired token is a real failure
        return self.token_exp is None or self._token_seconds_left() > 0

    def _ensure_token(self):
        left = self._token_seconds_left()
        if left is not None and left <= TOKEN_SKEW:
            self.refresh_token(force=True)

    # ----- requests -----

    def api_get(self, url):
        """GET through the browser context (cookies sent automatically). Returns (status or None, JSON or None).
        Refreshes the token on 401/403 (pausing for login if that fails) and backs off on 429/5xx/network errors."""
        auth_refreshes = transient = 0
        while True:
            self._ensure_token()
            try:
                resp = self.context.request.get(url, headers=self._headers(), timeout=TIMEOUT * 1000)
            except PlaywrightError as e:
                transient += 1
                if transient > MAX_RETRIES:
                    print(f"  [ERROR] {redact(e)} (gave up after {MAX_RETRIES} retries)")
                    return None, None
                delay = BACKOFF_FACTOR * 2 ** (transient - 1)
                print(f"  [RETRY] {redact(e)}, retrying in {delay:.0f}s ({transient}/{MAX_RETRIES})")
                self._sleep(delay)
                continue

            status = resp.status
            if status in AUTH_STATUSES:
                auth_refreshes += 1
                if auth_refreshes > AUTH_MAX_REFRESH:
                    print(f"  [ERROR] HTTP {status}: still refused after {AUTH_MAX_REFRESH} token refreshes.")
                    return status, None
                print(f"  [AUTH] HTTP {status}; refreshing the session token ...")
                if not self.refresh_token(force=True):
                    self.wait_for_login(reason=f"HTTP {status} - your login may have expired.")
                continue
            if status in RETRY_STATUSES:
                transient += 1
                if transient > MAX_RETRIES:
                    print(f"  [ERROR] HTTP {status} (gave up after {MAX_RETRIES} retries)")
                    return status, None
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else BACKOFF_FACTOR * 2 ** (transient - 1)
                print(f"  [RETRY] HTTP {status}, retrying in {delay:.0f}s ({transient}/{MAX_RETRIES})")
                self._sleep(delay)
                continue
            if status >= 400:
                print(f"  [ERROR] HTTP {status}: {redact(resp.text()[:300])}")
                return status, None
            try:
                return status, resp.json()
            except (ValueError, UnicodeDecodeError):
                print(f"  [ERROR] response was not JSON: {redact(resp.text()[:200])!r}")
                return status, None

    # ----- export -----

    def list_roles(self):
        """Return every role summary, following page/size pagination. None if listing fails outright."""
        print(f"Listing roles from {self.base}/api/access/v2/roles/list ...")
        roles, page = [], 0
        while True:
            params = urllib.parse.urlencode({"roleName": "", "roleTypeId": "", "solutionNames": "",
                                             "page": page, "size": ROLES_PAGE_SIZE})
            _, data = self.api_get(f"{self.base}/api/access/v2/roles/list?{params}")
            if data is None:
                return roles or None
            if isinstance(data, list):   # bare array, not paginated
                return data
            content, total_pages = normalize_roles_list(data)
            if content is None:
                print(f"  [ERROR] unexpected roles-list response; keys: {list(data)[:10]}")
                return roles or None
            roles.extend(content)
            if not content or (total_pages is not None and page + 1 >= total_pages):
                return roles
            page += 1
            self._sleep(REQUEST_DELAY)

    def export(self, roles):
        out_path = os.path.join(HERE, OUTPUT_FILE)
        results = load_previous(out_path)
        todo = [r for r in roles if r.get("id") not in results]
        print(f"{len(roles)} role(s); {len(roles) - len(todo)} already saved, {len(todo)} to fetch.\n")

        failed = []
        for i, role in enumerate(todo, 1):
            role_id, name = role.get("id"), role.get("name", "<unnamed>")
            if not role_id:
                print(f"[{i}/{len(todo)}] SKIP {name}: no id")
                failed.append(name)
                continue
            print(f"[{i}/{len(todo)}] {name} ...")
            _, detail = self.api_get(f"{self.base}/api/access/v2/roles/{role_id}")
            self._sleep(0.2)
            _, permissions = self.api_get(f"{self.base}/api/access/v2/roles/{role_id}/accessible-permissions")
            if detail is None and permissions is None:
                failed.append(name)
            else:
                results[role_id] = merge_role(role, detail, permissions)
                save(out_path, roles, results)
            if i < len(todo):
                self._sleep(REQUEST_DELAY)

        save(out_path, roles, results)
        print(f"\nSaved {len(results)}/{len(roles)} role(s) to {out_path}")
        if failed:
            print(f"Failed ({len(failed)}): {', '.join(failed)}. Run again to retry them.")


def main():
    if "yourtenant" in DOMAIN or not DOMAIN:
        sys.exit("[CONFIG] Set ONETRUST_DOMAIN at the top of the script to your tenant host, e.g. app-de.onetrust.com")
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError:
        sys.exit("[SETUP] Playwright isn't installed. Run:\n  pip install playwright\n"
                 "  playwright install chromium   (only needed if Google Chrome isn't installed)")

    global PlaywrightError
    PlaywrightError = Error

    with sync_playwright() as pw:
        exporter = BrowserExporter(pw)
        exporter.start()
        try:
            if not exporter.wait_for_login(initial=True):
                sys.exit("Gave up waiting for a OneTrust session. Run again and log in when the window opens.")
            roles = exporter.list_roles()
            if not roles:
                sys.exit("No roles were listed. Check that ONETRUST_DOMAIN matches the tenant you logged in to.")
            exporter.export(roles)
        finally:
            exporter.close()


if __name__ == "__main__":
    main()
