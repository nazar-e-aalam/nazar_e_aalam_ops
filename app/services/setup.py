"""First-run setup: domain + DNS guidance, writing .env, restart, and the 'setup done' flag."""
import os
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path

from ..core import config, db

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
_ip_cache = {"t": 0.0, "v": ""}


def is_done() -> bool:
    return bool(db.sget("setup_done"))


def mark_done(email: str) -> None:
    db.sset("setup_done", {"at": time.time(), "by": email})


def clean(domain: str, sub: str):
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split(":")[0].strip(".")
    if d.startswith("www."):
        d = d[4:]
    s = (sub or "ops").strip().lower().strip(".")
    if not _HOST.match(d):
        raise ValueError("Enter your domain like example.com (no http://, no path)")
    if not _LABEL.match(s):
        raise ValueError("Subdomain may only contain letters, digits and dashes (for example: ops)")
    return d, s


def server_ip() -> str:
    """Public IPv4 of this server (cached 10 min). Empty string if it cannot be found."""
    if time.time() - _ip_cache["t"] < 600 and _ip_cache["v"]:
        return _ip_cache["v"]
    ip = ""
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nazar-ops"}), timeout=4) as r:
                v = r.read().decode().strip()
                if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", v):
                    ip = v
                    break
        except Exception:
            continue
    _ip_cache.update(t=time.time(), v=ip)
    return ip


def resolve(host: str) -> list:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def dns_check(domain: str, sub: str, ip: str) -> list:
    """Look up the root + panel hostnames and compare with the server IP."""
    out = []
    for host, purpose in ((domain, "Public downloads page"), (f"{sub}.{domain}", "Ops panel")):
        found = resolve(host)
        if not found:
            st, msg = "missing", "No DNS record found yet. Add it at your domain provider, then check again (can take 1 to 30 minutes)."
        elif ip and ip in found:
            st, msg = "ok", "Points to this server."
        elif ip:
            st, msg = "wrong", f"Points to {', '.join(found)} instead of {ip}. Edit the record, or turn off the Cloudflare proxy (grey cloud) until HTTPS works."
        else:
            st, msg = "found", f"Resolves to {', '.join(found)} (could not detect this server's IP to compare)."
        out.append({"host": host, "purpose": purpose, "status": st, "found": found, "message": msg})
    return out


def env_path() -> Path:
    return config.ENV_FILE or (config.BASE_DIR / ".env")


def write_env(updates: dict) -> str:
    """Set KEY=value lines in .env (keeps comments and other lines). Atomic, mode 600."""
    p = env_path()
    lines = p.read_text().splitlines() if p.exists() else []
    todo = dict(updates)
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=", ln)
        if m and m.group(1) in todo:
            lines[i] = f"{m.group(1)}={todo.pop(m.group(1))}"
    for k, v in todo.items():
        lines.append(f"{k}={v}")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return str(p)


def can_write_env() -> bool:
    p = env_path()
    return os.access(p, os.W_OK) if p.exists() else os.access(p.parent, os.W_OK)


def under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))


def schedule_restart() -> None:
    """Exit the process shortly; systemd (Restart=always) brings it back with the new .env."""
    threading.Timer(1.2, lambda: os._exit(3)).start()
