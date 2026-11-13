"""PII protection at rest.

Two different needs, two different tools:

* **Phone -> lookup key.** Events must be matched to leads by phone, so we need a
  *deterministic* token. A plain SHA-256 is NOT enough: there are only ~4 billion
  valid Indian mobile numbers, so anyone with the DB could hash them all in minutes
  and reverse every phone. We use **HMAC-SHA256 with a secret key**: deterministic
  (same phone -> same token, so joins work) but useless without the key.
* **Name (and the phone itself) -> encrypted.** Encrypted with Fernet (AES-128-CBC +
  HMAC) so an authorised process can still recover them, e.g. for the dialler.

Only the last 4 digits of the phone are stored in clear: they are needed for
``phone_masked`` and are not enough to identify anyone.

Key management: the master secret comes from ``LEDGER_PII_KEY``. For local runs, if
it is unset we create a random key file next to the DB (``<db>.key``, git-ignored)
and warn. In production the key would come from a secrets manager / KMS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ENV_VAR = "LEDGER_PII_KEY"


def load_master_key(db_path: str) -> bytes:
    env = os.environ.get(ENV_VAR)
    if env:
        return env.encode()
    key_file = Path(str(db_path) + ".key")
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = secrets.token_urlsafe(32).encode()
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    print(f"[ledger] {ENV_VAR} not set; created dev key file {key_file}. "
          f"Keep it with the DB, or the phone hashes cannot be recomputed.", file=sys.stderr)
    return key


class PiiVault:
    def __init__(self, master_key: bytes):
        # Derive two independent sub-keys so one key is never used for two purposes.
        self._hmac_key = hmac.new(master_key, b"ledger/phone-hmac/v1", hashlib.sha256).digest()
        enc_key = hmac.new(master_key, b"ledger/fernet/v1", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))

    def phone_token(self, canonical_phone: str) -> str:
        """Deterministic, keyed hash of a canonical '+91XXXXXXXXXX' phone."""
        return hmac.new(self._hmac_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, values: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(values, sort_keys=True).encode())

    def decrypt(self, blob: bytes) -> dict:
        return json.loads(self._fernet.decrypt(blob))
