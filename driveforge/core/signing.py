"""Ed25519 signature verification for firmware blobs.

Firmware DB entries carry a detached base64-encoded Ed25519 signature over
the canonical message `<model>|<transport>|<version>|<sha256>`. DriveForge
verifies against a bundled trust root (single public key for MVP; multi-
root community signing is Phase 8+).

Unsigned entries are surfaced in the UI as "unverified — cannot auto-apply"
but can still be used for check-only detection.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

# MVP trust-root placeholder. Replace with JT's real production key before
# the first signed firmware DB release. Generate with:
#   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
#   sk = Ed25519PrivateKey.generate(); pk = sk.public_key()
BUNDLED_TRUST_PUBKEY_B64 = ""  # empty = no trust root configured yet


def canonical_message(*, model: str, transport: str, version: str, sha256: str) -> bytes:
    return f"{model}|{transport}|{version}|{sha256}".encode()


def load_pubkey(key_b64: str | None) -> Ed25519PublicKey | None:
    src = key_b64 or BUNDLED_TRUST_PUBKEY_B64
    if not src:
        return None
    try:
        raw = base64.b64decode(src)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to decode trust pubkey: %s", exc)
        return None


def verify_signature(
    *,
    model: str,
    transport: str,
    version: str,
    sha256: str,
    signature_b64: str | None,
    pubkey: Ed25519PublicKey | None,
) -> bool:
    """Return True if the signature is present, decodable, and valid."""
    if not signature_b64 or pubkey is None:
        return False
    try:
        sig = base64.b64decode(signature_b64)
        message = canonical_message(model=model, transport=transport, version=version, sha256=sha256)
        pubkey.verify(sig, message)
        return True
    except (InvalidSignature, ValueError, TypeError) as exc:
        logger.warning("firmware signature verification failed: %s", exc)
        return False
