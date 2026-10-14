"""The publisher's signature on a release's ``.sha256`` file.

The sha256 beside a release zip proves the download is intact, but the same
feed publishes both, so on its own it cannot say who published them. The
release job signs the ``.sha256`` file with an Ed25519 key held as the
repository secret ``UPDATE_SIGNING_KEY`` (``scripts/sign_release.py``) and
attaches ``<zip>.sha256.sig``; the update check accepts the sha256 only when
that signature verifies against ``public_key`` in ``config/update.yaml``.
Keys and signatures are base64 of the raw 32- and 64-byte Ed25519 values."""

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def keygen() -> tuple[str, str]:
    """A new (private, public) key pair."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(*_RAW)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def sign(private: str, data: bytes) -> str:
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private.strip()))
    return base64.b64encode(key.sign(data)).decode()


def verify(public: str, data: bytes, signature: str) -> bool:
    """True only when ``signature`` is ``public``'s signature of ``data``; a
    malformed key or signature is a failed check, never an error."""
    try:
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public.strip(), validate=True)
        )
        key.verify(base64.b64decode(signature.strip(), validate=True), data)
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True
