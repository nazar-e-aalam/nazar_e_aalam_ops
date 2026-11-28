"""Notifications: an in-panel feed (bell icon) plus email to the admins when a deployment or a server set-up fails.

Settings > Notifications (stored as `notify_cfg`):  email on/off, also on success, extra recipients."""
import logging
import re
import threading
import time

from ..core import config, mailer
from ..db import db
from ..db.models import Notification, User

log = logging.getLogger("ops.notify")
DEFAULTS = {"email": True, "on_success": False, "extra_emails": ""}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_cfg() -> dict:
    return {**DEFAULTS, **(db.sget("notify_cfg") or {})}


def set_cfg(new: dict) -> dict:
    cfg = get_cfg()
    for k in ("email", "on_success"):
        if k in new:
            cfg[k] = bool(new[k])
    if "extra_emails" in new:
        items = [e.strip().lower() for e in re.split(r"[\s,;]+", str(new["extra_emails"] or "")) if e.strip()]
        bad = [e for e in items if not _EMAIL.match(e)]
        if bad:
            raise ValueError("Not an email address: " + ", ".join(bad[:3]))
        cfg["extra_emails"] = ", ".join(items)
    db.sset("notify_cfg", cfg)
    return cfg


def recipients() -> list:
    admins = db.all(User, User.role == "admin", User.disabled == 0, cols=[User.email])
    extra = [e for e in re.split(r"[\s,;]+", get_cfg()["extra_emails"] or "") if e]
    seen, out = set(), []
    for e in [a["email"] for a in admins] + extra:
        if "@" in e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def add(level: str, kind: str, title: str, body: str = "", link: str = "", mail: tuple | None = None) -> int:
    """Store a notification; if `mail` = (subject, text, html) is given and email is set up, send it in the background."""
    nid = db.insert(Notification, ts=time.time(), level=level, kind=kind, title=title[:200], body=body[:4000], link=link[:255])
    if mail and get_cfg()["email"] and mailer.configured():
        threading.Thread(target=_send, args=(nid, mail), daemon=True, name="notify-mail").start()
    return nid


def _send(nid: int, mail: tuple) -> None:
    subject, text, html = mail
    sent = 0
    for to in recipients():
        try:
            mailer.send(to, subject, text, html)
            sent += 1
        except mailer.MailError as e:
            db.audit("system", "notify.email_failed", f"{to}: {e}")
    if sent:
        db.update(Notification, Notification.id == nid, emailed=1)


def feed(user_id: int, limit: int = 30) -> dict:
    rows = db.all(Notification, order=Notification.id.desc(), limit=limit)
    seen = int(db.sget(f"notif_seen:{user_id}", 0) or 0)
    return {"items": [dict(r, unread=r["id"] > seen) for r in rows], "unread": sum(1 for r in rows if r["id"] > seen)}


def mark_seen(user_id: int) -> None:
    top = db.get(Notification, order=Notification.id.desc(), cols=[Notification.id])
    db.sset(f"notif_seen:{user_id}", top["id"] if top else 0)


# ---- deployments and server set-up ---------------------------------------------------------------------------
def _tail(dep_id: int, n: int = 25) -> str:
    from . import deploy
    text, _ = deploy.read_log(dep_id, 0)
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[-n:])[-3000:]


def run_finished(d: dict, status: str, error: str | None, target: dict | None, failed_stage: str = "") -> None:
    """Called when any deployment, rollback or server set-up ends."""
    if status == "cancelled":
        return
    kind, name = d["kind"], (target or {}).get("name", "server")
    ok = status == "success"
    if ok and not (get_cfg()["on_success"] or kind == "bootstrap"):
        return
    link = f"{config.OPS_URL}/#/deploy/{d['id']}"
    sha = d.get("short_sha") or ""
    what = {"bootstrap": "Server set-up", "rollback": "Rollback"}.get(kind, "Deployment")
    if kind == "bootstrap":
        link = f"{config.OPS_URL}/#/servers/{(target or {}).get('id', 0)}"
    if ok:
        title = f"{what} finished on {name}" if kind != "bootstrap" else f"Server {name} is ready"
        summary = "Everything passed." if kind != "bootstrap" else "Packages, Python and the deploy folder are set up, and a test build passed (or was skipped, see the log)."
        level = "success"
    else:
        title = f"{what} #{d['id']} failed on {name}"
        summary = error or "It failed. See the log for details."
        level = "error"
    facts = [("Server", name)]
    if sha or d.get("message"):
        facts.append(("Commit", f"{sha}  {d.get('message') or ''}".strip()))
    if failed_stage and not ok:
        facts.append(("Failed at", failed_stage))
    facts.append(("Triggered by", d.get("triggered_by") or d.get("trigger") or "-"))
    hint = ""
    if not ok and kind == "deploy":
        hint = "The version that was running before is still running." if not d.get("rolled_back") else "The previous version was restored automatically."
    mail = mailer.notification_email(level, title, summary, facts, "" if ok else _tail(d["id"]), hint, link,
                                     "Open server" if kind == "bootstrap" else "Open deployment")
    add(level, f"{kind}.{status}", title, summary, f"#/deploy/{d['id']}" if kind != "bootstrap" else f"#/servers/{(target or {}).get('id', 0)}", mail)
