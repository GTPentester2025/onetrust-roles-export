"""Read-only OAuth2 client for OneTrust's official APIs. Python standard library only - no dependencies.

Client-credentials flow (https://developer.onetrust.com/onetrust/reference/getoauthtoken-1):
  POST /api/access/v1/oauth/token   grant_type=client_credentials & client_id & client_secret
returns a short-lived bearer token, which is then sent as `Authorization: Bearer <token>` on GET calls.

READ-ONLY BY CONSTRUCTION: this client exposes only get() plus the token request. It has no
post()/put()/patch()/delete() for OneTrust resources, so it cannot create, change or delete anything.

Create the credentials in OneTrust under Global Settings > Access Management > Client Credentials, with a
read scope (ASSESSMENT_READ). Provide them via the environment:
    set ONETRUST_CLIENT_ID=...         (PowerShell:  $env:ONETRUST_CLIENT_ID = "...")
    set ONETRUST_CLIENT_SECRET=...
If unset, you're prompted (hidden). The secret is never logged - errors are passed through redact().
"""
import getpass
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TOKEN_PATH = "/api/access/v1/oauth/token"
DEFAULT_SCOPE = "ASSESSMENT_READ"
TOKEN_SKEW = 30        # refresh this many seconds before the token actually expires
DEFAULT_TTL = 3000     # assumed token lifetime if the response omits expires_in
TIMEOUT = 60


# ------------------------------ pure helpers (unit-tested, no network) ------------------------------

def load_credentials(prompt=True):
    """Return (client_id, client_secret) from the environment, prompting (hidden) only if allowed."""
    cid = os.environ.get("ONETRUST_CLIENT_ID")
    secret = os.environ.get("ONETRUST_CLIENT_SECRET")
    if prompt and not cid:
        cid = input("OneTrust client_id: ").strip()
    if prompt and not secret:
        secret = getpass.getpass("OneTrust client_secret: ").strip()
    if not cid or not secret:
        raise ValueError("missing client_id/client_secret "
                         "(set ONETRUST_CLIENT_ID / ONETRUST_CLIENT_SECRET, or run interactively)")
    return cid, secret


def token_form(client_id, client_secret, scope):
    """Build the x-www-form-urlencoded body for the client-credentials token request."""
    fields = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    if scope:
        fields["scope"] = scope
    return urllib.parse.urlencode(fields).encode("utf-8")


def parse_token_response(data):
    """Return (access_token, expires_in) from a token response, tolerating a missing expires_in."""
    if not isinstance(data, dict):
        return None, None
    return data.get("access_token"), data.get("expires_in")


def redact(text, *secrets):
    """Strip known secrets and token-shaped substrings from text before printing it."""
    s = str(text)
    for sec in secrets:
        if sec:
            s = s.replace(sec, "[REDACTED]")
    s = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", "[REDACTED-JWT]", s)
    s = re.sub(r"(?i)(\"?(?:access_token|refresh_token|client_secret)\"?\s*[:=]\s*\"?)[^\"&,\s]+",
               r"\1[REDACTED]", s)
    return s


def _http(method, url, headers, body=None):
    """One HTTP call. Returns (status, parsed-json-or-text). HTTP errors return their status + body."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


# ------------------------------ the client ------------------------------

class ApiClient:
    """A read-only OAuth2 client. Only get() and the token request reach OneTrust."""

    def __init__(self, domain, client_id, client_secret, scope=DEFAULT_SCOPE, now=None):
        self.base = "https://" + domain.replace("https://", "").replace("http://", "").strip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self._now = now or __import__("time").time
        self._access_token = None
        self._expiry = 0.0

    def _fetch_token(self):
        """POST the client-credentials request and return (access_token, expires_in)."""
        status, data = _http("POST", self.base + TOKEN_PATH,
                             {"Content-Type": "application/x-www-form-urlencoded",
                              "Accept": "application/json"},
                             token_form(self._client_id, self._client_secret, self.scope))
        if status != 200:
            raise RuntimeError(f"token request failed: HTTP {status}: "
                               f"{redact(json.dumps(data) if isinstance(data, dict) else data, self._client_secret)}")
        access_token, expires_in = parse_token_response(data)
        if not access_token:
            raise RuntimeError("token response had no access_token")
        return access_token, expires_in

    def token(self):
        """Return a valid bearer token, fetching or refreshing as needed (cached in memory)."""
        if self._access_token and self._now() < self._expiry - TOKEN_SKEW:
            return self._access_token
        access_token, expires_in = self._fetch_token()
        self._access_token = access_token
        self._expiry = self._now() + (expires_in or DEFAULT_TTL)
        return access_token

    def get(self, path, params=None):
        """GET an endpoint with the bearer token. Returns (status, parsed body). Refreshes once on 401/403."""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        status, data = self._get_once(url)
        if status in (401, 403):            # token may be stale; drop it and try one fresh token
            self._access_token = None
            status, data = self._get_once(url)
        return status, data

    def _get_once(self, url):
        return _http("GET", url, {"Authorization": "Bearer " + self.token(), "Accept": "application/json"})
