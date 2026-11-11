"""Passively record the API calls the OneTrust web app makes, so we learn its real endpoints.

Phase 1 of the assessment-template export. It launches the browser (reusing your saved login from the
other scripts), then only LISTENS: it records every API call the OneTrust web app itself makes while
you click around. It sends NO requests of its own - it cannot create, change or delete anything,
because it never asks OneTrust to do anything. Pure observation.

Use it for the PIA assessment templates: open PIA, go to assessment templates, click each of the three
sub-tabs (Templates, Archive, Notifications), and open a template or two so the app loads their full
definitions. Everything the app fetches is captured. Close the browser window when you're done.

Masking: everything written to disk or printed is run through mask(), which scrubs bearer tokens,
cookies and secret-shaped values. The live Authorization and Cookie headers are never recorded at all.
Captured response bodies keep your real template data but have any credential-shaped substrings masked.

Everything stays local. The only network destination is your own OneTrust tenant (the app's own
traffic); nothing is sent anywhere else. Outputs go in discovery/ next to this script (git-ignored).

Outputs:
  discovery/endpoints.json        - the de-duplicated map of every endpoint seen (method, path, params)
  discovery/responses/*.json      - the masked JSON bodies the app loaded (your data for what you browsed)
  discovery/responses/_manifest.json - every captured call with its response file

Requires Playwright (same one-time setup as onetrust_browser_export.py):
    pip install playwright
    playwright install chromium        # only if Chrome isn't installed
Then: python onetrust_discover.py
"""
import json
import os
import re
import sys
import time
import urllib.parse

# ============================ CONFIGURATION ============================
ONETRUST_DOMAIN = "yourtenant.my.onetrust.com"   # same tenant host you logged in to (no https://)
# ======================================================================

OUTPUT_DIR = "discovery"
PROFILE_DIR = ".onetrust_profile"       # reuse the login saved by onetrust_browser_export.py
BROWSER_CHANNEL = "chrome"              # installed Chrome; falls back to bundled Chromium
HEADLESS = False
API_MARKER = "/api/"                    # only record calls whose URL contains this
MAX_RECORD_SECONDS = 1800               # stop recording after this long if you don't close the window
MAX_BODY_BYTES = 5_000_000             # cap each saved response body
TIMEOUT = 30
LOGIN_TIMEOUT = 600                     # seconds to wait to first see a logged-in API call
HIGHLIGHT = ("template", "assessment", "notification", "pia", "dpia")  # paths to call out in the summary

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

PlaywrightError = Exception   # reassigned to playwright.sync_api.Error in main()


# ------------------------------ pure helpers (no browser; unit-tested) ------------------------------

# Scrubs credentials from anything we write or print. Captured template data is preserved; only
# token-, cookie- and secret-shaped substrings are replaced.
_MASKS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "[MASKED-JWT]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer [MASKED]"),
    (re.compile(r"(?i)(\"?\b(?:authorization|cookie|set-cookie|x-api-key|api[_-]?key|password|"
                r"client[_-]?secret|access[_-]?token|refresh[_-]?token|id[_-]?token)\b\"?\s*[:=]\s*)"
                r"\"?[^\"&;,\s]+\"?"), r"\1[MASKED]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_\-]*(?:token|session|secret|fgpt|jsessionid)[A-Za-z0-9_\-]*)=[^;,&\s\"']+"),
     r"\1=[MASKED]"),
]


def mask(text):
    """Return text with token-, cookie- and secret-shaped substrings replaced by a placeholder."""
    text = str(text)
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return text


def split_url(url):
    """Return (base path without query, query string) for a URL."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", parts.query


def response_filename(idx, method, url):
    """A safe, informative filename for a captured response body."""
    path = urllib.parse.urlsplit(url).path
    safe = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:80] or "root"
    return f"{idx:04d}_{method}_{safe}.json"


def dedupe_endpoints(records):
    """Collapse the raw call log into one entry per (method, path), with params/statuses seen."""
    groups = {}
    for r in records:
        method, url = r.get("method", "GET"), r.get("url", "")
        path, query = split_url(url)
        group = groups.setdefault((method, path), {
            "method": method, "path": path, "count": 0,
            "statuses": set(), "content_types": set(), "sample_query": "", "sample_post_data": "",
        })
        group["count"] += 1
        if r.get("status") is not None:
            group["statuses"].add(r["status"])
        if r.get("content_type"):
            group["content_types"].add(r["content_type"].split(";")[0].strip())
        if query and not group["sample_query"]:
            group["sample_query"] = query
        if r.get("post_data") and not group["sample_post_data"]:
            group["sample_post_data"] = r["post_data"]
    out = []
    for group in groups.values():
        group["statuses"] = sorted(group["statuses"])
        group["content_types"] = sorted(group["content_types"])
        out.append(group)
    out.sort(key=lambda g: (g["path"], g["method"]))
    return out


# ------------------------------ passive recorder ------------------------------

class ApiRecorder:
    def __init__(self, playwright):
        self._pw = playwright
        self.domain = DOMAIN
        self.base = f"https://{DOMAIN}"
        self.out_dir = os.path.join(HERE, OUTPUT_DIR)
        self.responses_dir = os.path.join(self.out_dir, "responses")
        self.context = None
        self.page = None
        self.records = []        # one dict per API call seen (metadata, masked)
        self._pending = []       # (record, Response) awaiting body read
        self.bodies_saved = 0
        self.session_live = False
        self.closed = False

    def start(self):
        os.makedirs(self.responses_dir, exist_ok=True)
        common = dict(user_data_dir=os.path.join(HERE, PROFILE_DIR), headless=HEADLESS,
                      args=LAUNCH_ARGS, no_viewport=True, accept_downloads=False)
        try:
            self.context = self._pw.chromium.launch_persistent_context(channel=BROWSER_CHANNEL, **common)
        except PlaywrightError as e:
            print(f"[WARNING] Could not launch '{BROWSER_CHANNEL}' ({mask(e)}); using bundled Chromium instead.")
            self.context = self._pw.chromium.launch_persistent_context(**common)
        self.context.set_default_timeout(TIMEOUT * 1000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("close", lambda *_: setattr(self, "closed", True))
        self.context.on("response", self._on_response)

    def close(self):
        if self.context:
            try:
                self.context.close()
            except PlaywrightError:
                pass

    def _on_response(self, response):
        """Record one API call's metadata (safe, no body read here) and queue it for a body read."""
        try:
            url = response.url
            if self.domain not in url or API_MARKER not in url:
                return
            request = response.request
            content_type = response.headers.get("content-type", "")
            record = {
                "method": request.method,
                "url": mask(url),               # mask any token carried in the query string
                "status": response.status,
                "content_type": content_type,
            }
            post_data = request.post_data       # safe property; never the Authorization/Cookie header
            if post_data:
                record["post_data"] = mask(post_data[:MAX_BODY_BYTES])
            self.records.append(record)
            self._pending.append((record, response))
            if response.status == 200 and "json" in content_type.lower():
                self.session_live = True
        except PlaywrightError:
            pass

    def _drain(self):
        """Read and save bodies for queued responses while the browser context is still alive."""
        pending, self._pending = self._pending, []
        for record, response in pending:
            if "json" not in record["content_type"].lower():
                record["captured"] = False
                continue
            try:
                body = response.text()
            except (PlaywrightError, ValueError, UnicodeDecodeError):
                record["captured"] = False
                continue
            idx = self.bodies_saved + 1
            name = response_filename(idx, record["method"], record["url"])
            with open(os.path.join(self.responses_dir, name), "w", encoding="utf-8") as f:
                f.write(mask(body)[:MAX_BODY_BYTES])   # keep the data; mask any embedded credential
            record["response_file"] = os.path.join("responses", name)
            record["captured"] = True
            self.bodies_saved += 1

    def wait_for_session(self):
        try:
            self.page.goto(self.base + "/", wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
        except PlaywrightError:
            pass
        print("Waiting to see a logged-in API call in the browser window ... (log in there if prompted)")
        deadline = time.time() + LOGIN_TIMEOUT
        last_note = 0.0
        while time.time() < deadline and not self.session_live and not self.closed:
            try:
                self.page.wait_for_timeout(1000)
            except PlaywrightError:
                break
            self._drain()
            if time.time() - last_note > 15:
                print(f"  ... still waiting ({int(deadline - time.time())}s left). Log in / open OneTrust.")
                last_note = time.time()
        if self.session_live:
            print("  Session is live.\n")
        else:
            print("  [NOTE] Haven't seen a logged-in call yet - recording anyway; log in if you haven't.\n")

    def record(self):
        print("RECORDING. Now in the browser: open PIA > assessment templates and click each of the three\n"
              "sub-tabs (Templates, Archive, Notifications), and open a template or two to load their\n"
              "definitions. Everything the app loads is captured. CLOSE THE WINDOW when you're done.\n")
        start = last_note = time.time()
        while time.time() - start < MAX_RECORD_SECONDS and not self.closed:
            try:
                self.page.wait_for_timeout(1000)
            except PlaywrightError:
                break   # window closed
            self._drain()
            if time.time() - last_note > 5:
                print(f"  recording ... {len(self.records)} API call(s) seen, {self.bodies_saved} body(ies) saved. "
                      f"Close the window when done.")
                last_note = time.time()
        self._drain()

    def write_outputs(self):
        endpoints = dedupe_endpoints(self.records)
        with open(os.path.join(self.out_dir, "endpoints.json"), "w", encoding="utf-8") as f:
            json.dump(endpoints, f, indent=4, ensure_ascii=False)
        with open(os.path.join(self.responses_dir, "_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.records, f, indent=4, ensure_ascii=False)
        return endpoints


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
        recorder = ApiRecorder(pw)
        recorder.start()
        try:
            recorder.wait_for_session()
            recorder.record()
        finally:
            endpoints = recorder.write_outputs()
            recorder.close()

    print(f"\nSaved {len(endpoints)} distinct endpoint(s) and {recorder.bodies_saved} response body(ies) "
          f"to {os.path.join(HERE, OUTPUT_DIR)}")
    highlighted = [e for e in endpoints if any(h in e["path"].lower() for h in HIGHLIGHT)]
    if highlighted:
        print("\nLikely assessment/template endpoints found:")
        for e in highlighted:
            q = f"?{e['sample_query']}" if e["sample_query"] else ""
            print(f"  {e['method']:4} {e['path']}{q}  -> {e['statuses']}")
    else:
        print("\n[NOTE] No template/assessment endpoints were seen. Make sure you opened the PIA "
              "assessment templates screens before closing the window.")
    print("\nSend me discovery/endpoints.json and I'll build the exporter against the real endpoints.")


if __name__ == "__main__":
    main()
