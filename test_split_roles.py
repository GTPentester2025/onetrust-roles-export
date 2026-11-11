"""Tests for split_roles (no network, no browser).

Run: python test_split_roles.py
"""
import json
import os
import tempfile

import split_roles as mod


def test_sanitize_strips_illegal_chars():
    assert mod.sanitize_filename('a/b:c*d?"e') == "a_b_c_d__e"


def test_sanitize_collapses_whitespace_and_trims_dots():
    assert mod.sanitize_filename("  Privacy   Admin . ") == "Privacy Admin"


def test_sanitize_reserved_name_is_escaped():
    assert mod.sanitize_filename("CON") == "_CON"


def test_sanitize_empty_uses_fallback():
    assert mod.sanitize_filename("", fallback="role") == "role"
    assert mod.sanitize_filename("   ", fallback="role") == "role"   # whitespace-only -> fallback
    assert mod.sanitize_filename("...") == ""                        # dots-only -> empty (no fallback given)
    assert mod.sanitize_filename("///") == "___"                     # slashes become underscores, not removed


def test_role_stem_unique_by_name():
    used = set()
    assert mod.role_stem("Admin", "id1", used) == "Admin"
    # same name, different id -> id appended
    assert mod.role_stem("Admin", "id2", used) == "Admin-id2"
    # same name, no id -> counter
    assert mod.role_stem("Admin", None, used) == "Admin (2)"


def test_role_stem_empty_name_falls_back_to_id():
    used = set()
    assert mod.role_stem("", "abc123", used) == "abc123"


def test_split_writes_one_file_per_role():
    roles = [
        {"id": "1", "name": "Privacy Admin", "accessible_permissions": {"p": [1]}},
        {"id": "2", "name": "Privacy Admin", "accessible_permissions": {"p": [2]}},  # dup name
        {"id": "3", "name": "Auditor/EU", "accessible_permissions": {}},             # slash in name
    ]
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "full.json")
        out = os.path.join(d, "roles")
        with open(src, "w", encoding="utf-8") as f:
            json.dump(roles, f)
        written, skipped = mod.split_roles(src, out)
        assert written == 3 and skipped == 0
        files = sorted(os.listdir(out))
        assert files == ["Auditor_EU.json", "Privacy Admin-2.json", "Privacy Admin.json"]
        # content is the whole role record, unchanged
        with open(os.path.join(out, "Privacy Admin.json"), encoding="utf-8") as f:
            assert json.load(f)["accessible_permissions"] == {"p": [1]}


def test_split_skips_non_dict_entries():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "full.json")
        out = os.path.join(d, "roles")
        with open(src, "w", encoding="utf-8") as f:
            json.dump([{"id": "1", "name": "Ok"}, "junk", 123], f)
        written, skipped = mod.split_roles(src, out)
        assert written == 1 and skipped == 2
        assert os.listdir(out) == ["Ok.json"]


def test_extract_roles_shapes():
    assert mod.extract_roles([{"id": "1"}]) == [{"id": "1"}]
    assert mod.extract_roles({"content": [{"id": "1"}]}) == [{"id": "1"}]
    assert mod.extract_roles({"oops": 1}) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
