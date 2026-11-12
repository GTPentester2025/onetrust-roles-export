"""Tests for onetrust_sanitize. Pure functions, no browser/network.

Run: python test_onetrust_sanitize.py
"""
import json

import onetrust_sanitize as san

GUID_A = "0f8b6c1e-2a3d-4b5c-8d7e-1f2a3b4c5d6e"
GUID_B = "11112222-3333-4444-5555-666677778888"


def _sanitize(obj, **kw):
    return san.Sanitizer(**kw).sanitize(obj)


def test_guid_value_tokenized_and_consistent():
    out = _sanitize({"a": GUID_A, "b": GUID_A, "c": GUID_B})
    assert out["a"].startswith("<GUID_") and out["a"] == out["b"], out
    assert out["c"] != out["a"], "different guids must get different tokens"


def test_guid_relationship_preserved_across_keys():
    # the same id used as a value in two places keeps one shared token, so links stay visible
    out = _sanitize({"rootVersionId": GUID_A, "ref": {"templateRootVersionId": GUID_A}})
    assert out["rootVersionId"] == out["ref"]["templateRootVersionId"]


def test_email_tokenized():
    out = _sanitize({"owner": "jane.doe@example.com", "owner2": "jane.doe@example.com"})
    assert out["owner"].startswith("<EMAIL_") and out["owner"] == out["owner2"]


def test_guid_key_tokenized_but_plain_keys_kept():
    out = _sanitize({GUID_A: {"inner": 1}, "status": "x"})
    keys = list(out.keys())
    assert any(k.startswith("<GUID_") for k in keys), keys
    assert "status" in keys, "ordinary keys (the schema) must be kept"


def test_enum_locale_number_kept():
    out = _sanitize({"type": "PIA", "stage": "IN_PROGRESS", "locale": "en-US", "version": "3", "page": "200"})
    assert out["type"] == "PIA"
    assert out["stage"] == "IN_PROGRESS"
    assert out["locale"] == "en-US"
    assert out["version"] == "3"
    assert out["page"] == "200"


def test_date_collapsed():
    out = _sanitize({"created": "2026-11-12T08:30:00.000Z"})
    assert out["created"] == "<date>"


def test_free_text_length_only_by_default():
    out = _sanitize({"name": "Vendor Risk Review", "desc": "Contains PII of John Smith"})
    assert out["name"] == "<str:18>", out["name"]
    assert out["desc"].startswith("<str:") and "John" not in out["desc"]


def test_secret_masked():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEFghiJKLmnoPQRstuvWXyz0123456789_-"
    out = _sanitize({"token": jwt, "apiKey": "0123456789abcdef0123456789abcdef0123"})
    assert out["token"] == "<secret>", out["token"]
    assert out["apiKey"] == "<secret>", out["apiKey"]


def test_bool_number_null_kept_as_is():
    out = _sanitize({"active": True, "count": 7, "ratio": 1.5, "missing": None})
    assert out == {"active": True, "count": 7, "ratio": 1.5, "missing": None}


def test_nested_shape_preserved():
    out = _sanitize({"content": [{"id": GUID_A, "q": "What data?"}], "empty": []})
    assert isinstance(out["content"], list) and len(out["content"]) == 1
    assert out["content"][0]["id"].startswith("<GUID_")
    assert out["empty"] == []


def test_no_real_value_leaks_into_output():
    secret_text = "John Smith"
    out = _sanitize({"id": GUID_A, "email": "a@b.com", "name": secret_text, "jwt": "eyJx.eyJy.zzzz"})
    dumped = json.dumps(out)
    for leaked in (GUID_A, "a@b.com", secret_text, "eyJx.eyJy.zzzz"):
        assert leaked not in dumped, f"leaked: {leaked}"


def test_keep_text_mode_preserves_freetext_but_still_tokenizes_ids():
    out = _sanitize({"name": "Vendor Risk Review", "id": GUID_A, "email": "a@b.com"}, mask_free_text=False)
    assert out["name"] == "Vendor Risk Review"      # kept
    assert out["id"].startswith("<GUID_")           # ids still protected
    assert out["email"].startswith("<EMAIL_")       # emails still protected


def test_empty_string_kept():
    assert _sanitize({"x": ""}) == {"x": ""}


def test_sanitize_text_fallback_tokenizes_and_masks():
    # non-JSON input: still tokenize guids/emails and mask secrets
    s = san.Sanitizer()
    text = f"id={GUID_A} user=a@b.com Bearer eyJa.eyJb.cccc"
    out = s.sanitize_text(text)
    assert GUID_A not in out and "a@b.com" not in out
    assert "<GUID_" in out and "<EMAIL_" in out


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
