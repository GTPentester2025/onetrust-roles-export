"""Passphrase-based encryption at rest for exported data.

Uses the vetted `cryptography` library's Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from your
passphrase via PBKDF2-HMAC-SHA256. No hand-rolled crypto. A random salt is stored with each file, so the
same passphrase produces independent keys per file.

File layout (binary): MAGIC || 16-byte salt || Fernet token.

Install: pip install cryptography
"""
import base64
import getpass
import json
import os
import sys

MAGIC = b"OTENC1\n"
SALT_LEN = 16
KDF_ITERATIONS = 200_000   # PBKDF2 iterations


def available():
    """True if the cryptography library is importable."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _derive_key(passphrase, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_bytes(data, passphrase):
    """Return MAGIC + salt + Fernet token for the given bytes."""
    from cryptography.fernet import Fernet
    salt = os.urandom(SALT_LEN)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(data)
    return MAGIC + salt + token


def decrypt_bytes(blob, passphrase):
    """Return the plaintext bytes. Raises ValueError on a non-OT file or a wrong passphrase."""
    if not blob.startswith(MAGIC):   # checked before importing crypto, so this path needs no library
        raise ValueError("not a OneTrust-encrypted file (bad magic header)")
    from cryptography.fernet import Fernet, InvalidToken
    salt = blob[len(MAGIC):len(MAGIC) + SALT_LEN]
    token = blob[len(MAGIC) + SALT_LEN:]
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise ValueError("wrong passphrase or corrupted file")


def encrypt_json_to_file(path, obj, passphrase):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as f:
        f.write(encrypt_bytes(data, passphrase))


def decrypt_file_to_json(path, passphrase):
    with open(path, "rb") as f:
        return json.loads(decrypt_bytes(f.read(), passphrase).decode("utf-8"))


def prompt_passphrase(confirm):
    """Ask for a passphrase (hidden). When confirm=True (a fresh file), require it entered twice."""
    passphrase = getpass.getpass("Passphrase: ")
    if not passphrase:
        sys.exit("No passphrase entered.")
    if confirm:
        if passphrase != getpass.getpass("Confirm passphrase: "):
            sys.exit("Passphrases didn't match. Run again.")
    return passphrase
