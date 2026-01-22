"""
API key generation and verification utilities.

User API keys follow format: 'athena_{timestamp}_{random}'
- Prefix allows version identification
- Timestamp allows key age tracking
- Random component provides security
- Total length ~50 chars for obscurity

Security:
- Keys are hashed with SHA-256 before storage
- Verification uses constant-time comparison (hmac.compare_digest)
- Raw keys shown only once at creation
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Tuple

import structlog

logger = structlog.get_logger()


def generate_api_key() -> str:
    """
    Generate a cryptographically secure API key.

    Returns:
        str: Format 'athena_{timestamp}_{random32}'
        Example: 'athena_1734451234_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6'
    """
    timestamp = str(int(datetime.utcnow().timestamp()))
    random_part = secrets.token_hex(16)  # 32 hex chars, no underscores
    return f"athena_{timestamp}_{random_part}"


def extract_key_prefix(api_key: str, length: int = 16) -> str:
    """
    Extract prefix from API key for efficient database lookup.

    Args:
        api_key: Full API key
        length: Prefix length (default 16)

    Returns:
        str: First N characters
    """
    return api_key[:length]


def hash_api_key(api_key: str) -> str:
    """
    Hash an API key using SHA-256.

    Args:
        api_key: Raw API key to hash

    Returns:
        str: 64-character hex-encoded SHA-256 hash

    CRITICAL: Use this for storage, never store raw keys
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(provided_key: str, stored_hash: str) -> bool:
    """
    Verify an API key against stored hash.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        provided_key: API key from request
        stored_hash: SHA-256 hash from database

    Returns:
        bool: True if key matches
    """
    computed_hash = hash_api_key(provided_key)
    # Constant-time comparison prevents timing attacks
    return hmac.compare_digest(computed_hash, stored_hash)


def calculate_expiration(days: int = 90) -> datetime:
    """
    Calculate expiration datetime.

    Args:
        days: Days until expiration (default 90)

    Returns:
        datetime: Future expiration timestamp
    """
    return datetime.utcnow() + timedelta(days=days)


def is_valid_key_format(api_key: str) -> bool:
    """
    Validate API key format.

    Args:
        api_key: Key to validate

    Returns:
        bool: True if format is valid
    """
    if not api_key:
        return False
    if not api_key.startswith('athena_'):
        return False
    parts = api_key.split('_')
    if len(parts) != 3:
        return False
    # Check timestamp part is numeric
    try:
        int(parts[1])
    except ValueError:
        return False
    return True
