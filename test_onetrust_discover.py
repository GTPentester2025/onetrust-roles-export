"""Tests for the pure logic in onetrust_discover (no browser, no Playwright needed).

Run: python test_onetrust_discover.py
"""
import onetrust_discover as mod


# ----- masking (the part the user specifically asked to be sure of) -----

def test_mask_jwt_in_body():
    body = '{"token": "eyJhbGciOiJ.eyJleHAiOjE5.sig", "name": "PIA Template"}'
    out = mod.mask(body)
    assert "eyJhbGciOiJ.eyJleHAiOjE5.sig" not in out
    assert "[MASKED-JWT]" in out
    assert "PIA Template" in out   # real data preserved


def test_mask_bearer_and_cookie():
    out = mod.mask("Authorization: Bearer abc.def.ghi\nCookie: __Secure-fgpt=secretval; a=b")
    assert "abc.def.ghi" not in out and "secretval" not in out
    assert "[MASKED]" in out


def test_mask_token_in_query_string():
    out = mod.mask("https://x.onetrust.com/api/template/v1/list?access_token=supersecret&type=PIA")
    assert "supersecret" not in out
    assert "type=PIA" in out       # non-secret params preserved


def test_mask_json_field_values():
    out = mod.mask('{"client_secret":"topsecret","id_token":"zzz","title":"keep me"}')
    for leaked in ("topsecret", "zzz"):
        assert leaked not in out
    assert "keep me" in out


def test_mask_keeps_ordinary_text():
    assert mod.mask("GET /api/template/v1/published-template-metadata -> 200") == \
        "GET /api/template/v1/published-template-metadata -> 200"


# ----- URL / filename helpers -----

def test_split_url():
    base, query = mod.split_url("https://x.onetrust.com/api/template/v1/list?type=PIA&page=0")
    assert base == "https://x.onetrust.com/api/template/v1/list"
    assert query == "type=PIA&page=0"


def test_response_filename_is_safe_and_informative():
    name = mod.response_filename(7, "GET", "https://x.onetrust.com/api/template/v1/published-template-metadata?type=PIA")
    assert name.startswith("0007_GET_")
    assert name.endswith(".json")
    assert "/" not in name and "?" not in name


# ----- endpoint de-duplication -----

def test_dedupe_endpoints_groups_and_summarizes():
    records = [
        {"method": "GET", "url": "https://x.onetrust.com/api/template/v1/list?type=PIA&page=0",
         "status": 200, "content_type": "application/json;charset=UTF-8"},
        {"method": "GET", "url": "https://x.onetrust.com/api/template/v1/list?type=PIA&page=1",
         "status": 200, "content_type": "application/json"},
        {"method": "POST", "url": "https://x.onetrust.com/api/assessment/v2/search",
         "status": 200, "content_type": "application/json", "post_data": '{"q":"x"}'},
    ]
    endpoints = mod.dedupe_endpoints(records)
    assert len(endpoints) == 2   # the two GET /list calls collapse into one
    by_path = {e["path"]: e for e in endpoints}
    listing = by_path["https://x.onetrust.com/api/template/v1/list"]
    assert listing["count"] == 2
    assert listing["statuses"] == [200]
    assert listing["content_types"] == ["application/json"]
    assert listing["sample_query"] == "type=PIA&page=0"
    assert by_path["https://x.onetrust.com/api/assessment/v2/search"]["sample_post_data"] == '{"q":"x"}'


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
