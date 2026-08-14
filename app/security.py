"""Hash de senha (pbkdf2, stdlib) e verificacao. Sem dependencias externas."""
from __future__ import annotations

import hashlib
import hmac
import os

_ALGO = "sha256"
_ITER = 240_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Gera 'pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>'."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITER)
    return f"pbkdf2_{_ALGO}${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        assert scheme == f"pbkdf2_{_ALGO}"
        dk = hashlib.pbkdf2_hmac(
            _ALGO, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False
