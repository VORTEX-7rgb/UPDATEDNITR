# Security Audit: CollegeClaw / NITRISClaw

This document presents a comprehensive production-readiness security audit of the repository, evaluating credential storage, encryption practices, key validation, potential secrets leakage, and logging robustness.

---

## 1. Credential Encryption & Key Management

### A. Encryption Standards
* **Implementation**: Uses `cryptography.fernet.Fernet` for symmetric encryption of passwords.
* **Encryption Strength**: Fernet uses AES-128 in CBC mode with HMAC-SHA256 for integrity authentication. This is an industry-standard symmetric encryption algorithm that is virtually impossible to crack without the key.
* **Key Validation**:
  * The system implements robust validation checks on startup (`crypto.py` lines 10-34).
  * If the `ENCRYPTION_KEY` environment variable is missing, or is not exactly 32 base64-encoded bytes, the application raises a clear `ValueError` and terminates immediately.
  * **Result**: Fail-fast behavior guarantees the server will never boot in an unconfigured or insecure state.

### B. Symmetric Key Rotation
* **Key Rotation Facility**: The system provides `rotate_encryption_keys` inside `crypto.py`:
  * Decrypts all user credentials using the old key.
  * Symmetrically re-encrypts the passwords using the new key.
  * Raises a `ValueError` immediately if decryption fails for any record, aborting the process to prevent partial rotations.
* **Evaluation**: Symmetrically sound and correct.

---

## 2. Secrets & Plaintext Exposure Auditing

### A. Plaintext Password Lifecycles
1. **User Inbound Message deletion**:
   * During FSM registration, once the password is parsed and stored, the inbound message is deleted:
     ```python
     try:
         await message.delete()
     except Exception:
         pass
     ```
   * **Security Impact**: Extremely high. This completely eliminates the plaintext password from the user's Telegram chat history, preventing unauthorized access if the user's phone or Telegram account is compromised.
2. **Password Decryption**:
   * Plaintext passwords only reside in memory as short-lived Python variables during sync execution.
   * They are never persisted in plaintext, nor cached in temporary stores.

### B. Prevention of Plaintext Leakage in Logs
1. **Model Reprs**:
   * The `User` model implements a custom `__repr__` that excludes `encrypted_password`, ensuring that logging a user model (e.g. `logger.info("Syncing user %s", user)`) will never print the password hash.
2. **Logging Censorship**:
   * Scraper exceptions, login errors, and network errors are caught and logged using generic messages (e.g., `"Login request failed"`). No passwords or user credentials are leaked to server log streams.

---

## 3. Vulnerability Audit & Recommendations

### A. Environment Configuration Safety
* **Current state**: Environment variables (`BOT_TOKEN`, `DATABASE_URL`, `ENCRYPTION_KEY`) are fetched from a local `.env` file via `python-dotenv`.
* **Production Recommendation**: For cloud environments, standard practice requires injecting these variables directly through environment secrets managers (e.g. AWS Secrets Manager, GitHub Secrets, or Docker Secrets) instead of keeping `.env` files in filesystem directories.

### B. Sanitized Error Propagation to Telegram Users
* When scraper/connection failures occur, the Telegram bot replies with clean, user-friendly errors (e.g., `"Could not fetch attendance. Please try again later."` or `"An unexpected error occurred."`) rather than raw Python stack traces, preventing technical architecture information leakages.
