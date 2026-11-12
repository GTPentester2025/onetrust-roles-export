"""Export PIA/DPIA assessment-template definitions via OneTrust's OFFICIAL, documented API. READ-ONLY.

This is the supported path (no browser, no reverse-engineered endpoints). It uses the published
Assessment-Automation API with a read-only OAuth2 client (onetrust_api.py):

    GET /api/template/v1/published-template-metadata?template-types=PIA        (list; PIA covers DPIA)
  then per template {rootVersionId}:
    GET /api/template/v2/templates/{rootVersionId}/export?templateVersion=latest   (full definition)

Every call is a GET - it cannot create, change or delete anything. The export endpoint returns the full
template metadata (questions, rules, skip/show logic) - the same "Full definitions" you get from the UI's
template export, in JSON.

SCOPE OF THIS SCRIPT: the official list returns PUBLISHED templates, and the API has no per-template
notifications endpoint. So the Archive and Notifications sub-tabs are NOT covered here - keep using
onetrust_assessment_export.py (browser-driven) for those two.

Credentials: create a read-only client under OneTrust Global Settings > Access Management > Client
Credentials (scope ASSESSMENT_READ) and provide them via the environment:
    PowerShell:  $env:ONETRUST_CLIENT_ID = "..."; $env:ONETRUST_CLIENT_SECRET = "..."
    cmd:         set ONETRUST_CLIENT_ID=...   &   set ONETRUST_CLIENT_SECRET=...
If unset, you're prompted (hidden). No new pip packages are needed - this uses the standard library plus,
for the encrypted output, the `cryptography` package already used by the other exporter.

Run: python onetrust_templates_api_export.py
Output is encrypted at rest by default (see onetrust_crypto.py); decrypt with onetrust_decrypt.py, or
sanitize for sharing with onetrust_sanitize.py.
"""
import json
import os
import sys
import time
import urllib.parse

import onetrust_api as api
import onetrust_crypto as otc
from split_roles import role_stem
# Shared pure helpers (already unit-tested in test_onetrust_assessment_export). Importing this module does
# NOT require Playwright - the browser module keeps that import inside its main().
from onetrust_assessment_export import structure, describe_shape, ci_get, extract_templates

# ============================ CONFIGURATION ============================
ONETRUST_DOMAIN = "app-de.onetrust.com"   # your tenant host (no https://)
TEMPLATE_TYPES = ["PIA"]                   # PIA covers PIA & DPIA Automation. Others: CONTROL, VENDOR, ...
TEMPLATE_VERSION = "latest"                # export the newest published version of each template
# ======================================================================

SCOPE = "ASSESSMENT_READ"
ENCRYPT_OUTPUT = True
ENC_FILE = "assessment_templates_api_export.enc"
OUTPUT_FILE = "assessment_templates_api_full.json"      # plaintext, only when ENCRYPT_OUTPUT = False
PER_TEMPLATE_DIR = "assessment_templates_api"
STRUCTURE_FILE = "assessment_templates_api_list_structure.json"   # shapes only, NO values/IDs/PII
REQUEST_DELAY = 0.2

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = ONETRUST_DOMAIN.replace("https://", "").replace("http://", "").strip("/")


# ------------------------------ pure helpers (unit-tested) ------------------------------

def template_ref(record):
    """The identifier to pass to the export endpoint: the template's root version id."""
    return ci_get(record, "rootVersionId", "templateRootVersionId", "rootVersion", "id")


def template_name(record):
    return ci_get(record, "name", "templateName", "title") or "<unnamed>"


def template_type(record):
    return ci_get(record, "templateType", "type")


def list_params(types):
    return {"template-types": list(types)}


def export_params(version):
    return {"templateVersion": version} if version else {}


def dedupe_by_ref(items):
    """Return (unique template summaries, order of refs) keeping first-seen order, de-duplicated by ref."""
    templates, seen, order = [], set(), []
    for item in items or []:
        ref = template_ref(item)
        if ref and ref not in seen:
            seen.add(ref)
            templates.append(item)
            order.append(ref)
    return templates, order


def build_record(summary, export):
    """One combined record per template."""
    return {
        "rootVersionId": template_ref(summary),
        "name": template_name(summary),
        "templateType": template_type(summary),
        "summary": summary,
        "export": export,
    }


# ------------------------------ resume (encrypted or plaintext) ------------------------------

def load_results(passphrase):
    """Return {rootVersionId: record} from an earlier run, so those templates are skipped on rerun."""
    if passphrase:
        enc_path = os.path.join(HERE, ENC_FILE)
        if os.path.exists(enc_path):
            bundle = otc.decrypt_file_to_json(enc_path, passphrase)   # ValueError on wrong passphrase
            recs = bundle.get("templates", []) if isinstance(bundle, dict) else []
            return {r["rootVersionId"]: r for r in recs if isinstance(r, dict) and r.get("rootVersionId")}
        return {}
    path = os.path.join(HERE, OUTPUT_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    return {r["rootVersionId"]: r for r in data if isinstance(r, dict) and r.get("rootVersionId")}


def save_results(order, results, passphrase):
    ordered = [results[ref] for ref in order if ref in results]
    if passphrase:
        otc.encrypt_json_to_file(os.path.join(HERE, ENC_FILE), {"templates": ordered}, passphrase)
    else:
        with open(os.path.join(HERE, OUTPUT_FILE), "w", encoding="utf-8") as f:
            json.dump(ordered, f, indent=4, ensure_ascii=False)


def write_per_template(out_dir, records):
    os.makedirs(out_dir, exist_ok=True)
    used = set()
    for record in records:
        stem = role_stem(record.get("name"), record.get("rootVersionId"), used)
        with open(os.path.join(out_dir, stem + ".json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=4, ensure_ascii=False)
    return len(records)


def _write_structure(data):
    """Privacy-safe diagnostic: field names, types and counts only - NO values/IDs/PII. Safe to share."""
    try:
        with open(os.path.join(HERE, STRUCTURE_FILE), "w", encoding="utf-8") as f:
            json.dump(structure(data), f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _status_hint(status):
    return {401: " (token/credentials rejected)",
            403: " (missing scope - the client needs ASSESSMENT_READ)",
            429: " (rate limited - rerun to resume)"}.get(status, "")


# ------------------------------ export flow ------------------------------

def run_export(client, passphrase):
    results = load_results(passphrase)

    status, data = client.get("/api/template/v1/published-template-metadata", list_params(TEMPLATE_TYPES))
    print(f"  [DIAG] published-template-metadata?template-types={TEMPLATE_TYPES} -> "
          f"status={status}, shape={describe_shape(data)}")
    _write_structure(data)

    items = extract_templates(data)
    if items is None:
        print(f"  [ERROR] list call returned HTTP {status}{_status_hint(status)}; shape not a template list. "
              f"See {STRUCTURE_FILE} (shapes only, no values).")
        items = []

    templates, order = dedupe_by_ref(items)
    todo = [t for t in templates if template_ref(t) not in results]
    print(f"{len(templates)} template(s); {len(templates) - len(todo)} already saved, {len(todo)} to fetch.\n")
    if not templates and status == 200:
        print("[NOTE] The API returned no published PIA templates for this client. Check that the client's "
              "scope can see PIA & DPIA Automation templates, or that any exist.\n")

    failed = []
    for i, summary in enumerate(todo, 1):
        ref, name = template_ref(summary), template_name(summary)
        print(f"[{i}/{len(todo)}] {name} ...")
        st, export = client.get(f"/api/template/v2/templates/{urllib.parse.quote(str(ref), safe='')}/export",
                                 export_params(TEMPLATE_VERSION))
        if st != 200:
            snippet = export if isinstance(export, str) else json.dumps(export)
            print(f"   [ERROR] export HTTP {st}{_status_hint(st)}: {api.redact(snippet)[:160]}")
            failed.append(name)
            continue
        results[ref] = build_record(summary, export)
        save_results(order, results, passphrase)
        if i < len(todo):
            time.sleep(REQUEST_DELAY)

    save_results(order, results, passphrase)
    ordered = [results[ref] for ref in order if ref in results]
    if passphrase:
        print(f"\nSaved {len(results)}/{len(templates)} template(s), encrypted, to {os.path.join(HERE, ENC_FILE)}")
        print("Decrypt with: python onetrust_decrypt.py " + ENC_FILE
              + "   |   Sanitize to share: python onetrust_sanitize.py " + ENC_FILE)
    else:
        written = write_per_template(os.path.join(HERE, PER_TEMPLATE_DIR), ordered)
        print(f"\nSaved {len(results)}/{len(templates)} template(s) to {os.path.join(HERE, OUTPUT_FILE)}")
        print(f"Split into {written} per-template file(s) under {os.path.join(HERE, PER_TEMPLATE_DIR)}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}. Run again to retry them.")


def main():
    if "yourtenant" in DOMAIN or not DOMAIN:
        sys.exit("[CONFIG] Set ONETRUST_DOMAIN at the top of the script, e.g. app-de.onetrust.com")

    passphrase = None
    if ENCRYPT_OUTPUT:
        if not otc.available():
            sys.exit("[SETUP] Encryption needs the cryptography package. Run:\n  pip install cryptography\n"
                     "(or set ENCRYPT_OUTPUT = False at the top to write plaintext instead).")
        enc_path = os.path.join(HERE, ENC_FILE)
        passphrase = otc.prompt_passphrase(confirm=not os.path.exists(enc_path))
        if os.path.exists(enc_path):
            try:
                otc.decrypt_file_to_json(enc_path, passphrase)
            except ValueError as e:
                sys.exit(f"[ENCRYPT] {e}. Delete {ENC_FILE} to start over, or rerun with the right passphrase.")

    try:
        client_id, client_secret = api.load_credentials(prompt=True)
    except ValueError as e:
        sys.exit(f"[AUTH] {e}")

    client = api.ApiClient(DOMAIN, client_id, client_secret, scope=SCOPE)
    try:
        client.token()   # fail fast with a clear message if credentials/scope are wrong
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[AUTH] could not get a token: {api.redact(e, client_secret)}")

    run_export(client, passphrase)


if __name__ == "__main__":
    main()
