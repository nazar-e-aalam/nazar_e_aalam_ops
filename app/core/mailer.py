"""Outgoing email over SMTP (standard library). Settings come from Settings > Email (stored in the database),
falling back to OPS_SMTP_* environment variables. With no host configured, email is simply off and the panel
falls back to copyable links (handy in development)."""
import smtplib
import time
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ..db import db
from . import config

TIMEOUT = 15


class MailError(Exception):
    pass


def cfg() -> dict:
    c = dict(config.SMTP)
    c.update({k: v for k, v in (db.sget("smtp_cfg") or {}).items() if v not in (None, "")})
    c["port"] = int(c.get("port") or 587)
    c["security"] = c.get("security") if c.get("security") in ("starttls", "ssl", "none") else "starttls"
    return c


def configured() -> bool:
    c = cfg()
    return bool(c["host"] and c["from_email"])


def public_cfg() -> dict:
    c = cfg()
    stored = db.sget("smtp_cfg") or {}
    return {"host": c["host"], "port": c["port"], "security": c["security"], "username": c["username"],
            "password_set": bool(c["password"]), "from_email": c["from_email"], "from_name": c["from_name"],
            "configured": configured(), "source": "panel" if stored.get("host") else ("env" if config.SMTP["host"] else "none")}


def save_cfg(host, port, security, username, password, from_email, from_name) -> None:
    old = db.sget("smtp_cfg") or {}
    db.sset("smtp_cfg", {"host": host.strip(), "port": int(port), "security": security, "username": username.strip(),
                         "password": old.get("password", "") if password is None else password,
                         "from_email": from_email.strip(), "from_name": from_name.strip() or "Nazar Ops"})


def send(to: str, subject: str, text: str, html_body: str | None = None) -> None:
    c = cfg()
    if not (c["host"] and c["from_email"]):
        raise MailError("Email is not configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((c["from_name"], c["from_email"]))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=c["from_email"].split("@")[-1])
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        ctx = ssl.create_default_context()
        if c["security"] == "ssl":
            s = smtplib.SMTP_SSL(c["host"], c["port"], timeout=TIMEOUT, context=ctx)
        else:
            s = smtplib.SMTP(c["host"], c["port"], timeout=TIMEOUT)
        with s:
            s.ehlo()
            if c["security"] == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
            if c["username"]:
                s.login(c["username"], c["password"])
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server rejected the username or password")
    except smtplib.SMTPRecipientsRefused:
        raise MailError("The mail server refused the recipient address")
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Could not send: {getattr(e, 'strerror', None) or e}")


# ---- templates ------------------------------------------------------------------------------------------------
# HTML lives in app/templates/email/*.html (table layout + inline styles: works in Gmail, Outlook and Apple Mail,
# with a dark-mode variant). Every message also gets a plain-text part for clients that do not show HTML.
BRAND = "Nazar Ops"
ACCENTS = {"info": "#ffd60a", "success": "#30d9a0", "warning": "#ff9f1c", "error": "#ff4964"}
_env = Environment(loader=FileSystemLoader(str(config.BASE_DIR / "app" / "templates" / "email")),
                   autoescape=select_autoescape(["html"]), undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def render(name: str, subject: str, preheader: str = "", title: str = "", badge: str = "", level: str = "info", **ctx) -> str:
    return _env.get_template(name + ".html").render(
        subject=subject, preheader=preheader or title, title=title or subject, badge=badge, accent=ACCENTS.get(level, ACCENTS["info"]),
        brand=BRAND, panel_url=config.OPS_URL, panel_host=config.OPS_URL.split("://")[-1], **ctx)


def _text(title: str, *parts: str) -> str:
    return "\n\n".join([title] + [p for p in parts if p]) + f"\n\n-- {BRAND} ({config.OPS_URL})\n"


def invite_email(to: str, inviter: str, role: str, link: str, days: int) -> tuple:
    subject = f"{inviter} invited you to {BRAND}"
    html_body = render("invite", subject, preheader=f"Accept your invitation (valid {days} days)", title="You're invited",
                       badge="Invitation", inviter=inviter, role=role, link=link, days=days)
    text = _text("You're invited", f"{inviter} invited you to the {BRAND} panel as {role}.", f"Accept the invitation: {link}",
                 f"The link works once and expires in {days} days. If you weren't expecting it, ignore this email.")
    return subject, text, html_body


def reset_email(link: str, minutes: int) -> tuple:
    subject = f"Reset your {BRAND} password"
    html_body = render("reset", subject, preheader=f"This link expires in {minutes} minutes", title="Reset your password",
                       badge="Security", link=link, minutes=minutes)
    text = _text("Reset your password", f"We received a request to reset the password of your {BRAND} account.",
                 f"Choose a new password: {link}", f"The link works once and expires in {minutes} minutes. If you didn't ask for it, ignore this email.")
    return subject, text, html_body


def notification_email(level: str, title: str, summary: str, facts: list | None = None, log: str = "", hint: str = "",
                       link: str = "", button: str = "") -> tuple:
    """Deployment / server set-up alerts. `facts` is a list of (label, value) pairs."""
    badge = {"error": "Failed", "success": "Succeeded", "warning": "Attention"}.get(level, "Notice")
    subject = f"[{BRAND}] {title}"
    html_body = render("notification", subject, preheader=summary[:120], title=title, badge=badge, level=level,
                       summary=summary, facts=facts or [], log=log, hint=hint, link=link, button=button)
    lines = [f"{k}: {v}" for k, v in (facts or [])]
    text = _text(title, summary, "\n".join(lines), ("Last lines of the log:\n" + log) if log else "", hint, f"Open: {link}" if link else "")
    return subject, text, html_body


def test_email() -> tuple:
    subject = f"{BRAND}: test email"
    c = cfg()
    html_body = render("test", subject, preheader="Your email settings work", title="Email is working", badge="Test", level="success",
                       host=c["host"], sent_at=time.strftime("%Y-%m-%d %H:%M:%S %Z"))
    return subject, _text("Email is working", "Your email settings work."), html_body


def preview(kind: str) -> str:
    """Sample HTML for Settings > Email (what each message looks like), no mail is sent."""
    link = config.OPS_URL + "/#/example-link"
    if kind == "invite":
        return invite_email("", "Alex", "operator", link, 7)[2]
    if kind == "reset":
        return reset_email(link, 60)[2]
    if kind == "deploy_failed":
        return notification_email("error", "Deployment #42 failed on web-1", "Tests failed (python -m pytest -q exited with 1), the running version was not touched.",
                                  [("Server", "web-1"), ("Commit", "a1b2c3d  Fix login redirect"), ("Failed at", "Run tests"), ("Triggered by", "auto-deploy")],
                                  "FAILED tests/test_login.py::test_redirect - AssertionError\n1 failed, 24 passed in 3.2s",
                                  "Nothing was changed on the server: the previous version is still running.", link + "/deploy/42", "Open deployment")[2]
    if kind == "setup_ok":
        return notification_email("success", "Server web-1 is ready", "Packages, Python and the deploy folder are set up, and a test build passed.",
                                  [("Server", "web-1"), ("Python", "3.12.3"), ("Tests", "passed")], link=link + "/servers", button="Open servers")[2]
    if kind == "test":
        return test_email()[2]
    raise KeyError(kind)


PREVIEWS = ("invite", "reset", "deploy_failed", "setup_ok", "test")
