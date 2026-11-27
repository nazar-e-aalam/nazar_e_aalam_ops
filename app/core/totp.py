"""TOTP (RFC 6238) and recovery codes. Standard library only."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP, DIGITS = 30, 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return str(n).zfill(DIGITS)


def code_at(secret: str, at: float | None = None) -> str:
    return _code(secret, int((time.time() if at is None else at) // STEP))


def verify(secret: str, code: str, last_step: int = 0, at: float | None = None):
    """Returns the matched time step (int) or None. Accepts +-1 step; a step <= last_step is a replay."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    now = int((time.time() if at is None else at) // STEP)
    hit = None
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(_code(secret, step), code) and step > last_step:
            hit = step
    return hit


def otpauth_uri(secret: str, account: str, issuer: str = "Nazar Ops") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"


def new_recovery_codes(n: int = 10) -> list:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["".join(secrets.choice(alphabet) for _ in range(5)) + "-" + "".join(secrets.choice(alphabet) for _ in range(5))
            for _ in range(n)]


def norm_recovery(code: str) -> str:
    c = "".join(ch for ch in (code or "").lower() if ch.isalnum())
    return c[:5] + "-" + c[5:] if len(c) == 10 else ""


def hash_recovery(code: str) -> str:
    return hashlib.sha256(("ops-recovery:" + norm_recovery(code)).encode()).hexdigest()
