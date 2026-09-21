import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import APP_SECRET


def _fernet() -> Fernet:
    key = hashlib.sha256(APP_SECRET.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
