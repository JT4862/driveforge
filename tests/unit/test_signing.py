from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from driveforge.core import signing
from driveforge.core.firmware import verify_blob


def test_signature_roundtrip() -> None:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    pub_bytes = pk.public_bytes_raw()
    pub_b64 = base64.b64encode(pub_bytes).decode()
    loaded = signing.load_pubkey(pub_b64)
    assert loaded is not None

    msg = signing.canonical_message(
        model="Samsung SSD 970 EVO Plus 1TB",
        transport="nvme",
        version="3B2QEXM7",
        sha256="a" * 64,
    )
    sig_bytes = sk.sign(msg)
    sig_b64 = base64.b64encode(sig_bytes).decode()

    assert signing.verify_signature(
        model="Samsung SSD 970 EVO Plus 1TB",
        transport="nvme",
        version="3B2QEXM7",
        sha256="a" * 64,
        signature_b64=sig_b64,
        pubkey=loaded,
    )


def test_wrong_message_fails_verification() -> None:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    sig = sk.sign(signing.canonical_message(model="M", transport="nvme", version="1", sha256="a" * 64))
    sig_b64 = base64.b64encode(sig).decode()
    # Same key, different message contents
    assert not signing.verify_signature(
        model="M",
        transport="nvme",
        version="2",  # mismatch
        sha256="a" * 64,
        signature_b64=sig_b64,
        pubkey=pk,
    )


def test_missing_signature_fails_closed() -> None:
    assert not signing.verify_signature(
        model="M",
        transport="nvme",
        version="1",
        sha256="a" * 64,
        signature_b64=None,
        pubkey=None,
    )


def test_verify_blob_matches_sha256(tmp_path: Path) -> None:
    blob = tmp_path / "firmware.bin"
    blob.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert verify_blob(blob, expected)
    assert not verify_blob(blob, "0" * 64)
