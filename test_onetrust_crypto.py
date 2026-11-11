"""Tests for onetrust_crypto. The roundtrip tests need the cryptography package; they skip if absent.

Run: python test_onetrust_crypto.py
"""
import os
import tempfile

import onetrust_crypto as otc

HAVE_CRYPTO = otc.available()


def test_decrypt_rejects_non_magic_without_crypto():
    # bad-magic check happens before any crypto import, so this always runs
    try:
        otc.decrypt_bytes(b"not encrypted", "pw")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "magic" in str(e).lower()


def test_roundtrip():
    if not HAVE_CRYPTO:
        print("  (skipped: cryptography not installed)")
        return
    blob = otc.encrypt_bytes(b'{"hello": "world"}', "correct horse")
    assert blob.startswith(otc.MAGIC)
    assert otc.decrypt_bytes(blob, "correct horse") == b'{"hello": "world"}'


def test_wrong_passphrase_fails():
    if not HAVE_CRYPTO:
        print("  (skipped: cryptography not installed)")
        return
    blob = otc.encrypt_bytes(b"secret", "right")
    try:
        otc.decrypt_bytes(blob, "wrong")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "passphrase" in str(e).lower()


def test_distinct_salt_per_encryption():
    if not HAVE_CRYPTO:
        print("  (skipped: cryptography not installed)")
        return
    a = otc.encrypt_bytes(b"x", "pw")
    b = otc.encrypt_bytes(b"x", "pw")
    assert a != b   # random salt -> different ciphertext for the same input


def test_json_file_roundtrip():
    if not HAVE_CRYPTO:
        print("  (skipped: cryptography not installed)")
        return
    obj = {"templates": [{"id": "1", "name": "PIA"}], "settings": {"x": True}}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "export.enc")
        otc.encrypt_json_to_file(path, obj, "pw")
        with open(path, "rb") as f:
            assert f.read(len(otc.MAGIC)) == otc.MAGIC   # stored as ciphertext, not plaintext JSON
        assert otc.decrypt_file_to_json(path, "pw") == obj


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
