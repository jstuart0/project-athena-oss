"""
Encryption utilities for secret management.

Uses Fernet (symmetric encryption) from cryptography library.
"""
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import structlog

logger = structlog.get_logger()

# Get encryption key from environment
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Salt for key derivation (stored in environment or generated once)
ENCRYPTION_SALT = os.getenv("ENCRYPTION_SALT")

if not ENCRYPTION_KEY:
    logger.warning("encryption_key_missing", message="ENCRYPTION_KEY not set, generating temporary key")
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    logger.warning("generated_encryption_key", message="This is a temporary key. Set ENCRYPTION_KEY environment variable for production")

if not ENCRYPTION_SALT:
    logger.warning("encryption_salt_missing", message="ENCRYPTION_SALT not set, using default")
    ENCRYPTION_SALT = base64.urlsafe_b64encode(b"athena-admin-default-salt-change-me").decode()


def get_fernet_key() -> bytes:
    """
    Derive Fernet key from master key and salt.

    Returns:
        bytes: Fernet-compatible encryption key
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=base64.urlsafe_b64decode(ENCRYPTION_SALT.encode()),
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(ENCRYPTION_KEY.encode()))
    return key


# Initialize Fernet cipher
try:
    fernet = Fernet(get_fernet_key())
except Exception as e:
    logger.error("fernet_initialization_failed", error=str(e))
    # Fallback: generate new key
    fernet = Fernet(Fernet.generate_key())


def encrypt_value(plaintext: str) -> str:
    """
    Encrypt a plaintext string.

    Args:
        plaintext: String to encrypt

    Returns:
        str: Base64-encoded encrypted value

    Raises:
        Exception: If encryption fails
    """
    try:
        encrypted_bytes = fernet.encrypt(plaintext.encode())
        return base64.urlsafe_b64encode(encrypted_bytes).decode()
    except Exception as e:
        logger.error("encryption_failed", error=str(e))
        raise


def decrypt_value(encrypted_value: str) -> str:
    """
    Decrypt an encrypted string.

    Args:
        encrypted_value: Base64-encoded encrypted value

    Returns:
        str: Decrypted plaintext

    Raises:
        Exception: If decryption fails
    """
    try:
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_value.encode())
        decrypted_bytes = fernet.decrypt(encrypted_bytes)
        return decrypted_bytes.decode()
    except Exception as e:
        logger.error("decryption_failed", error=str(e))
        raise


def generate_encryption_key() -> tuple[str, str]:
    """
    Generate new encryption key and salt for initial setup.

    Returns:
        tuple: (encryption_key, encryption_salt) as base64-encoded strings
    """
    import secrets

    # Generate random key (32 bytes)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

    # Generate random salt (32 bytes)
    salt = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

    return key, salt


if __name__ == "__main__":
    # Test encryption/decryption
    test_value = "test-api-key-123"

    print("Testing encryption/decryption...")
    encrypted = encrypt_value(test_value)
    print(f"Encrypted: {encrypted}")

    decrypted = decrypt_value(encrypted)
    print(f"Decrypted: {decrypted}")

    assert decrypted == test_value, "Encryption/decryption test failed"
    print("✓ Encryption/decryption working correctly")

    # Generate new keys
    print("\nGenerating new encryption keys...")
    key, salt = generate_encryption_key()
    print(f"ENCRYPTION_KEY={key}")
    print(f"ENCRYPTION_SALT={salt}")
    print("\nAdd these to your Kubernetes secret or .env file")
