"""Recovery commands for the person with shell access to the server (e.g. locked out, or no email set up):

    python -m app.manage reset-password you@example.com
    python -m app.manage disable-2fa you@example.com
    python -m app.manage create-admin you@example.com
    python -m app.manage list-users
"""
import getpass
import secrets
import sys
import time

from .core import security
from .db import db
from .db.models import RecoveryCode, User, UserSession


def _user(email: str):
    u = db.get(User, User.email == email.strip().lower())
    if not u:
        sys.exit(f"No user with email {email}")
    return u


def _ask_password() -> str:
    pw = getpass.getpass("New password (empty = generate one): ")
    if not pw:
        pw = secrets.token_urlsafe(14)
        print(f"Generated password: {pw}")
        return pw
    err = security.check_password_strength(pw)
    if err:
        sys.exit(err)
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("The passwords do not match")
    return pw


def main(argv: list) -> int:
    db.init()
    cmd = argv[0] if argv else ""
    if cmd == "list-users":
        for u in db.all(User, order=User.id, cols=[User.email, User.role, User.disabled, User.totp_enabled]):
            print(f"{u['email']:40} {u['role']:9} {'disabled ' if u['disabled'] else ''}{'2FA' if u['totp_enabled'] else ''}")
        return 0
    if len(argv) != 2 or cmd not in ("reset-password", "disable-2fa", "create-admin"):
        print(__doc__)
        return 2
    email = argv[1].strip().lower()
    if cmd == "reset-password":
        u = _user(email)
        pw = _ask_password()
        db.update(User, User.id == u["id"], password_hash=security.hash_password(pw), disabled=0)
        db.delete(UserSession, UserSession.user_id == u["id"])
        db.audit("cli", "password.reset", email)
        print(f"Password for {email} updated; they were signed out everywhere.")
    elif cmd == "disable-2fa":
        u = _user(email)
        db.update(User, User.id == u["id"], totp_secret=None, totp_pending=None, totp_enabled=0, totp_last_step=0)
        db.delete(RecoveryCode, RecoveryCode.user_id == u["id"])
        db.audit("cli", "2fa.reset_by_admin", email)
        print(f"Two-factor authentication turned off for {email}.")
    else:
        if db.exists(User, User.email == email):
            sys.exit("That user already exists. Use reset-password.")
        pw = _ask_password()
        db.insert(User, email=email, name=email.split("@")[0], first_name=email.split("@")[0],
                  password_hash=security.hash_password(pw), role="admin", created_at=time.time())
        db.audit("cli", "user.created", f"{email} (admin)")
        print(f"Admin {email} created.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
