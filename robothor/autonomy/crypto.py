"""Versioned native-vault envelopes bound to tenant, owner and record.

Key material is supplied by the vault host, never by an agent. The keyring
allows old envelopes to remain readable during an explicit rotation.
"""

import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from robothor.autonomy.models import Scope


def _aad(scope: Scope, record_id: str, key_id: str) -> bytes:
    return json.dumps(
        ["genus-resource", 1, scope.tenant_id, scope.owner_id, record_id, key_id],
        separators=(",", ":"),
    ).encode()


def seal_resource(
    value: str, keys: dict[str, bytes], key_id: str, scope: Scope, record_id: str
) -> bytes:
    encoded_id = key_id.encode("ascii")
    if not 1 <= len(encoded_id) <= 64:
        raise ValueError("invalid encryption key identifier")
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(keys[key_id]).encrypt(nonce, value.encode(), _aad(scope, record_id, key_id))
    return b"GR1" + bytes([len(encoded_id)]) + encoded_id + nonce + sealed


def open_resource(blob: bytes, keys: dict[str, bytes], scope: Scope, record_id: str) -> str:
    if len(blob) < 33 or blob[:3] != b"GR1" or not 1 <= blob[3] <= 64:
        raise ValueError("invalid resource envelope")
    end = 4 + blob[3]
    key_id = blob[4:end].decode("ascii")
    return (
        AESGCM(keys[key_id])
        .decrypt(blob[end : end + 12], blob[end + 12 :], _aad(scope, record_id, key_id))
        .decode()
    )
