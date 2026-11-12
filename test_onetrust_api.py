"""Tests for onetrust_api (the read-only OAuth2 client). Pure helpers + token caching; no network.

Run: python test_onetrust_api.py
"""
import os

import onetrust_api as api


def test_token_form_has_client_credentials_fields():
    body = api.token_form("cid", "sec", "ASSESSMENT_READ").decode("utf-8")
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "client_secret=sec" in body
    assert "scope=ASSESSMENT_READ" in body


def test_parse_token_response_tolerant():
    assert api.parse_token_response({"access_token": "abc", "expires_in": 3600}) == ("abc", 3600)
    # missing expires_in -> None (caller supplies a default)
    assert api.parse_token_response({"access_token": "abc"}) == ("abc", None)
    assert api.parse_token_response({"nope": 1}) == (None, None)


def test_redact_removes_secret_and_tokens():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig12345"
    msg = f"failed secret=topsecret token={jwt}"
    out = api.redact(msg, "topsecret")
    assert "topsecret" not in out
    assert jwt not in out
    assert "[REDACTED" in out


def test_load_credentials_from_env(monkeypatch=None):
    os.environ["ONETRUST_CLIENT_ID"] = "envid"
    os.environ["ONETRUST_CLIENT_SECRET"] = "envsecret"
    try:
        cid, sec = api.load_credentials(prompt=False)
        assert (cid, sec) == ("envid", "envsecret")
    finally:
        del os.environ["ONETRUST_CLIENT_ID"]
        del os.environ["ONETRUST_CLIENT_SECRET"]


def test_load_credentials_missing_raises_without_prompt():
    for k in ("ONETRUST_CLIENT_ID", "ONETRUST_CLIENT_SECRET"):
        os.environ.pop(k, None)
    try:
        api.load_credentials(prompt=False)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_token_is_cached_and_refreshes_on_expiry():
    clock = {"t": 1000.0}
    client = api.ApiClient("host", "cid", "sec", now=lambda: clock["t"])
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return f"tok{calls['n']}", 3600
    client._fetch_token = fake_fetch

    assert client.token() == "tok1"
    assert client.token() == "tok1"        # cached, no second fetch
    assert calls["n"] == 1
    clock["t"] += 3600                       # past expiry (minus skew)
    assert client.token() == "tok2"          # refreshed
    assert calls["n"] == 2


def test_client_has_no_write_methods():
    # structural read-only guarantee: no post/put/patch/delete on the client
    for name in ("post", "put", "patch", "delete"):
        assert not hasattr(api.ApiClient, name), f"ApiClient must not expose {name}()"


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
