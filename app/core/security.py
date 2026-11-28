"""Passwords, sessions, login throttling. No web-framework imports here."""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

from sqlalchemy import select

from . import config, logs
from ..db import db
from ..db.models import User, UserSession

log = logging.getLogger("ops.auth")

COOKIE = "ops_session"
_SCRYPT = dict(n=2 ** 14, r=8, p=1)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${}${}${}${}${}".format(_SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"],
                                          base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                              n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def check_password_strength(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if password.lower() in ("password123", "1234567890", "changeme123"):
        return "That password is too common."
    return None


def seed_admin() -> None:
    """Create the first admin from OPS_ADMIN_EMAIL / OPS_ADMIN_PASSWORD."""
    if db.get(User, cols=[User.id]):
        # database already has users: make sure the admin from .env can still get in
        if config.ADMIN_PASSWORD:
            u = db.get(User, User.email == config.ADMIN_EMAIL, cols=[User.id])
            if not u:
                db.insert(User, email=config.ADMIN_EMAIL, name=config.ADMIN_NAME,
                          password_hash=hash_password(config.ADMIN_PASSWORD), role="admin", created_at=time.time())
                log.info("added admin %s from .env (the database already had other users)", config.ADMIN_EMAIL)
            elif config.ADMIN_FORCE:
                db.update(User, User.id == u["id"], password_hash=hash_password(config.ADMIN_PASSWORD), role="admin")
                log.warning("password of %s reset from .env (OPS_ADMIN_FORCE_PASSWORD is on, turn it off after login)", config.ADMIN_EMAIL)
        return
    password = config.ADMIN_PASSWORD
    generated = False
    if not password:
        password = secrets.token_urlsafe(14)
        generated = True
    db.insert(User, email=config.ADMIN_EMAIL, name=config.ADMIN_NAME, password_hash=hash_password(password),
              role="admin", created_at=time.time())
    if generated:
        logs.banner(["First login (shown once, change it in Settings > Account)", f"Email    : {config.ADMIN_EMAIL}", f"Password : {password}"])
    else:
        log.info("seeded admin %s", config.ADMIN_EMAIL)


# ---- sessions ----------------------------------------------------------------
def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_hash(token: str) -> str:
    return _th(token)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(user_id: int, ip: str, ua: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.insert(UserSession, token_hash=_th(token), user_id=user_id, created_at=now,
              expires_at=now + config.SESSION_HOURS * 3600, last_seen=now, ip=ip, ua=(ua or "")[:300])
    db.delete(UserSession, UserSession.expires_at < now)
    return token


def user_for_token(token: str | None):
    if not token:
        return None
    now = time.time()
    row = db.select_one(
        select(User.id, User.email, User.name, User.first_name, User.last_name, User.totp_enabled, User.role, User.disabled,
               UserSession.expires_at, UserSession.last_seen, UserSession.token_hash)
        .select_from(UserSession.__table__.join(User.__table__, User.id == UserSession.user_id))
        .where(UserSession.token_hash == _th(token)))
    if not row or row["expires_at"] < now or row["disabled"]:
        return None
    if now - row["last_seen"] > 60:
        db.update(UserSession, UserSession.token_hash == row["token_hash"], last_seen=now)
    row["session"] = row["token_hash"][:12]
    return row


def destroy_session(token: str | None) -> None:
    if token:
        db.delete(UserSession, UserSession.token_hash == _th(token))


# ---- login throttling (in memory, per ip+email) -------------------------------
_fail: dict = {}
_lock = threading.Lock()
MAX_FAILS, WINDOW = 8, 600


def throttled(key: str) -> int:
    """Seconds to wait, or 0."""
    with _lock:
        cnt, first = _fail.get(key, (0, 0))
        if cnt >= MAX_FAILS:
            left = int(first + WINDOW - time.time())
            if left > 0:
                return left
            _fail.pop(key, None)
    return 0


def note_failure(key: str) -> None:
    with _lock:
        cnt, first = _fail.get(key, (0, time.time()))
        if time.time() - first > WINDOW:
            cnt, first = 0, time.time()
        _fail[key] = (cnt + 1, first)


def clear_failures(key: str) -> None:
    with _lock:
        _fail.pop(key, None)
