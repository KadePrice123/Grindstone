"""Password hashing and envelope encryption for broker credentials.

Design (REQUIREMENTS.md 6.7):
  login password --argon2id--> KEK  (never stored)
  random per-user DEK, stored wrapped by the KEK (AES-256-GCM)
  each broker secret encrypted individually under the DEK (AES-256-GCM,
  fresh 96-bit nonce, AAD binds user/account/field so ciphertexts cannot be
  swapped between rows without detection)

The login hash and the KEK use INDEPENDENT salts: the verifier proves the
password; the KDF salt derives the key. Password loss means key loss by
design — reset deletes and re-enrolls broker credentials.
"""
from __future__ import annotations

import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# RFC 9106 second recommended (low-memory) profile — matches argon2-cffi
# defaults, stated explicitly so a library default change can't silently
# weaken the KDF (the KDF params MUST stay in lockstep for old DEKs to unwrap).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_PARALLELISM = 4

_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_KIB,
    parallelism=ARGON2_PARALLELISM,
)

NONCE_LEN = 12
KEY_LEN = 32
SALT_LEN = 16


class BadPassword(Exception):
    """Wrong password (or tampered vault)."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(pw_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(pw_hash, password)
    except VerifyMismatchError:
        return False


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_LEN)


def new_dek() -> bytes:
    return secrets.token_bytes(KEY_LEN)


def derive_kek(password: str, kdf_salt: bytes) -> bytes:
    """Derive the key-encryption key. Salt must be the stored per-user kdf_salt,
    NOT the salt inside the argon2 login hash — independence is the point."""
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=kdf_salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise BadPassword()
    try:
        return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)
    except InvalidTag as e:
        raise BadPassword() from e


def wrap_dek(kek: bytes, dek: bytes, username: str) -> bytes:
    return _seal(kek, dek, b"grindstone-dek:" + username.encode("utf-8"))


def unwrap_dek(kek: bytes, wrapped: bytes, username: str) -> bytes:
    return _open(kek, wrapped, b"grindstone-dek:" + username.encode("utf-8"))


def secret_aad(user_id: int, account_id: int, field: str) -> bytes:
    return f"grindstone-secret:{user_id}:{account_id}:{field}".encode("utf-8")


def encrypt_secret(dek: bytes, value: str, user_id: int, account_id: int, field: str) -> bytes:
    return _seal(dek, value.encode("utf-8"), secret_aad(user_id, account_id, field))


def decrypt_secret(dek: bytes, blob: bytes, user_id: int, account_id: int, field: str) -> str:
    return _open(dek, blob, secret_aad(user_id, account_id, field)).decode("utf-8")


def best_effort_wipe(buf: bytearray) -> None:
    """Overwrite a mutable key buffer. Python may hold immutable copies —
    this is best-effort, documented as such, never claimed stronger."""
    for i in range(len(buf)):
        buf[i] = 0
