"""Invitations, forgot / reset password, two-factor set-up and email settings."""
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from sqlalchemy import select

from ..core import config, mailer, qr, security, totp
from ..db import db
from ..db.models import Invite, LoginChallenge, PasswordReset, RecoveryCode, User, UserSession
from .ops import (accept_second_factor, admin_only, csrf, current_user, ip_of, security_cfg, start_session,
                         user_view)

router = APIRouter()
INVITE_DAYS, RESET_MINUTES = 7, 60
ROLES = ("admin", "operator", "viewer")


def _email(v: str) -> str:
    v = (v or "").strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@") or " " in v or len(v) > 120:
        raise HTTPException(400, "Enter a valid email address")
    return v


def _names(first: str, last: str) -> tuple:
    first, last = (first or "").strip(), (last or "").strip()
    if not first or not last:
        raise HTTPException(400, "Enter your first and last name")
    if len(first) > 40 or len(last) > 40:
        raise HTTPException(400, "Names can be at most 40 characters")
    return first, last


def _passwords(pw: str, confirm: str) -> None:
    if pw != confirm:
        raise HTTPException(400, "The passwords do not match")
    err = security.check_password_strength(pw)
    if err:
        raise HTTPException(400, err)


def _link(kind: str, token: str) -> str:
    # Always built from the server's own configuration, never from the Host header of the request.
    return f"{config.OPS_URL}/#/{kind}?token={token}"


# ---- public ---------------------------------------------------------------------------------
@router.get("/api/auth/options")
def options():
    return {"smtp": mailer.configured(), "version": config.VERSION}


def _live_invite(token: str):
    row = db.get(Invite, Invite.token_hash == security.token_hash(token or ""))
    if not row or row["used_at"] or row["revoked"] or row["expires_at"] < time.time():
        return None
    return row


@router.get("/api/auth/invite")
def invite_info(token: str, request: Request):
    key = f"{ip_of(request)}|invite"
    if security.throttled(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    row = _live_invite(token)
    if not row:
        security.note_failure(key)
        raise HTTPException(404, "This invitation link is invalid or has expired. Ask an admin to send a new one.")
    return {"email": row["email"], "role": row["role"], "invited_by": row["invited_by"], "expires_at": row["expires_at"]}


class AcceptBody(BaseModel):
    token: str
    first_name: str
    last_name: str
    password: str
    confirm_password: str


@router.post("/api/auth/accept-invite")
def accept_invite(body: AcceptBody, request: Request, response: Response):
    csrf(request)
    key = f"{ip_of(request)}|invite"
    if security.throttled(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    first, last = _names(body.first_name, body.last_name)
    _passwords(body.password, body.confirm_password)
    ph = security.hash_password(body.password)
    with db.transaction() as c:
        row = c.get(Invite, Invite.token_hash == security.token_hash(body.token), Invite.used_at.is_(None),
                    Invite.revoked == 0, Invite.expires_at > time.time())
        if not row:
            security.note_failure(key)
            raise HTTPException(404, "This invitation link is invalid or has expired. Ask an admin to send a new one.")
        if c.exists(User, User.email == row["email"]):
            raise HTTPException(400, "An account with this email already exists. Sign in instead.")
        if c.update(Invite, Invite.id == row["id"], Invite.used_at.is_(None), used_at=time.time()) != 1:
            security.note_failure(key)
            raise HTTPException(404, "This invitation link is invalid or has expired. Ask an admin to send a new one.")
        uid = c.insert(User, email=row["email"], name=f"{first} {last}", first_name=first, last_name=last,
                       password_hash=ph, role=row["role"], created_at=time.time())
    start_session(uid, request, response)
    db.audit(row["email"], "invite.accepted", f"role {row['role']}", ip_of(request))
    return {"user": user_view(db.get(User, User.id == uid))}


class ForgotBody(BaseModel):
    email: str


def _send_reset(user: dict) -> str:
    token = security.new_token()
    db.update(PasswordReset, PasswordReset.user_id == user["id"], PasswordReset.used_at.is_(None), used_at=time.time())
    db.insert(PasswordReset, token_hash=security.token_hash(token), user_id=user["id"], created_at=time.time(),
              expires_at=time.time() + RESET_MINUTES * 60)
    return _link("reset-password", token)


@router.post("/api/auth/forgot")
def forgot(body: ForgotBody, request: Request):
    csrf(request)
    if not mailer.configured():
        raise HTTPException(400, "Email is not set up on this server. Ask an admin to reset your password.")
    key = f"{ip_of(request)}|forgot"
    if security.throttled(key):
        raise HTTPException(429, "Too many requests. Try again later.")
    security.note_failure(key)          # every request counts: stops using this form to spam inboxes
    email = (body.email or "").strip().lower()
    u = db.get(User, User.email == email, User.disabled == 0) if "@" in email else None
    if u:
        link = _send_reset(u)
        subject, text, html = mailer.reset_email(link, RESET_MINUTES)

        def go():
            try:
                mailer.send(u["email"], subject, text, html)
            except mailer.MailError as e:
                db.audit(u["email"], "reset.email_failed", str(e), "")
        threading.Thread(target=go, daemon=True).start()
        db.audit(u["email"], "reset.requested", "", ip_of(request))
    # identical answer whether or not the address has an account
    return {"ok": True}


def _reset_select(th: str):
    """Live (unused, unexpired) reset token joined with its enabled user's email."""
    return (select(*PasswordReset.__table__.columns, User.email)
            .select_from(PasswordReset.__table__.join(User.__table__, User.id == PasswordReset.user_id))
            .where(PasswordReset.token_hash == th, PasswordReset.used_at.is_(None),
                   PasswordReset.expires_at > time.time(), User.disabled == 0))


@router.get("/api/auth/reset")
def reset_info(token: str, request: Request):
    key = f"{ip_of(request)}|reset"
    if security.throttled(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    row = db.select_one(_reset_select(security.token_hash(token or "")))
    if not row:
        security.note_failure(key)
        raise HTTPException(404, "This reset link is invalid or has expired. Request a new one.")
    return {"email": row["email"]}


class ResetBody(BaseModel):
    token: str
    password: str
    confirm_password: str


@router.post("/api/auth/reset")
def reset_password(body: ResetBody, request: Request):
    csrf(request)
    key = f"{ip_of(request)}|reset"
    if security.throttled(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    _passwords(body.password, body.confirm_password)
    ph = security.hash_password(body.password)
    with db.transaction() as c:
        row = c.first(_reset_select(security.token_hash(body.token)))
        if not row:
            security.note_failure(key)
            raise HTTPException(404, "This reset link is invalid or has expired. Request a new one.")
        if c.update(PasswordReset, PasswordReset.token_hash == row["token_hash"], PasswordReset.used_at.is_(None),
                    used_at=time.time()) != 1:
            security.note_failure(key)
            raise HTTPException(404, "This reset link is invalid or has expired. Request a new one.")
        c.update(User, User.id == row["user_id"], password_hash=ph)
        c.update(PasswordReset, PasswordReset.user_id == row["user_id"], PasswordReset.used_at.is_(None), used_at=time.time())
        c.delete(UserSession, UserSession.user_id == row["user_id"])
        c.delete(LoginChallenge, LoginChallenge.user_id == row["user_id"])
    db.audit(row["email"], "password.reset", "via email link", ip_of(request))
    return {"ok": True}      # signing in afterwards still asks for the 2FA code


# ---- invitations (admin) -----------------------------------------------------------------------
class InviteBody(BaseModel):
    email: str
    role: str = "operator"


def _invite_out(row: dict, link: str | None = None, sent: bool = False, error: str | None = None) -> dict:
    return {"id": row["id"], "email": row["email"], "role": row["role"], "expires_at": row["expires_at"],
            "link": link, "sent": sent, "email_error": error}


def _issue_invite(email: str, role: str, inviter: dict) -> dict:
    token = security.new_token()
    now = time.time()
    db.update(Invite, Invite.email == email, Invite.used_at.is_(None), Invite.revoked == 0, revoked=1)
    iid = db.insert(Invite, token_hash=security.token_hash(token), email=email, role=role,
                    invited_by=inviter["name"] or inviter["email"], created_at=now, expires_at=now + INVITE_DAYS * 86400)
    link = _link("accept-invite", token)
    sent, error = False, None
    if mailer.configured():
        subject, text, html = mailer.invite_email(email, inviter["name"] or inviter["email"], role, link, INVITE_DAYS)
        try:
            mailer.send(email, subject, text, html)
            sent = True
            db.update(Invite, Invite.id == iid, emailed=1)
        except mailer.MailError as e:
            error = str(e)
    return _invite_out(db.get(Invite, Invite.id == iid), link, sent, error)


@router.post("/api/invites")
def invite_create(body: InviteBody, request: Request, user: dict = Depends(admin_only)):
    email = _email(body.email)
    if body.role not in ROLES:
        raise HTTPException(400, "Role must be admin, operator or viewer")
    if db.exists(User, User.email == email):
        raise HTTPException(400, "A user with this email already exists")
    out = _issue_invite(email, body.role, user)
    db.audit(user["email"], "invite.sent" if out["sent"] else "invite.created", f"{email} ({body.role})", ip_of(request))
    return out


@router.get("/api/invites")
def invite_list(user: dict = Depends(admin_only)):
    rows = db.all(Invite, Invite.used_at.is_(None), Invite.revoked == 0, order=Invite.id.desc())
    now = time.time()
    return [dict(_invite_out(r), expired=r["expires_at"] < now, emailed=bool(r["emailed"]), invited_by=r["invited_by"],
                 created_at=r["created_at"]) for r in rows]


@router.post("/api/invites/{iid}/resend")
def invite_resend(iid: int, request: Request, user: dict = Depends(admin_only)):
    row = db.get(Invite, Invite.id == iid, Invite.used_at.is_(None), Invite.revoked == 0)
    if not row:
        raise HTTPException(404, "Invitation not found")
    if db.exists(User, User.email == row["email"]):
        raise HTTPException(400, "A user with this email already exists")
    out = _issue_invite(row["email"], row["role"], user)      # a fresh link; the old one stops working
    db.audit(user["email"], "invite.resent", row["email"], ip_of(request))
    return out


@router.delete("/api/invites/{iid}")
def invite_revoke(iid: int, request: Request, user: dict = Depends(admin_only)):
    row = db.get(Invite, Invite.id == iid)
    if not row:
        raise HTTPException(404, "Invitation not found")
    db.update(Invite, Invite.id == iid, revoked=1)
    db.audit(user["email"], "invite.revoked", row["email"], ip_of(request))
    return {"ok": True}


@router.post("/api/users/{uid}/reset-link")
def user_reset_link(uid: int, request: Request, user: dict = Depends(admin_only)):
    u = db.get(User, User.id == uid, User.disabled == 0)
    if not u:
        raise HTTPException(404, "User not found or disabled")
    link = _send_reset(u)
    sent, error = False, None
    if mailer.configured():
        subject, text, html = mailer.reset_email(link, RESET_MINUTES)
        try:
            mailer.send(u["email"], subject, text, html)
            sent = True
        except mailer.MailError as e:
            error = str(e)
    db.audit(user["email"], "reset.sent_by_admin", u["email"], ip_of(request))
    return {"link": link, "sent": sent, "email_error": error, "minutes": RESET_MINUTES}


# ---- two-factor ----------------------------------------------------------------------------------
def _fresh(user: dict) -> dict:
    return db.get(User, User.id == user["id"])


def _recovery_left(uid: int) -> int:
    return db.count(RecoveryCode, RecoveryCode.user_id == uid, RecoveryCode.used_at.is_(None))


@router.get("/api/2fa/status")
def twofa_status(user: dict = Depends(current_user)):
    u = _fresh(user)
    return {"enabled": bool(u["totp_enabled"]), "recovery_left": _recovery_left(u["id"]),
            "required": security_cfg()["require_2fa"]}


@router.post("/api/2fa/setup")
def twofa_setup(user: dict = Depends(current_user)):
    u = _fresh(user)
    if u["totp_enabled"]:
        raise HTTPException(400, "Two-factor authentication is already on")
    secret = totp.new_secret()
    db.update(User, User.id == u["id"], totp_pending=secret)
    uri = totp.otpauth_uri(secret, u["email"])
    return {"secret": secret, "uri": uri, "qr": qr.svg(uri)}


def _store_recovery(uid: int) -> list:
    codes = totp.new_recovery_codes()
    with db.transaction() as tx:                      # old codes are only replaced if the new ones were stored
        tx.delete(RecoveryCode, RecoveryCode.user_id == uid)
        for c in codes:
            tx.insert(RecoveryCode, user_id=uid, code_hash=totp.hash_recovery(c))
    return codes


class CodeBody(BaseModel):
    code: str


@router.post("/api/2fa/enable")
def twofa_enable(body: CodeBody, request: Request, user: dict = Depends(current_user)):
    u = _fresh(user)
    key = f"{ip_of(request)}|{u['id']}|2fa-setup"
    if security.throttled(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    if u["totp_enabled"] or not u["totp_pending"]:
        raise HTTPException(400, "Start the set-up again")
    step = totp.verify(u["totp_pending"], body.code)
    if step is None:
        security.note_failure(key)
        raise HTTPException(400, "That code is not right. Check the time on your phone and try the next code.")
    db.update(User, User.id == u["id"], totp_secret=u["totp_pending"], totp_pending=None, totp_enabled=1, totp_last_step=step)
    security.clear_failures(key)
    db.audit(u["email"], "2fa.enabled", "", ip_of(request))
    return {"recovery_codes": _store_recovery(u["id"])}


class PwCodeBody(BaseModel):
    password: str
    code: str


def _confirm_identity(u: dict, body: PwCodeBody, request: Request) -> None:
    key = f"{ip_of(request)}|{u['id']}|2fa-confirm"
    if security.throttled(key):
        raise HTTPException(429, "Too many attempts. Try again later.")
    if not security.verify_password(body.password, u["password_hash"]) or not accept_second_factor(u, body.code):
        security.note_failure(key)
        raise HTTPException(400, "Wrong password or code")
    security.clear_failures(key)


@router.post("/api/2fa/disable")
def twofa_disable(body: PwCodeBody, request: Request, user: dict = Depends(current_user)):
    u = _fresh(user)
    if not u["totp_enabled"]:
        raise HTTPException(400, "Two-factor authentication is not on")
    if security_cfg()["require_2fa"]:
        raise HTTPException(400, "Your admin requires two-factor authentication, so it cannot be turned off")
    _confirm_identity(u, body, request)
    db.update(User, User.id == u["id"], totp_secret=None, totp_pending=None, totp_enabled=0, totp_last_step=0)
    db.delete(RecoveryCode, RecoveryCode.user_id == u["id"])
    db.audit(u["email"], "2fa.disabled", "", ip_of(request))
    return {"ok": True}


@router.post("/api/2fa/recovery")
def twofa_recovery(body: PwCodeBody, request: Request, user: dict = Depends(current_user)):
    u = _fresh(user)
    if not u["totp_enabled"]:
        raise HTTPException(400, "Two-factor authentication is not on")
    _confirm_identity(u, body, request)
    db.audit(u["email"], "2fa.recovery_regenerated", "", ip_of(request))
    return {"recovery_codes": _store_recovery(u["id"])}


@router.post("/api/users/{uid}/reset-2fa")
def user_reset_2fa(uid: int, request: Request, user: dict = Depends(admin_only)):
    u = db.get(User, User.id == uid)
    if not u:
        raise HTTPException(404, "User not found")
    db.update(User, User.id == uid, totp_secret=None, totp_pending=None, totp_enabled=0, totp_last_step=0)
    db.delete(RecoveryCode, RecoveryCode.user_id == uid)
    db.delete(LoginChallenge, LoginChallenge.user_id == uid)
    db.audit(user["email"], "2fa.reset_by_admin", u["email"], ip_of(request))
    return {"ok": True}


# ---- settings: email + security policy ------------------------------------------------------------
@router.get("/api/email")
def email_get(user: dict = Depends(admin_only)):
    return mailer.public_cfg()


class EmailBody(BaseModel):
    host: str
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str | None = None      # None = keep the saved one
    from_email: str
    from_name: str = "Nazar Ops"


@router.put("/api/email")
def email_put(body: EmailBody, request: Request, user: dict = Depends(admin_only)):
    host = body.host.strip()
    if not host or " " in host or "/" in host:
        raise HTTPException(400, "Enter the SMTP server name, e.g. smtp.example.com")
    if not 1 <= body.port <= 65535:
        raise HTTPException(400, "Port must be between 1 and 65535")
    if body.security not in ("starttls", "ssl", "none"):
        raise HTTPException(400, "Security must be starttls, ssl or none")
    _email(body.from_email)
    mailer.save_cfg(host, body.port, body.security, body.username, body.password, body.from_email, body.from_name)
    db.audit(user["email"], "email.settings", f"{host}:{body.port} {body.security}", ip_of(request))
    return mailer.public_cfg()


@router.delete("/api/email")
def email_clear(request: Request, user: dict = Depends(admin_only)):
    db.sdel("smtp_cfg")
    db.audit(user["email"], "email.cleared", "", ip_of(request))
    return mailer.public_cfg()


class TestBody(BaseModel):
    to: str = ""


@router.post("/api/email/test")
def email_test(body: TestBody, request: Request, user: dict = Depends(admin_only)):
    to = _email(body.to or user["email"])
    try:
        subject, text, html = mailer.test_email()
        mailer.send(to, subject, text, html)
    except mailer.MailError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "email.test", to, ip_of(request))
    return {"ok": True, "to": to}


@router.get("/api/security")
def security_get(user: dict = Depends(admin_only)):
    return security_cfg()


class SecurityBody(BaseModel):
    require_2fa: bool


@router.put("/api/security")
def security_put(body: SecurityBody, request: Request, user: dict = Depends(admin_only)):
    db.sset("security_cfg", {"require_2fa": body.require_2fa})
    security_cfg(fresh=True)
    db.audit(user["email"], "security.require_2fa", "on" if body.require_2fa else "off", ip_of(request))
    return security_cfg()
