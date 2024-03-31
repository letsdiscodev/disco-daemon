from base64 import standard_b64decode, standard_b64encode
from typing import overload

from cryptography.fernet import Fernet


@overload
def encrypt(string: str) -> str:
    ...


@overload
def encrypt(string: None) -> None:
    ...


def encrypt(string: str | None) -> str | None:
    if string is None:
        return None
    cipher_suite = Fernet(_encryption_key())
    string_bytes = string.encode("utf-8")
    encoded_bytes = cipher_suite.encrypt(string_bytes)
    encoded_str = standard_b64encode(encoded_bytes).decode("ascii")
    return encoded_str


@overload
def decrypt(string: str) -> str:
    ...


@overload
def decrypt(string: None) -> None:
    ...


def decrypt(string: str | None) -> str | None:
    if string is None:
        return None
    cipher_suite = Fernet(_encryption_key())
    encoded_bytes = standard_b64decode(string)
    decoded_bytes = cipher_suite.decrypt(encoded_bytes)
    decoded_text = decoded_bytes.decode("utf-8")
    return decoded_text


def generate_key() -> bytes:
    return Fernet.generate_key()


_cached_encryption_key: bytes | None = None


def _encryption_key() -> bytes:
    global _cached_encryption_key
    if _cached_encryption_key is None:
        with open("/run/secrets/disco_encryption_key", "rb") as f:
            _cached_encryption_key = f.read()
    return _cached_encryption_key


def obfuscate(string: str) -> str:
    asterisks = "*" * (len(string) - 4)
    return f"{string[:3]}{asterisks}{string[-1]}"
