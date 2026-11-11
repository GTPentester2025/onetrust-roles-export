"""Tests for the pure logic in onetrust_browser_export (no browser, no Playwright needed).

Run: python test_onetrust_browser_export.py
"""
import base64
import json
import os
import tempfile
import time

import onetrust_browser_export as mod


def _make_jwt(exp):
    """Build a realistic three-part JWT (header.payload.signature, each base64url) with the given exp."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_redact_removes_jwt():
    token = _make_jwt(1893456000)
    out = mod.redact(f"HTTP 403 for token {token} denied")
    assert token not in out and "[REDACTED-JWT]" in out


def test_redact_removes_bearer_and_headers():
    out = mod.redact("Authorization: Bearer abc.def.ghi\nCookie: __Secure-fgpt=secretvalue; a=b")
    assert "abc.def.ghi" not in out
    assert "secretvalue" not in out
    assert "[REDACTED]" in out


def test_redact_removes_cookie_and_secret_pairs():
    out = mod.redact("__Secure-fgpt=xyz123; client_secret=topsecret; access_token=zzz")
    for leaked in ("xyz123", "topsecret", "zzz"):
        assert leaked not in out


def test_redact_keeps_ordinary_text():
    assert mod.redact("HTTP 500 Internal Server Error") == "HTTP 500 Internal Server Error"


def test_jwt_exp_reads_exp():
    assert mod.jwt_exp(_make_jwt(1893456000)) == 1893456000


def test_jwt_exp_handles_garbage():
    for bad in ["", "not-a-jwt", "a.b", "a.!!!.c", "a." + base64.urlsafe_b64encode(b"{}").decode() + ".c"]:
        assert mod.jwt_exp(bad) is None


def test_normalize_roles_list_page_object():
    roles, total = mod.normalize_roles_list({"content": [{"id": "1"}, {"id": "2"}], "totalPages": 3})
    assert [r["id"] for r in roles] == ["1", "2"]
    assert total == 3


def test_normalize_roles_list_bare_array():
    roles, total = mod.normalize_roles_list([{"id": "1"}])
    assert roles == [{"id": "1"}] and total is None


def test_normalize_roles_list_missing_total_pages():
    roles, total = mod.normalize_roles_list({"content": []})
    assert roles == [] and total is None


def test_normalize_roles_list_unexpected():
    roles, total = mod.normalize_roles_list({"oops": 1})
    assert roles is None and total is None


def test_merge_role_prefers_detail_and_attaches_permissions():
    rec = mod.merge_role({"id": "r1", "name": "Admin"},
                         {"id": "r1", "name": "Admin", "description": "full"},
                         {"permissions": ["a", "b"]})
    assert rec["description"] == "full"
    assert rec["accessible_permissions"] == {"permissions": ["a", "b"]}


def test_merge_role_falls_back_to_summary_when_detail_missing():
    rec = mod.merge_role({"id": "r2", "name": "Viewer"}, None, {"permissions": []})
    assert rec["id"] == "r2" and rec["name"] == "Viewer"
    assert rec["accessible_permissions"] == {"permissions": []}


def test_save_and_load_roundtrip_in_roles_order():
    roles = [{"id": "b"}, {"id": "a"}, {"id": "c"}]
    results = {"a": {"id": "a"}, "b": {"id": "b"}}  # inserted out of order; "c" not fetched yet
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.json")
        mod.save(path, roles, results)
        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        assert [r["id"] for r in on_disk] == ["b", "a"]  # roles-list order, only fetched ones
        assert mod.load_previous(path) == results


def test_load_previous_missing_file():
    with tempfile.TemporaryDirectory() as d:
        assert mod.load_previous(os.path.join(d, "nope.json")) == {}


def test_load_previous_skips_bad_entries():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"id": "a"}, {"no_id": 1}, "junk"], f)
        assert mod.load_previous(path) == {"a": {"id": "a"}}


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
        except Exception as e:  # noqa: BLE001 - surface any unexpected error per test
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
