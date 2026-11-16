"""Password hashing and opaque session-token primitives; no IO."""
import hashlib
import hmac
import secrets

_PASSWORD_ITERATIONS = 600_000

def hash_password(password: str, salt: bytes | None=None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, _PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}'

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, expected = encoded.split('$', 3)
        if scheme != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt_hex), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
