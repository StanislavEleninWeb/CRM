"""Encryption of provider secrets.

AES-256-GCM with a random nonce per secret. The associated data binds each ciphertext
to its tenant, provider and connection, so a ciphertext copied into another row or
another tenant fails to decrypt. Key material comes from the environment, never the
database; each ciphertext records which key version produced it, which makes rotation
a matter of adding a new first key and re-encrypting.
"""

import base64
import binascii
import os
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


class SecretError(Exception):
    """A secret could not be encrypted or decrypted. Never carries secret material."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes
    key_version: str


class Keyring:
    def __init__(self, spec: str) -> None:
        self._keys: dict[str, bytes] = {}
        self._current: str | None = None
        for entry in filter(None, (part.strip() for part in spec.split(","))):
            version, _, encoded = entry.partition(":")
            try:
                key = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SecretError(f"encryption key {version!r} is not valid base64") from exc
            if not version or len(key) != 32:
                raise SecretError("each encryption key needs a version and exactly 32 bytes")
            if version in self._keys:
                raise SecretError(f"encryption key version {version!r} is listed twice")
            self._keys[version] = key
            self._current = self._current or version

    @property
    def configured(self) -> bool:
        return self._current is not None

    @property
    def current_version(self) -> str:
        if self._current is None:
            raise SecretError("no encryption key is configured (SECRET_ENCRYPTION_KEYS)")
        return self._current

    @staticmethod
    def _aad(tenant_id: UUID, provider: str, connection_id: UUID) -> bytes:
        return f"{tenant_id}|{provider}|{connection_id}".encode()

    def seal(self, plaintext: str, *, tenant_id: UUID, provider: str, connection_id: UUID) -> Sealed:
        version = self.current_version
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keys[version]).encrypt(
            nonce, plaintext.encode(), self._aad(tenant_id, provider, connection_id)
        )
        return Sealed(ciphertext=ciphertext, nonce=nonce, key_version=version)

    def open(self, sealed: Sealed, *, tenant_id: UUID, provider: str, connection_id: UUID) -> str:
        key = self._keys.get(sealed.key_version)
        if key is None:
            raise SecretError(f"encryption key version {sealed.key_version!r} is not available")
        try:
            return (
                AESGCM(key)
                .decrypt(sealed.nonce, sealed.ciphertext, self._aad(tenant_id, provider, connection_id))
                .decode()
            )
        except InvalidTag as exc:
            raise SecretError("the stored secret could not be decrypted for this connection") from exc


@lru_cache
def get_keyring() -> Keyring:
    return Keyring(get_settings().secret_encryption_keys)


def hint(secret: str) -> str:
    """A masked reminder of which secret is stored: the last four characters only."""
    return f"…{secret[-4:]}" if len(secret) >= 12 else "…"
