"""Passwords, sessions, login throttling. No web-framework imports here."""
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

from . import config, db

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
    if db.one("SELECT id FROM users LIMIT 1"):
        # database already has users: make sure the admin from .env can still get in
        if config.ADMIN_PASSWORD:
            u = db.one("SELECT id FROM users WHERE email=?", (config.ADMIN_EMAIL,))
            if not u:
                db.execute("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                           (config.ADMIN_EMAIL, config.ADMIN_NAME, hash_password(config.ADMIN_PASSWORD), "admin", time.time()))
                print(f"[ops] Added admin {config.ADMIN_EMAIL} from .env (the database already had other users)")
            elif config.ADMIN_FORCE:
                db.execute("UPDATE users SET password_hash=?, role='admin' WHERE id=?", (hash_password(config.ADMIN_PASSWORD), u["id"]))
                print(f"[ops] Password of {config.ADMIN_EMAIL} reset from .env (OPS_ADMIN_FORCE_PASSWORD is on, turn it off after login)")
        return
    password = config.ADMIN_PASSWORD
    generated = False
    if not password:
        password = secrets.token_urlsafe(14)
        generated = True
    db.execute("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
               (config.ADMIN_EMAIL, config.ADMIN_NAME, hash_password(password), "admin", time.time()))
    if generated:
        f = config.DATA_DIR / "initial_admin_password.txt"
        f.write_text(f"{config.ADMIN_EMAIL}\n{password}\n")
        f.chmod(0o600)
        print(f"[ops] Seeded admin {config.ADMIN_EMAIL}. Generated password saved to {f}")
    else:
        print(f"[ops] Seeded admin {config.ADMIN_EMAIL}")


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
    db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at,last_seen,ip,ua) VALUES(?,?,?,?,?,?,?)",
               (_th(token), user_id, now, now + config.SESSION_HOURS * 3600, now, ip, (ua or "")[:300]))
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def user_for_token(token: str | None):
    if not token:
        return None
    now = time.time()
    row = db.one("""SELECT u.id,u.email,u.name,u.first_name,u.last_name,u.totp_enabled,u.role,u.disabled,s.expires_at,s.last_seen,s.token_hash
                    FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (_th(token),))
    if not row or row["expires_at"] < now or row["disabled"]:
        return None
    if now - row["last_seen"] > 60:
        db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, row["token_hash"]))
    row["session"] = row["token_hash"][:12]
    return row


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (_th(token),))


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
