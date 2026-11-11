"""Decrypt an export produced by onetrust_assessment_export.py (passphrase-protected, encrypted at rest).

Prompts for the passphrase, decrypts assessment_templates_export.enc, and writes the readable results:
  assessment_templates_full.json         - all templates (one array)
  assessment_templates/                  - one file per template
  assessment_templates_settings.json     - module settings
These decrypted files contain your real data; they're git-ignored. Delete them when you're done.

Usage:
    python onetrust_decrypt.py                 # decrypt assessment_templates_export.enc
    python onetrust_decrypt.py FILE.enc        # decrypt a specific file
    python onetrust_decrypt.py FILE.enc OUTDIR # ... writing outputs into OUTDIR
"""
import json
import os
import sys

import onetrust_crypto as otc
from split_roles import role_stem

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENC = "assessment_templates_export.enc"


def main():
    if not otc.available():
        sys.exit("[SETUP] Decryption needs the cryptography package. Run:\n  pip install cryptography")

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    enc_path = args[0] if len(args) >= 1 else os.path.join(HERE, DEFAULT_ENC)
    out_dir = args[1] if len(args) >= 2 else HERE
    if not os.path.exists(enc_path):
        sys.exit(f"[INPUT] {enc_path} not found. Run onetrust_assessment_export.py first.")

    passphrase = otc.prompt_passphrase(confirm=False)
    try:
        bundle = otc.decrypt_file_to_json(enc_path, passphrase)
    except ValueError as e:
        sys.exit(f"[DECRYPT] {e}")

    templates = bundle.get("templates", []) if isinstance(bundle, dict) else []
    settings = bundle.get("settings") if isinstance(bundle, dict) else None

    os.makedirs(out_dir, exist_ok=True)
    full_path = os.path.join(out_dir, "assessment_templates_full.json")
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=4, ensure_ascii=False)

    per_dir = os.path.join(out_dir, "assessment_templates")
    os.makedirs(per_dir, exist_ok=True)
    used = set()
    for record in templates:
        if not isinstance(record, dict):
            continue
        stem = role_stem(record.get("name"), record.get("id"), used)
        with open(os.path.join(per_dir, stem + ".json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=4, ensure_ascii=False)

    if settings is not None:
        with open(os.path.join(out_dir, "assessment_templates_settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)

    print(f"Decrypted {len(templates)} template(s) to {full_path}")
    print(f"Split into {per_dir}")
    print("[REMINDER] These files are plaintext (git-ignored). Delete them when you're done.")


if __name__ == "__main__":
    main()
