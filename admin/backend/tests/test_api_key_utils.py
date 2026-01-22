"""
Unit tests for API key generation, hashing, and verification utilities.
"""
import os
import pytest

# Set test environment
os.environ["DEV_MODE"] = "true"

from app.utils.api_keys import (
    generate_api_key,
    hash_api_key,
    verify_api_key,
    extract_key_prefix,
    is_valid_key_format,
    calculate_expiration,
)


class TestGenerateApiKey:
    """Tests for API key generation."""

    def test_format_starts_with_athena(self):
        """Key should start with 'athena_' prefix."""
        key = generate_api_key()
        assert key.startswith('athena_')

    def test_format_has_three_parts(self):
        """Key should have format: athena_{timestamp}_{random}."""
        key = generate_api_key()
        parts = key.split('_')
        assert len(parts) == 3
        assert parts[0] == 'athena'

    def test_timestamp_is_numeric(self):
        """Second part should be a valid Unix timestamp."""
        key = generate_api_key()
        parts = key.split('_')
        timestamp = int(parts[1])
        # Should be a reasonable timestamp (after 2020, before 2100)
        assert 1577836800 < timestamp < 4102444800

    def test_random_part_is_32_hex_chars(self):
        """Random part should be 32 hex characters."""
        key = generate_api_key()
        parts = key.split('_')
        assert len(parts[2]) == 32
        # Verify it's valid hex
        int(parts[2], 16)

    def test_keys_are_unique(self):
        """Generated keys should be unique."""
        keys = [generate_api_key() for _ in range(100)]
        assert len(set(keys)) == 100

    def test_key_length_reasonable(self):
        """Key should be between 40-60 characters."""
        key = generate_api_key()
        assert 40 <= len(key) <= 60


class TestHashApiKey:
    """Tests for API key hashing."""

    def test_hash_is_64_chars(self):
        """SHA-256 hash should be 64 hex characters."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        assert len(hashed) == 64

    def test_hash_is_hex(self):
        """Hash should be valid hexadecimal."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        int(hashed, 16)  # Should not raise

    def test_same_key_same_hash(self):
        """Same key should produce same hash."""
        key = generate_api_key()
        hash1 = hash_api_key(key)
        hash2 = hash_api_key(key)
        assert hash1 == hash2

    def test_different_keys_different_hashes(self):
        """Different keys should produce different hashes."""
        key1 = generate_api_key()
        key2 = generate_api_key()
        assert hash_api_key(key1) != hash_api_key(key2)

    def test_hash_is_deterministic(self):
        """Hash function should be deterministic."""
        test_key = "athena_1234567890_testkey123"
        hash1 = hash_api_key(test_key)
        hash2 = hash_api_key(test_key)
        assert hash1 == hash2


class TestVerifyApiKey:
    """Tests for API key verification."""

    def test_correct_key_verifies(self):
        """Correct key should verify successfully."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        assert verify_api_key(key, hashed) is True

    def test_wrong_key_fails(self):
        """Wrong key should fail verification."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        assert verify_api_key("wrong_key", hashed) is False

    def test_similar_key_fails(self):
        """Key with one character different should fail."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        wrong_key = key[:-1] + "X"
        assert verify_api_key(wrong_key, hashed) is False

    def test_empty_key_fails(self):
        """Empty key should fail."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        assert verify_api_key("", hashed) is False

    def test_wrong_hash_fails(self):
        """Correct key with wrong hash should fail."""
        key = generate_api_key()
        wrong_hash = "a" * 64
        assert verify_api_key(key, wrong_hash) is False

    def test_verification_is_timing_safe(self):
        """Verification should use constant-time comparison."""
        key = generate_api_key()
        hashed = hash_api_key(key)

        # These should all take similar time (can't easily test timing,
        # but at least verify they all return False)
        assert verify_api_key("athena_0_a", hashed) is False
        assert verify_api_key("athena_0_" + "a" * 100, hashed) is False
        assert verify_api_key(key[:-1], hashed) is False


class TestExtractKeyPrefix:
    """Tests for key prefix extraction."""

    def test_default_length_16(self):
        """Default prefix should be 16 characters."""
        key = "athena_1234567890_abcdefghijklmnop"
        prefix = extract_key_prefix(key)
        assert len(prefix) == 16

    def test_custom_length(self):
        """Should support custom prefix length."""
        key = "athena_1234567890_abcdefghijklmnop"
        prefix = extract_key_prefix(key, length=10)
        assert len(prefix) == 10
        assert prefix == "athena_123"

    def test_prefix_matches_start(self):
        """Prefix should be the start of the key."""
        key = generate_api_key()
        prefix = extract_key_prefix(key)
        assert key.startswith(prefix)

    def test_short_key_returns_full(self):
        """Key shorter than prefix length should return full key."""
        key = "short"
        prefix = extract_key_prefix(key)
        assert prefix == "short"


class TestIsValidKeyFormat:
    """Tests for key format validation."""

    def test_valid_format_accepted(self):
        """Valid format should be accepted."""
        assert is_valid_key_format("athena_1234567890_abcdef") is True

    def test_generated_key_valid(self):
        """Generated keys should be valid."""
        key = generate_api_key()
        assert is_valid_key_format(key) is True

    def test_wrong_prefix_rejected(self):
        """Wrong prefix should be rejected."""
        assert is_valid_key_format("invalid_1234567890_abcdef") is False

    def test_missing_parts_rejected(self):
        """Keys with wrong number of parts should be rejected."""
        assert is_valid_key_format("athena_1234567890") is False
        assert is_valid_key_format("athena") is False

    def test_non_numeric_timestamp_rejected(self):
        """Non-numeric timestamp should be rejected."""
        assert is_valid_key_format("athena_notanumber_abcdef") is False

    def test_empty_rejected(self):
        """Empty string should be rejected."""
        assert is_valid_key_format("") is False

    def test_none_rejected(self):
        """None should be rejected."""
        assert is_valid_key_format(None) is False


class TestCalculateExpiration:
    """Tests for expiration calculation."""

    def test_default_90_days(self):
        """Default expiration should be 90 days."""
        from datetime import datetime, timedelta

        before = datetime.utcnow()
        expiration = calculate_expiration()
        after = datetime.utcnow()

        expected_min = before + timedelta(days=90)
        expected_max = after + timedelta(days=90)

        assert expected_min <= expiration <= expected_max

    def test_custom_days(self):
        """Custom days should work."""
        from datetime import datetime, timedelta

        before = datetime.utcnow()
        expiration = calculate_expiration(days=30)
        after = datetime.utcnow()

        expected_min = before + timedelta(days=30)
        expected_max = after + timedelta(days=30)

        assert expected_min <= expiration <= expected_max
