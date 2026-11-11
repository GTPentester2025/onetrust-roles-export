"""Split the combined export into one JSON file per role, in a subfolder.

Takes onetrust_roles_full.json (the array written by onetrust_browser_export.py) and writes each role
to its own file under a subfolder (default: roles/). Each file is named after the role's name, with
its id appended only when needed to keep names unique and filesystem-safe.

Standard library only: no pip install needed (Python 3.8+). Read-only toward OneTrust: it only reads
a local file and writes local files, and never contacts the network.

Usage:
    python split_roles.py                        # roles/  from onetrust_roles_full.json
    python split_roles.py INPUT.json             # custom input, into roles/
    python split_roles.py INPUT.json OUT_DIR     # custom input and output folder
"""
import json
import os
import re
import sys

DEFAULT_INPUT = "onetrust_roles_full.json"
DEFAULT_OUTPUT_DIR = "roles"

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows reserved device names: a file can't be named any of these (case-insensitive).
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def sanitize_filename(name, fallback=""):
    """Turn an arbitrary role name into a safe filename stem (no extension)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or ""))   # drop path separators / illegal chars
    name = re.sub(r"\s+", " ", name).strip()                        # collapse whitespace
    name = name[:100].strip(" .")                                   # cap length; no leading/trailing dots or spaces
    if name.lower() in _RESERVED:
        name = "_" + name
    return name or fallback


def role_stem(name, role_id, used):
    """Return a unique filename stem for a role, recording it in `used` (a set of lowercased stems).
    Uses the role name; appends the id, then a counter, only to resolve a clash or an empty name."""
    base = sanitize_filename(name) or sanitize_filename(role_id, fallback="role")
    stem = base
    if stem.lower() in used and role_id:
        stem = f"{base}-{sanitize_filename(role_id)}"
    counter = 2
    while stem.lower() in used:
        stem = f"{base} ({counter})"
        counter += 1
    used.add(stem.lower())
    return stem


def extract_roles(data):
    """Pull the list of role records out of the loaded JSON (a bare array, or a page object)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("content"), list):
        return data["content"]
    return None


def split_roles(input_path, out_dir):
    """Write one file per role. Returns (written, skipped). Raises SystemExit on input problems."""
    try:
        with open(input_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"[INPUT] {input_path} not found. Run the export first, or pass the file path as an argument.")
    except ValueError as e:
        sys.exit(f"[INPUT] {input_path} is not valid JSON ({e}).")

    roles = extract_roles(data)
    if roles is None:
        sys.exit(f"[INPUT] {input_path} isn't a list of roles (expected a JSON array).")
    if not roles:
        sys.exit(f"[INPUT] {input_path} has no roles in it.")

    os.makedirs(out_dir, exist_ok=True)
    used, written, skipped = set(), 0, 0
    for i, role in enumerate(roles, 1):
        if not isinstance(role, dict):
            print(f"[{i}/{len(roles)}] SKIP: entry is not a role object")
            skipped += 1
            continue
        stem = role_stem(role.get("name"), role.get("id"), used)
        path = os.path.join(out_dir, stem + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(role, f, indent=4, ensure_ascii=False)
        written += 1
    return written, skipped


def main():
    args = [a for a in sys.argv[1:] if a not in ("-h", "--help")]
    if len(sys.argv) > 1 and len(args) != len(sys.argv) - 1:
        print(__doc__)
        return
    input_path = args[0] if len(args) >= 1 else os.path.join(HERE, DEFAULT_INPUT)
    out_dir = args[1] if len(args) >= 2 else os.path.join(HERE, DEFAULT_OUTPUT_DIR)

    written, skipped = split_roles(input_path, out_dir)
    print(f"\nWrote {written} role file(s) to {os.path.abspath(out_dir)}")
    if skipped:
        print(f"Skipped {skipped} entr(y/ies) that weren't role objects.")


if __name__ == "__main__":
    main()
