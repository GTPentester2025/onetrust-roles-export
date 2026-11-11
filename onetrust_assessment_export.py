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
import sys

import onetrust_browser_export as obe   # reuse the browser session (login, token capture, api_get)
from split_roles import role_stem       # reuse safe, unique per-record filenames

# ============================ CONFIGURATION ============================
ONETRUST_DOMAIN = "app-de.onetrust.com"   # your tenant host (prefilled from discovery; no https://)
# Each entry is a query string for the templates list. PIA covers PIA & DPIA. If archived templates
# turn out to need their own filter, add it here as another entry, e.g. "type=PIA&status=ARCHIVED";
# templates are de-duplicated by id across entries.
LIST_QUERIES = ["type=PIA"]
# ======================================================================

OUTPUT_FILE = "assessment_templates_full.json"
PER_TEMPLATE_DIR = "assessment_templates"
SETTINGS_FILE = "assessment_templates_settings.json"
NOTIF_PAGE_SIZE = 200
REQUEST_DELAY = 0.3

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = ONETRUST_DOMAIN.replace("https://", "").replace("http://", "").strip("/")


# ------------------------------ pure helpers (no browser; unit-tested) ------------------------------

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


# ------------------------------ export flow (uses the reused browser session) ------------------------------

def list_templates(sess):
    """Fetch every template summary across LIST_QUERIES, de-duplicated by id, in first-seen order."""
    templates, seen = [], set()
    for query in LIST_QUERIES:
        _, data = sess.api_get(f"{sess.base}/api/template/v1/templates/details?{query}")
        items = extract_templates(data)
        if items is None:
            print(f"  [WARNING] list ({query}) returned an unexpected shape; skipping it.")
            continue
        for item in items:
            tid = template_id(item)
            if tid and tid not in seen:
                seen.add(tid)
                templates.append(item)
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


def run_export(sess):
    out_path = os.path.join(HERE, OUTPUT_FILE)
    results = load_previous(out_path)

    _, settings = sess.api_get(f"{sess.base}/api/template/v1/settings")
    if settings is not None:
        with open(os.path.join(HERE, SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)

    print("Listing PIA/DPIA templates ...")
    templates = list_templates(sess)
    order = [template_id(t) for t in templates if template_id(t)]
    todo = [t for t in templates if template_id(t) not in results]
    print(f"{len(templates)} template(s); {len(templates) - len(todo)} already saved, {len(todo)} to fetch.\n")

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
            save_combined(out_path, order, results)
        except obe.PlaywrightError as e:
            print(f"   [ERROR] {obe.redact(e)}")
            failed.append(name)
        if i < len(todo):
            sess._sleep(REQUEST_DELAY)

    save_combined(out_path, order, results)
    ordered = [results[tid] for tid in order if tid in results]
    written = write_per_template(os.path.join(HERE, PER_TEMPLATE_DIR), ordered)
    print(f"\nSaved {len(results)}/{len(templates)} template(s) to {out_path}")
    print(f"Split into {written} per-template file(s) under {os.path.join(HERE, PER_TEMPLATE_DIR)}")
    if settings is not None:
        print(f"Module settings saved to {os.path.join(HERE, SETTINGS_FILE)}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}. Run again to retry them.")
    if any(r.get("content") is None for r in results.values()):
        print("\n[NOTE] Some templates have no questionnaire content (rootVersionId/version not found in the "
              "list/details response). Send me one template's '/details' response and I'll map the field names.")


def main():
    if "yourtenant" in DOMAIN or not DOMAIN:
        sys.exit("[CONFIG] Set ONETRUST_DOMAIN at the top of the script to your tenant host, e.g. app-de.onetrust.com")
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
            run_export(sess)
        finally:
            sess.close()


if __name__ == "__main__":
    main()
