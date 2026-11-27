"""Outgoing email over SMTP (standard library). Settings come from Settings > Email (stored in the database),
falling back to OPS_SMTP_* environment variables. With no host configured, email is simply off and the panel
falls back to copyable links (handy in development)."""
import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config, db

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


def _page(title: str, intro: str, button: str, link: str, foot: str) -> tuple:
    t = html.escape
    text = f"{title}\n\n{intro}\n\n{button}: {link}\n\n{foot}\n"
    body = f"""<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#1b1f27">
<h2 style="margin:0 0 12px">{t(title)}</h2><p style="line-height:1.5">{t(intro)}</p>
<p style="margin:24px 0"><a href="{t(link)}" style="background:#2f6feb;color:#fff;text-decoration:none;padding:11px 20px;border-radius:8px;display:inline-block;font-weight:600">{t(button)}</a></p>
<p style="font-size:13px;color:#5b6472;line-height:1.5">Or paste this link into your browser:<br><span style="word-break:break-all">{t(link)}</span></p>
<p style="font-size:13px;color:#5b6472">{t(foot)}</p></div>"""
    return text, body


def invite_email(to: str, inviter: str, role: str, link: str, days: int) -> tuple:
    return ("You're invited to Nazar Ops",) + _page(
        "You're invited to Nazar Ops", f"{inviter} invited you to the Nazar Ops panel as {role}. Set your name and password to get started.",
        "Accept invitation", link, f"This link works once and expires in {days} days. If you weren't expecting it, ignore this email.")


def reset_email(link: str, minutes: int) -> tuple:
    return ("Reset your Nazar Ops password",) + _page(
        "Reset your password", "We received a request to reset the password for your Nazar Ops account.",
        "Choose a new password", link, f"This link works once and expires in {minutes} minutes. If you didn't ask for this, you can ignore it; your password stays the same.")
