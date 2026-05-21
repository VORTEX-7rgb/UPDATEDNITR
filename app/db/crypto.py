"""Cryptographic utilities for secure symmetric password storage and rotation."""

import logging
import base64
from cryptography.fernet import Fernet
from app.config import config

logger = logging.getLogger(__name__)

def validate_key(key: str) -> None:
    """Validate that the given string is a valid Fernet key."""
    if not key:
        raise ValueError("ENCRYPTION_KEY is empty or not set.")
    try:
        # Fernet keys must be 32 URL-safe base64-encoded bytes
        key_bytes = key.encode("utf-8")
        decoded = base64.urlsafe_b64decode(key_bytes)
        if len(decoded) != 32:
            raise ValueError(f"Decoded key length is {len(decoded)} bytes, must be exactly 32 bytes.")
    except Exception as e:
        raise ValueError(
            "Invalid ENCRYPTION_KEY. Please ensure it is a valid 32-byte URL-safe base64 key. "
            "You can generate one using: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from e

# Validate on import/startup to fail loudly as requested
if not config.ENCRYPTION_KEY:
    # During testing or if not configured, raise a clear instruction
    raise ValueError(
        "ENCRYPTION_KEY environment variable is missing. "
        "Please generate a key and add it to your .env file:\n"
        "ENCRYPTION_KEY=your_base64_key"
    )
validate_key(config.ENCRYPTION_KEY)


def encrypt_password(raw_password: str, custom_key: str = None) -> str:
    """Encrypt a password string using Fernet symmetric encryption."""
    key = custom_key or config.ENCRYPTION_KEY
    if custom_key:
        validate_key(custom_key)
        
    f = Fernet(key.encode("utf-8"))
    encrypted_bytes = f.encrypt(raw_password.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def decrypt_password(encrypted_password: str, custom_key: str = None) -> str:
    """Decrypt a password string using Fernet symmetric encryption."""
    key = custom_key or config.ENCRYPTION_KEY
    if custom_key:
        validate_key(custom_key)
        
    f = Fernet(key.encode("utf-8"))
    decrypted_bytes = f.decrypt(encrypted_password.encode("utf-8"))
    return decrypted_bytes.decode("utf-8")


async def rotate_encryption_keys(users: list, old_key: str, new_key: str) -> dict[int, str]:
    """Symmetrically decrypt and re-encrypt passwords for a list of user models.
    
    Returns a mapping of user_id -> new_encrypted_password.
    Raises ValueError if decryption fails for any user.
    """
    validate_key(old_key)
    validate_key(new_key)
    
    updated_passwords = {}
    for user in users:
        try:
            # Settle decrypted password using old key
            plaintext = decrypt_password(user.encrypted_password, custom_key=old_key)
            # Re-encrypt password using new key
            ciphertext = encrypt_password(plaintext, custom_key=new_key)
            updated_passwords[user.id] = ciphertext
        except Exception as e:
            logger.error("Failed to decrypt password for user ID %s during rotation", user.id)
            raise ValueError(f"Key rotation failed at user {user.id} due to decryption error: {e}") from e
            
    return updated_passwords
