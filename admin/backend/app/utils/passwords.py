"""
Password hashing utilities for local Athena accounts.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Tuple


DEFAULT_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10


def validate_password_strength(password: str) -> Tuple[bool, str]:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters long"

    if password.strip() != password:
        return False, "Password cannot start or end with whitespace"

    return True, ""


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_str, salt_encoded, digest_encoded = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False

        iterations = int(iterations_str)

        # Support both hex encoding (current) and base64 encoding (legacy)
        import base64
        try:
            salt = bytes.fromhex(salt_encoded)
            expected = bytes.fromhex(digest_encoded)
        except ValueError:
            salt = base64.b64decode(salt_encoded)
            expected = base64.b64decode(digest_encoded)

        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False
