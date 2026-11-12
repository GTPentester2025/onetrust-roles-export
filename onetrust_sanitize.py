"""Sanitize OneTrust responses/exports so they're safe to SHARE (e.g. paste to someone for debugging).

This is NOT encryption-at-rest (that's onetrust_crypto.py, which protects files on your disk). This takes
any response/export and produces a masked copy with every sensitive VALUE removed, while keeping enough
structure to debug from:

  - GUIDs/UUIDs        -> <GUID_1>, <GUID_2> ...   (consistent: the same id always maps to the same token,
                                                    so relationships like rootVersionId <-> a reference stay
                                                    visible without ever exposing the real id)
  - Emails             -> <EMAIL_1> ...            (same consistent scheme)
  - Bearer/JWT/API-key/secret-shaped values -> <secret>
  - Free text (names, descriptions, question text) -> <str:N>   (length only; content gone)   [default]
  - Dates/timestamps   -> <date>
  - KEPT as-is:        JSON keys, booleans, numbers, null, short enum tokens (PIA, ACTIVE, IN_PROGRESS),
                       locale codes (en-US), and short all-digit strings (versions/counts/page sizes).

Note: ALL_CAPS tokens are preserved because they're almost always status/type codes - the values you need
to debug. If you want ZERO values at all, use the structure-only dump from onetrust_assessment_export.py.

The reverse mapping (token -> real value) is held only in memory and is NEVER written out, so a sanitized
file can't be turned back into the original by whoever receives it. One run uses one shared mapping across
all its inputs, so a given id gets the same token in every file.

Everything is local and read-only: it only transforms JSON/text you already have; no network, no changes
to OneTrust.

Usage:
    python onetrust_sanitize.py response.json                 # -> response.sanitized.json
    python onetrust_sanitize.py discovery/responses/          # sanitize every *.json in the folder
    python onetrust_sanitize.py assessment_templates_export.enc   # decrypt in memory, then sanitize
    python onetrust_sanitize.py - < input.json > out.json     # stdin -> stdout
    python onetrust_sanitize.py --keep-text response.json     # keep free text; mask only ids/secrets
"""
import json
import os
import re
import sys

# --------------------------- classification patterns (unit-tested) ---------------------------
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_GUID_FULL = re.compile(r"^" + GUID_RE.pattern + r"$")
_EMAIL_FULL = re.compile(r"^" + EMAIL_RE.pattern + r"$")
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")          # PIA, ACTIVE, IN_PROGRESS, ARCHIVED
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")      # en, en-US, fr-FR
_NUM = re.compile(r"^\d{1,6}$")                         # versions, counts, page sizes
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")               # 2026-11-12 / ...T...
_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_LONGHEX = re.compile(r"^[0-9a-fA-F]{32,}$")            # hex secrets / hashes
_LONGB64 = re.compile(r"^[A-Za-z0-9_\-]{40,}={0,2}$")   # long opaque tokens
_LITERAL_KEEP = {"true", "false", "null", "yes", "no", "n/a", "none"}


def _looks_secret(s):
    return bool(_JWT.match(s) or _LONGHEX.match(s) or _LONGB64.match(s))


class Sanitizer:
    """Deep-mask a JSON value. One instance = one consistent token namespace across every value it sees."""

    def __init__(self, mask_free_text=True):
        self.mask_free_text = mask_free_text
        self._maps = {}       # kind -> {real value: token}
        self._counts = {}     # kind -> running counter

    # ---- consistent tokens ----
    def _token(self, kind, value):
        table = self._maps.setdefault(kind, {})
        if value not in table:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            table[value] = f"<{kind}_{self._counts[kind]}>"
        return table[value]

    # ---- structured JSON ----
    def sanitize(self, data):
        return self._walk(data)

    def _walk(self, data):
        if isinstance(data, dict):
            return {self._key(k): self._walk(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._walk(v) for v in data]
        if isinstance(data, bool) or data is None or isinstance(data, (int, float)):
            return data            # non-string scalars carry no identifiers; keep them
        if isinstance(data, str):
            return self._scalar(data)
        return data

    def _key(self, key):
        s = str(key)
        if _GUID_FULL.match(s):
            return self._token("GUID", s)
        if _EMAIL_FULL.match(s):
            return self._token("EMAIL", s)
        return s                    # ordinary keys ARE the schema - keep them

    def _scalar(self, s):
        if s == "":
            return s
        if _GUID_FULL.match(s):
            return self._token("GUID", s)
        if _EMAIL_FULL.match(s):
            return self._token("EMAIL", s)
        if _looks_secret(s):
            return "<secret>"
        if _ENUM.match(s) or _LOCALE.match(s) or _NUM.match(s) or s.lower() in _LITERAL_KEEP:
            return s                # status/type/locale/version/count - safe and useful
        if _DATE.match(s):
            return "<date>"
        if not self.mask_free_text:
            return s                # --keep-text: leave free text, ids/secrets already handled above
        return f"<str:{len(s)}>"    # free text: keep only the length

    # ---- non-JSON text fallback ----
    def sanitize_text(self, text):
        """For input that isn't valid JSON: tokenize ids/emails and mask secret-shaped substrings."""
        text = str(text)
        text = GUID_RE.sub(lambda m: self._token("GUID", m.group(0)), text)
        text = EMAIL_RE.sub(lambda m: self._token("EMAIL", m.group(0)), text)
        text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", "<secret>", text)
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+", "Bearer <secret>", text)
        return text


# --------------------------- CLI plumbing ---------------------------

def _sanitize_json_bytes(sanitizer, raw):
    """Sanitize bytes as JSON if possible, else as text. Returns (text_out, was_json)."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return sanitizer.sanitize_text(raw.decode("utf-8", "replace")), False
    return json.dumps(sanitizer.sanitize(obj), indent=2, ensure_ascii=False), True


def _out_path(in_path):
    stem = re.sub(r"\.(json|enc|txt)$", "", in_path, flags=re.IGNORECASE)
    return stem + ".sanitized.json"


def _sanitize_file(sanitizer, path):
    if path.lower().endswith(".enc"):
        import onetrust_crypto as otc
        if not otc.available():
            sys.exit("[SETUP] Reading a .enc file needs: pip install cryptography")
        passphrase = otc.prompt_passphrase(confirm=False)
        try:
            obj = otc.decrypt_file_to_json(path, passphrase)
        except ValueError as e:
            sys.exit(f"[DECRYPT] {e}")
        text, _ = json.dumps(sanitizer.sanitize(obj), indent=2, ensure_ascii=False), True
    else:
        with open(path, "rb") as f:
            text, _ = _sanitize_json_bytes(sanitizer, f.read())
    out = _out_path(path)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mask_free_text = True
    if "--keep-text" in argv:
        mask_free_text = False
        argv.remove("--keep-text")
    if not argv:
        sys.exit(__doc__.strip().splitlines()[0] + "\n\nSee: python onetrust_sanitize.py --help-usage")
    if argv == ["--help-usage"]:
        print(__doc__)
        return

    sanitizer = Sanitizer(mask_free_text=mask_free_text)

    if argv == ["-"]:
        text, _ = _sanitize_json_bytes(sanitizer, sys.stdin.buffer.read())
        sys.stdout.write(text)
        return

    written = []
    for target in argv:
        if os.path.isdir(target):
            for name in sorted(os.listdir(target)):
                if name.lower().endswith(".json") and not name.lower().endswith(".sanitized.json"):
                    written.append(_sanitize_file(sanitizer, os.path.join(target, name)))
        elif os.path.exists(target):
            written.append(_sanitize_file(sanitizer, target))
        else:
            print(f"[SKIP] not found: {target}")

    for path in written:
        print(f"Wrote {path}")
    if written:
        print(f"\n{len(written)} file(s) sanitized. The token<->value mapping was NOT saved; these files "
              f"cannot be reversed.\nReview one before sharing if you want to be sure.")


if __name__ == "__main__":
    main()
