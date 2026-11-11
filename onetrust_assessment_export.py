"""Export all PIA/DPIA assessment templates - definitions, content, actions, translations, notifications.

Browser-driven and READ-ONLY: every request is a GET. It reuses the logged-in browser session from
onetrust_browser_export.py (same local .onetrust_profile, same token capture and auto-refresh), so you
don't paste tokens, and it calls the exact endpoints the OneTrust web app uses on PIA > assessment
templates (found with onetrust_discover.py):

    GET /api/template/v1/settings
    GET /api/template/v1/templates/details?type=PIA                         (the Templates/Archive list)
  then for each template {id}:
    GET /api/template/v1/templates/{id}/details                             (full definition)
    GET /api/template/v1/templates/rootversion/{rootVersionId}?version={n}  (questionnaire content)
    GET /api/template/v1/templates/{id}/actions                             (logic/actions)
    GET /api/template/v1/templates/{id}/translations                        (translations)
    GET /api/template/v1/custom-notifications/templates/{id}?...            (Notifications tab)

`type=PIA` covers both PIA and DPIA. One record per template is written to assessment_templates_full.json
(saved after each template, so a rerun skips what's done) and also split into one file per template under
assessment_templates/. Everything stays local; the only network destination is your tenant.

Requires Playwright (same setup as onetrust_browser_export.py) and that script next to this one.
Run: python onetrust_assessment_export.py
"""
import json
import os
import re
import sys

import onetrust_browser_export as obe   # reuse the browser session (login, token capture, api_get)
import onetrust_crypto as otc           # passphrase encryption at rest
from split_roles import role_stem       # reuse safe, unique per-record filenames

# ============================ CONFIGURATION ============================
ONETRUST_DOMAIN = "app-de.onetrust.com"   # your tenant host (prefilled from discovery; no https://)
# Each entry is a query string for the templates list. PIA covers PIA & DPIA. If archived templates
# turn out to need their own filter, add it here as another entry, e.g. "type=PIA&status=ARCHIVED";
# templates are de-duplicated by id across entries.
LIST_QUERIES = ["type=PIA"]
# ======================================================================

ENCRYPT_OUTPUT = True   # write the export encrypted at rest (passphrase); see onetrust_crypto.py
ENC_FILE = "assessment_templates_export.enc"    # encrypted bundle (templates + settings); decrypt to read
OUTPUT_FILE = "assessment_templates_full.json"  # plaintext output, only when ENCRYPT_OUTPUT = False
PER_TEMPLATE_DIR = "assessment_templates"
SETTINGS_FILE = "assessment_templates_settings.json"
STRUCTURE_FILE = "assessment_templates_list_structure.json"   # diagnostic: shapes only, NO values/IDs/PII
RAW_LIST_FILE = "assessment_templates_list_raw.json"          # raw list response(s); only if SAVE_RAW
SAVE_RAW = False   # keep False for privacy: don't write response bodies (IDs/PII) to disk at all
NOTIF_PAGE_SIZE = 200
REQUEST_DELAY = 0.3

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = ONETRUST_DOMAIN.replace("https://", "").replace("http://", "").strip("/")


# ------------------------------ pure helpers (no browser; unit-tested) ------------------------------

def describe_shape(data):
    """A short, safe description of a JSON value's shape, for diagnosing an unexpected response."""
    if isinstance(data, list):
        return f"list[{len(data)}]"
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            if isinstance(v, list):
                parts.append(f"{k}: list[{len(v)}]")
            elif isinstance(v, dict):
                parts.append(f"{k}: dict{{{len(v)}}}")
            else:
                parts.append(f"{k}: {type(v).__name__}")
        return "dict{" + ", ".join(parts) + "}"
    return type(data).__name__


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _safe_key(key):
    """Mask a dict key that is itself an id or email, so even field names leak nothing."""
    key = str(key)
    if _GUID_RE.match(key):
        return "<guid>"
    if "@" in key:
        return "<email>"
    return key


def structure(data, _depth=0):
    """Describe a JSON value's structure only - key names, types, list lengths - with NO values, ids or PII.
    Safe to share for diagnosing a response. For a list, shows its length and the shape of its first item."""
    if _depth > 8:
        return "..."
    if isinstance(data, dict):
        keys = list(data.keys())
        if len(keys) > 1 and all(_GUID_RE.match(str(k)) for k in keys):   # a map keyed by ids
            return {f"<{len(keys)} guid-keyed entries>": structure(next(iter(data.values())), _depth + 1)}
        return {_safe_key(k): structure(v, _depth + 1) for k, v in data.items()}
    if isinstance(data, list):
        return "list[0]" if not data else [f"list[{len(data)}]", structure(data[0], _depth + 1)]
    if data is None:
        return "null"
    return type(data).__name__


def extract_templates(data):
    """Pull the list of template records out of the list response, tolerating several shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("content", "data", "templates", "results", "items", "details"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):  # one level of nesting, e.g. {"data": {"content": [...]}}
                for inner in ("content", "data", "templates", "results", "items"):
                    if isinstance(value.get(inner), list):
                        return value[inner]
    return None


def ci_get(record, *names):
    """Case-insensitive lookup: return the first present, non-empty value among the given key names."""
    if not isinstance(record, dict):
        return None
    lower = {k.lower(): v for k, v in record.items()}
    for name in names:
        value = lower.get(name.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def template_id(record):
    return ci_get(record, "id", "templateId", "template_id", "templateRootVersionId")


def template_name(record):
    return ci_get(record, "name", "templateName", "title") or "<unnamed>"


def root_version_id(*records):
    for r in records:
        value = ci_get(r, "rootVersionId", "rootVersion", "templateRootVersionId", "rootVersionID")
        if value:
            return value
    return None


def version_number(*records):
    for r in records:
        value = ci_get(r, "version", "versionNumber", "currentVersion", "publishedVersion", "latestVersion")
        if value is not None:
            return value
    return None


def load_previous(path):
    """Return {template id: record} from an earlier run's output, so those templates are skipped."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return {r["id"]: r for r in json.load(f) if isinstance(r, dict) and r.get("id")}
    except (ValueError, TypeError):
        print(f"[WARNING] {path} could not be read; starting over.\n")
        return {}


def save_combined(path, order, results):
    """Write the records in discovery order."""
    ordered = [results[tid] for tid in order if tid in results]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=4, ensure_ascii=False)


def write_per_template(out_dir, records):
    """Write one file per template, named safely and uniquely. Returns the count written."""
    os.makedirs(out_dir, exist_ok=True)
    used = set()
    for record in records:
        stem = role_stem(record.get("name"), record.get("id"), used)
        with open(os.path.join(out_dir, stem + ".json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=4, ensure_ascii=False)
    return len(records)


def load_results(passphrase):
    """Return ({template id: record}, settings) from a previous run. Decrypts when a passphrase is given."""
    if passphrase:
        enc_path = os.path.join(HERE, ENC_FILE)
        if os.path.exists(enc_path):
            bundle = otc.decrypt_file_to_json(enc_path, passphrase)   # raises ValueError on wrong passphrase
            results = {r["id"]: r for r in bundle.get("templates", []) if isinstance(r, dict) and r.get("id")}
            return results, bundle.get("settings")
        return {}, None
    return load_previous(os.path.join(HERE, OUTPUT_FILE)), None


def save_results(order, results, settings, passphrase):
    """Persist the resume artifact: one encrypted bundle, or the plaintext combined file."""
    ordered = [results[tid] for tid in order if tid in results]
    if passphrase:
        otc.encrypt_json_to_file(os.path.join(HERE, ENC_FILE),
                                 {"templates": ordered, "settings": settings}, passphrase)
    else:
        save_combined(os.path.join(HERE, OUTPUT_FILE), order, results)


# ------------------------------ export flow (uses the reused browser session) ------------------------------

def list_templates(sess):
    """Fetch every template summary across LIST_QUERIES, de-duplicated by id, in first-seen order."""
    templates, seen, raw = [], set(), {}
    for query in LIST_QUERIES:
        url = f"{sess.base}/api/template/v1/templates/details?{query}"
        status, data = sess.api_get(url)
        raw[query] = data
        print(f"  [DIAG] templates/details?{query} -> status={status}, shape={describe_shape(data)}")
        items = extract_templates(data)
        if items is None:
            print(f"  [WARNING] list ({query}) shape not recognized; see {STRUCTURE_FILE} (no values/IDs).")
            continue
        for item in items:
            tid = template_id(item)
            if tid and tid not in seen:
                seen.add(tid)
                templates.append(item)
    # Privacy-safe diagnostic: structure only - field names, types and list lengths, NO values/IDs/PII.
    # Safe to share. Raw response bodies are written ONLY if you explicitly set SAVE_RAW = True.
    try:
        shapes = {query: structure(data) for query, data in raw.items()}
        with open(os.path.join(HERE, STRUCTURE_FILE), "w", encoding="utf-8") as f:
            json.dump(shapes, f, indent=2, ensure_ascii=False)
    except OSError:
        pass
    if SAVE_RAW:
        try:
            with open(os.path.join(HERE, RAW_LIST_FILE), "w", encoding="utf-8") as f:
                f.write(obe.redact(json.dumps(raw, indent=2, ensure_ascii=False)))
        except OSError:
            pass
    return templates


def fetch_template(sess, summary):
    """Fetch one template's full record (details, content, actions, translations, notifications)."""
    base, tid = sess.base, template_id(summary)
    _, details = sess.api_get(f"{base}/api/template/v1/templates/{tid}/details")
    _, actions = sess.api_get(f"{base}/api/template/v1/templates/{tid}/actions")
    _, translations = sess.api_get(f"{base}/api/template/v1/templates/{tid}/translations")
    _, notifications = sess.api_get(
        f"{base}/api/template/v1/custom-notifications/templates/{tid}"
        f"?page=0&size={NOTIF_PAGE_SIZE}&sort=lastModifiedDate,desc")

    rvid, version = root_version_id(summary, details), version_number(summary, details)
    content = None
    if rvid and version is not None:
        _, content = sess.api_get(f"{base}/api/template/v1/templates/rootversion/{rvid}?version={version}")
    else:
        print("   [NOTE] no rootVersionId/version found for this template; questionnaire content skipped.")

    return {
        "id": tid,
        "name": template_name(summary),
        "summary": summary,
        "details": details,
        "content": content,
        "actions": actions,
        "translations": translations,
        "notifications": notifications,
    }


def run_export(sess, passphrase):
    results, prev_settings = load_results(passphrase)

    _, settings = sess.api_get(f"{sess.base}/api/template/v1/settings")
    if settings is None:
        settings = prev_settings

    print("Listing PIA/DPIA templates ...")
    templates = list_templates(sess)
    order = [template_id(t) for t in templates if template_id(t)]
    todo = [t for t in templates if template_id(t) not in results]
    print(f"{len(templates)} template(s); {len(templates) - len(todo)} already saved, {len(todo)} to fetch.\n")
    if not templates:
        print(f"[NOTE] No templates came back. Open {STRUCTURE_FILE} (shapes only, no values/IDs) and paste me\n"
              f"the one [DIAG] line above. If it shows list[0], the logged-in account can't see these PIA\n"
              f"templates here - log in as the account that sees them in the browser window and rerun.\n")

    failed = []
    for i, summary in enumerate(todo, 1):
        tid, name = template_id(summary), template_name(summary)
        if not tid:
            print(f"[{i}/{len(todo)}] SKIP {name}: no id")
            failed.append(name)
            continue
        print(f"[{i}/{len(todo)}] {name} ...")
        try:
            results[tid] = fetch_template(sess, summary)
            save_results(order, results, settings, passphrase)
        except obe.PlaywrightError as e:
            print(f"   [ERROR] {obe.redact(e)}")
            failed.append(name)
        if i < len(todo):
            sess._sleep(REQUEST_DELAY)

    save_results(order, results, settings, passphrase)
    ordered = [results[tid] for tid in order if tid in results]
    if passphrase:
        print(f"\nSaved {len(results)}/{len(templates)} template(s), encrypted, to {os.path.join(HERE, ENC_FILE)}")
        print("Decrypt it with: python onetrust_decrypt.py")
    else:
        written = write_per_template(os.path.join(HERE, PER_TEMPLATE_DIR), ordered)
        if settings is not None:
            with open(os.path.join(HERE, SETTINGS_FILE), "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
        print(f"\nSaved {len(results)}/{len(templates)} template(s) to {os.path.join(HERE, OUTPUT_FILE)}")
        print(f"Split into {written} per-template file(s) under {os.path.join(HERE, PER_TEMPLATE_DIR)}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}. Run again to retry them.")
    if any(r.get("content") is None for r in results.values()):
        print("\n[NOTE] Some templates have no questionnaire content (rootVersionId/version not found in the "
              "list/details response). Send me one template's '/details' response and I'll map the field names.")


def main():
    if "yourtenant" in DOMAIN or not DOMAIN:
        sys.exit("[CONFIG] Set ONETRUST_DOMAIN at the top of the script to your tenant host, e.g. app-de.onetrust.com")

    passphrase = None
    if ENCRYPT_OUTPUT:
        if not otc.available():
            sys.exit("[SETUP] Encryption needs the cryptography package. Run:\n  pip install cryptography\n"
                     "(or set ENCRYPT_OUTPUT = False at the top to write plaintext instead).")
        enc_path = os.path.join(HERE, ENC_FILE)
        passphrase = otc.prompt_passphrase(confirm=not os.path.exists(enc_path))
        if os.path.exists(enc_path):   # fail fast if the passphrase doesn't match the existing file
            try:
                otc.decrypt_file_to_json(enc_path, passphrase)
            except ValueError as e:
                sys.exit(f"[ENCRYPT] {e}. Delete {ENC_FILE} to start over, or rerun with the right passphrase.")

    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError:
        sys.exit("[SETUP] Playwright isn't installed. Run:\n  pip install playwright\n"
                 "  playwright install chromium   (only needed if Google Chrome isn't installed)")

    obe.PlaywrightError = Error   # let the reused session catch Playwright errors precisely

    with sync_playwright() as pw:
        sess = obe.BrowserExporter(pw)
        sess.domain, sess.base = DOMAIN, f"https://{DOMAIN}"   # point the reused session at this tenant
        sess.start()
        try:
            if not sess.wait_for_login(initial=True):
                sys.exit("Gave up waiting for a OneTrust session. Run again and log in when the window opens.")
            run_export(sess, passphrase)
        finally:
            sess.close()


if __name__ == "__main__":
    main()
