"""Deploy targets: this server or remote servers over SSH (key based), access checks incl. sudo,
and the database settings (local or external) that are injected into the deployed .env."""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from ..core import config, db

SSH_DIR = config.DATA_DIR / "ssh"
KNOWN = SSH_DIR / "known_hosts"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_HOST = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$|^[0-9a-fA-F:]+$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
DB_DEFAULTS = {"mode": "local", "engine": "mysql", "host": "", "port": 3306, "name": "", "user": "", "password": "", "ssl": False,
               "keys": {"host": "DB_HOST", "port": "DB_PORT", "name": "DB_NAME", "user": "DB_USER", "password": "DB_PASSWORD", "url": "DATABASE_URL"}}


class TargetError(Exception):
    pass


# ---- storage ---------------------------------------------------------------------------
def local_target() -> dict:
    chk = db.sget("local_checks") or {}
    return {"id": 0, "name": "This server", "kind": "local", "host": "", "port": 22, "user": "", "root": "", "restart_cmd": "",
            "health_url": "", "key_mode": "", "db_json": json.dumps(db.sget("local_db") or {}), "checks": json.dumps(chk or None),
            "verified": 1 if chk.get("ok") else 0, "checked_at": chk.get("at")}


def get(tid: int):
    if int(tid) == 0:
        return local_target()
    return db.one("SELECT * FROM targets WHERE id=?", (int(tid),))


def rows() -> list:
    return [local_target()] + db.query("SELECT * FROM targets ORDER BY id")


def db_cfg(t: dict) -> dict:
    try:
        raw = json.loads(t.get("db_json") or "{}")
    except ValueError:
        raw = {}
    cfg = json.loads(json.dumps(DB_DEFAULTS))
    keys = raw.pop("keys", None)
    cfg.update({k: v for k, v in raw.items() if k in cfg and k != "keys"})
    if isinstance(keys, dict):
        cfg["keys"].update({k: v for k, v in keys.items() if k in cfg["keys"] and isinstance(v, str)})
    return cfg


def shape(t: dict, with_key: bool = True) -> dict:
    c = db_cfg(t)
    try:
        checks = json.loads(t.get("checks") or "null")
    except ValueError:
        checks = None
    out = {"id": t["id"], "name": t["name"], "kind": t["kind"], "host": t.get("host") or "", "port": t.get("port") or 22,
           "user": t.get("user") or "", "root": t.get("root") or "", "restart_cmd": t.get("restart_cmd") or "",
           "health_url": t.get("health_url") or "", "key_mode": t.get("key_mode") or "", "verified": bool(t.get("verified")),
           "checked_at": t.get("checked_at"), "checks": checks,
           "db": {**{k: v for k, v in c.items() if k != "password"}, "has_password": bool(c["password"])}}
    if t["kind"] == "ssh" and with_key:
        out["public_key"] = public_key(t["id"])
        out["authorize_cmd"] = authorize_cmd(t["id"])
        out["fingerprint"] = known_fingerprint(t.get("host") or "", t.get("port") or 22)
    return out


def validate(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    host = (body.get("host") or "").strip()
    user = (body.get("user") or "").strip()
    root = (body.get("root") or "").strip().rstrip("/")
    try:
        port = int(body.get("port") or 22)
    except (TypeError, ValueError):
        raise TargetError("Port must be a number")
    if not _NAME.match(name):
        raise TargetError("Give the server a short name (letters, numbers, spaces, dash)")
    if not host or not _HOST.match(host):
        raise TargetError("Enter the server's hostname or IP address (for example 203.0.113.10 or app.example.com)")
    if not 1 <= port <= 65535:
        raise TargetError("Port must be between 1 and 65535")
    if not _USER.match(user):
        raise TargetError("Enter the SSH user name (for example deploy or ubuntu)")
    if root and (not root.startswith("/") or ".." in root or not re.fullmatch(r"[\w./-]+", root)):
        raise TargetError("Deploy folder must be an absolute path like /srv/nazar_e_aalam_panel")
    hu = (body.get("health_url") or "").strip()
    if hu and not re.match(r"^https?://", hu):
        raise TargetError("Health URL must start with http:// or https://")
    return {"name": name, "host": host, "port": port, "user": user, "root": root,
            "restart_cmd": (body.get("restart_cmd") or "").strip(), "health_url": hu}


def create(body: dict) -> int:
    v = validate(body)
    if db.one("SELECT id FROM targets WHERE name=?", (v["name"],)):
        raise TargetError("A server with this name already exists")
    tid = db.execute("INSERT INTO targets(name,kind,host,port,user,root,restart_cmd,health_url,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (v["name"], "ssh", v["host"], v["port"], v["user"], v["root"], v["restart_cmd"], v["health_url"], time.time()))
    try:
        generate_key(tid)
    except TargetError:
        db.execute("DELETE FROM targets WHERE id=?", (tid,))
        raise
    return tid


def update(tid: int, body: dict) -> None:
    t = get(tid)
    if not t or t["kind"] != "ssh":
        raise TargetError("Server not found")
    v = validate(body)
    if db.one("SELECT id FROM targets WHERE name=? AND id<>?", (v["name"], tid)):
        raise TargetError("A server with this name already exists")
    changed = (v["host"], v["port"], v["user"], v["root"], v["restart_cmd"]) != (t["host"], t["port"], t["user"], t["root"], t["restart_cmd"])
    db.execute("UPDATE targets SET name=?,host=?,port=?,user=?,root=?,restart_cmd=?,health_url=?,verified=? WHERE id=?",
               (v["name"], v["host"], v["port"], v["user"], v["root"], v["restart_cmd"], v["health_url"], 0 if changed else t["verified"], tid))


def delete(tid: int) -> None:
    if tid == 0:
        raise TargetError("The local server cannot be removed")
    db.execute("DELETE FROM targets WHERE id=?", (tid,))
    for p in (key_path(tid), key_path(tid).with_suffix(".pub")):
        try:
            p.unlink()
        except OSError:
            pass


def save_db(tid: int, body: dict) -> dict:
    t = get(tid)
    if not t:
        raise TargetError("Server not found")
    old = db_cfg(t)
    cfg = json.loads(json.dumps(DB_DEFAULTS))
    cfg["mode"] = "external" if body.get("mode") == "external" else "local"
    engine = body.get("engine") or "mysql"
    if engine not in ("mysql", "mariadb", "postgres"):
        raise TargetError("Engine must be MySQL, MariaDB or PostgreSQL")
    cfg["engine"] = engine
    cfg["host"] = (body.get("host") or "").strip()
    try:
        cfg["port"] = int(body.get("port") or (5432 if engine == "postgres" else 3306))
    except (TypeError, ValueError):
        raise TargetError("Database port must be a number")
    cfg["name"], cfg["user"] = (body.get("name") or "").strip(), (body.get("user") or "").strip()
    cfg["password"] = body.get("password") if body.get("password") else old["password"]   # blank keeps the saved one
    cfg["ssl"] = bool(body.get("ssl"))
    given = body.get("keys") or {}
    for k in cfg["keys"]:
        v = (given.get(k) or "").strip()
        if k == "url" and k in given and v == "":
            cfg["keys"][k] = ""
        elif v:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", v):
                raise TargetError(f"Env key names must look like DB_HOST (got {v})")
            cfg["keys"][k] = v
    if cfg["mode"] == "external":
        if not cfg["host"] or not re.fullmatch(r"[A-Za-z0-9._-]+|[0-9a-fA-F:]+", cfg["host"]):
            raise TargetError("Enter the database host (for example db.example.com or an IP)")
        if not 1 <= cfg["port"] <= 65535:
            raise TargetError("Database port must be between 1 and 65535")
        if not cfg["name"] or not cfg["user"]:
            raise TargetError("Database name and user are required")
    if tid == 0:
        db.sset("local_db", cfg)
    else:
        db.execute("UPDATE targets SET db_json=? WHERE id=?", (json.dumps(cfg), tid))
    return cfg


def db_complete(t: dict) -> list:
    c = db_cfg(t)
    if c["mode"] != "external":
        return []
    return [f for f, v in (("host", c["host"]), ("database name", c["name"]), ("user", c["user"]), ("password", c["password"])) if not v]


def env_overrides(t: dict) -> dict:
    """KEY->value pairs written into the deployed .env when an external database is configured."""
    c = db_cfg(t)
    if c["mode"] != "external":
        return {}
    k = c["keys"]
    out = {k["host"]: c["host"], k["port"]: str(c["port"]), k["name"]: c["name"], k["user"]: c["user"], k["password"]: c["password"]}
    if k.get("url"):
        from urllib.parse import quote
        scheme = {"mysql": "mysql+pymysql", "mariadb": "mysql+pymysql", "postgres": "postgresql"}[c["engine"]]
        url = f"{scheme}://{quote(c['user'], safe='')}:{quote(c['password'], safe='')}@{c['host']}:{c['port']}/{quote(c['name'], safe='')}"
        out[k["url"]] = url + ("" if not c["ssl"] else "?sslmode=require" if c["engine"] == "postgres" else "?ssl=true")
    return out


# ---- ssh keys --------------------------------------------------------------------------
def key_path(tid: int) -> Path:
    return SSH_DIR / f"target-{int(tid)}"


def _need_tools():
    if not (shutil.which("ssh") and shutil.which("ssh-keygen")):
        raise TargetError("The ssh client is not installed on the ops server. Install it: sudo apt install openssh-client")


def generate_key(tid: int) -> str:
    _need_tools()
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    kp = key_path(tid)
    for p in (kp, kp.with_suffix(".pub")):
        p.unlink(missing_ok=True)
    r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"nazar-ops-target-{tid}", "-f", str(kp)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise TargetError("Could not create the key: " + (r.stderr or r.stdout)[:200])
    os.chmod(kp, 0o600)
    db.execute("UPDATE targets SET key_mode='generated', verified=0 WHERE id=?", (tid,))
    return public_key(tid)


def set_private_key(tid: int, text: str) -> str:
    _need_tools()
    text = (text or "").replace("\r\n", "\n").strip() + "\n"
    if "PRIVATE KEY" not in text:
        raise TargetError("That does not look like a private key (it should start with -----BEGIN … PRIVATE KEY-----)")
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    kp = key_path(tid)
    tmp = kp.with_name(kp.name + ".new")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(tmp)], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise TargetError("The key could not be read. Keys protected by a passphrase are not supported, use one without a passphrase.")
    os.replace(tmp, kp)
    _pub(tid).write_text(r.stdout.strip() + " nazar-ops-imported\n")
    db.execute("UPDATE targets SET key_mode='imported', verified=0 WHERE id=?", (tid,))
    return public_key(tid)


def _pub(tid: int) -> Path:
    kp = key_path(tid)
    return kp.with_name(kp.name + ".pub")


def public_key(tid: int) -> str:
    try:
        return _pub(tid).read_text().strip()
    except OSError:
        return ""


def authorize_cmd(tid: int) -> str:
    return "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo " + shlex.quote(public_key(tid)) + " >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"


def _known_name(host: str, port) -> str:
    return host if int(port) == 22 else f"[{host}]:{port}"


def known_fingerprint(host: str, port: int) -> str:
    if not host or not KNOWN.exists() or not shutil.which("ssh-keygen"):
        return ""
    r = subprocess.run(["ssh-keygen", "-F", _known_name(host, port), "-f", str(KNOWN)], capture_output=True, text=True)
    line = next((l for l in r.stdout.splitlines() if l and not l.startswith("#")), "")
    parts = line.split()
    if len(parts) < 3:
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as f:
        f.write(" ".join(parts[1:3]) + "\n")
        tmp = f.name
    try:
        o = subprocess.run(["ssh-keygen", "-lf", tmp], capture_output=True, text=True).stdout.split()
        return o[1] if len(o) > 1 else ""
    finally:
        os.unlink(tmp)


def forget_host(t: dict) -> None:
    if KNOWN.exists() and shutil.which("ssh-keygen"):
        subprocess.run(["ssh-keygen", "-R", _known_name(t["host"], t["port"]), "-f", str(KNOWN)], capture_output=True)
    db.execute("UPDATE targets SET verified=0 WHERE id=?", (t["id"],))


def ssh_base(t: dict, learn: bool = False) -> list:
    _need_tools()
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    return ["ssh", "-i", str(key_path(t["id"])), "-p", str(t["port"]), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", f"StrictHostKeyChecking={'accept-new' if learn else 'yes'}", "-o", f"UserKnownHostsFile={KNOWN}",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "LogLevel=ERROR",
            f"{t['user']}@{t['host']}"]


def exec_script(t: dict, script: str, timeout: int = 40, learn: bool = False):
    """Run a bash script on the target (this server or over ssh). Returns (rc, output)."""
    argv = ["bash", "-s"] if t["kind"] == "local" else ssh_base(t, learn) + ["bash -s"]
    try:
        r = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except (OSError, TargetError) as e:
        return 127, str(e)


def _ssh_hint(out: str) -> str:
    o = out.lower()
    if "permission denied" in o:
        return "The server refused the key. Add the public key to ~/.ssh/authorized_keys of that user (command shown in the guide)."
    if "host key verification failed" in o or "remote host identification has changed" in o:
        return "The server's host key changed. If you reinstalled it, press 'Forget host key' and test again. Otherwise someone may be intercepting the connection."
    if "connection refused" in o:
        return "Nothing is listening on that port. Check the port number and that sshd is running."
    if "timed out" in o or "no route" in o or "network is unreachable" in o:
        return "Cannot reach the server. Check the IP/host and the firewall / security group (allow this server's IP on the SSH port)."
    if "could not resolve" in o or "name or service not known" in o:
        return "The host name does not resolve. Check the spelling or use the IP address."
    return out.strip()[-300:]


# ---- access check ------------------------------------------------------------------------
def restart_parts(restart_cmd: str):
    rc = (restart_cmd or "").strip()
    needs = rc.startswith("sudo ")
    rest = rc[5:].strip() if needs else rc
    unit = re.search(r"systemctl\s+(?:restart|reload|start)\s+([\w@.\-]+)", rest)
    return needs, rest, (unit.group(1) if unit else "")


def check(t: dict, cfg: dict) -> dict:
    """Probe the target (python, tar, write access, sudo for the restart command ...). The result is stored."""
    root = (t.get("root") or cfg["deploy_root"]).rstrip("/") or "/"
    restart = t.get("restart_cmd") or cfg["restart_cmd"]
    needs_sudo, rest, unit = restart_parts(restart)
    py = shlex.quote(cfg.get("python") or "python3")
    script = f"""
echo "whoami=$(whoami)"; echo "uid=$(id -u)"; echo "os=$(uname -sr)"
echo "python=$(command -v {py} >/dev/null 2>&1 && {py} --version 2>&1 || echo missing)"
echo "venv=$({py} -c 'import venv' >/dev/null 2>&1 && echo yes || echo no)"
echo "tar=$(command -v tar >/dev/null && echo yes || echo no)"
echo "curl=$( (command -v curl >/dev/null || command -v wget >/dev/null) && echo yes || echo no)"
R={shlex.quote(root)}
if mkdir -p "$R/releases" "$R/shared" 2>/dev/null && [ -w "$R/releases" ]; then echo "root=yes"; else echo "root=no"; fi
echo "free_kb=$(df -Pk "$R" 2>/dev/null | tail -1 | awk '{{print $4}}')"
if sudo -n true >/dev/null 2>&1; then echo "sudo_all=yes"; else echo "sudo_all=no"; fi
"""
    if needs_sudo:
        script += f'if sudo -n -l {rest} >/dev/null 2>&1; then echo "sudo_cmd=yes"; else echo "sudo_cmd=no"; fi\n'
    if unit:
        script += f'if systemctl cat {shlex.quote(unit)} >/dev/null 2>&1; then echo "unit=yes"; else echo "unit=no"; fi\n'
    t0 = time.time()
    rc, out = exec_script(t, script, 60, learn=True)
    ms = int((time.time() - t0) * 1000)
    kv = dict(l.split("=", 1) for l in out.splitlines() if re.match(r"^[a-z_]+=", l))
    checks = []

    def add(key, label, ok, detail="", fix="", grant=False):
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail, "fix": fix, "grant": grant})
    if t["kind"] == "ssh":
        if not kv:
            add("connect", "SSH connection", False, _ssh_hint(out))
            res = {"ok": False, "at": time.time(), "ms": ms, "checks": checks, "needs_sudo": needs_sudo, "sudoers": sudoers_line(t, cfg)}
            _save(t, res)
            return res
        add("connect", "SSH connection", True, f"{t['user']}@{t['host']}:{t['port']} in {ms} ms")
        fp = known_fingerprint(t["host"], t["port"])
        add("hostkey", "Server identity (host key)", True, fp or "saved",
            "Compare it with the output of: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub (run on the server). Only keep it if it matches.")
    add("os", "System", True, f"{kv.get('whoami', '?')} · {kv.get('os', '?')}")
    py_ok = kv.get("python", "missing") != "missing"
    add("python", f"Python ({cfg.get('python') or 'python3'})", py_ok, kv.get("python", ""), "" if py_ok else "Install it: sudo apt install python3 python3-venv")
    if py_ok:
        add("venv", "python3-venv module", kv.get("venv") == "yes", "", "" if kv.get("venv") == "yes" else "Install it: sudo apt install python3-venv")
    add("tar", "tar", kv.get("tar") == "yes", "", "" if kv.get("tar") == "yes" else "Install it: sudo apt install tar")
    add("curl", "curl or wget (health check)", kv.get("curl") == "yes" or None, "", "" if kv.get("curl") == "yes" else "Install it: sudo apt install curl")
    root_ok = kv.get("root") == "yes"
    add("root", f"Deploy folder {root}", root_ok, "writable" if root_ok else "cannot create or write here",
        "" if root_ok else f"On the server run: sudo mkdir -p {root} && sudo chown {kv.get('whoami', 'USER')} {root}")
    try:
        free = int(kv.get("free_kb", "0")) * 1024
        low = free <= 500 * 1024 * 1024
        add("disk", "Free disk space", None if low else True, f"{free / 1e9:.1f} GB free", "Less than 500 MB free, deployments may fail." if low else "")
    except ValueError:
        pass
    if needs_sudo:
        if kv.get("uid") == "0":
            add("sudo", "sudo for the restart command", True, "running as root, sudo is not needed")
        elif kv.get("sudo_all") == "yes" or kv.get("sudo_cmd") == "yes":
            add("sudo", "sudo for the restart command", True, f"allowed without a password: sudo {rest}")
        else:
            add("sudo", "sudo for the restart command", False, f"'sudo {rest}' asks for a password, so a deployment could not restart the service.",
                "Press Grant access and type the user's sudo password once (it is never stored), or add the sudoers line below yourself.", grant=True)
    else:
        add("sudo", "Restart command", True, "does not use sudo" if restart else "no restart command set")
    if unit:
        add("unit", f"systemd service {unit}", True if kv.get("unit") == "yes" else None, "found" if kv.get("unit") == "yes" else "not found yet",
            "" if kv.get("unit") == "yes" else "Create the service on that server first (Settings → Server can generate the unit file).")
    required = {"connect", "python", "venv", "tar", "root", "sudo"}
    ok = all(c["ok"] for c in checks if c["key"] in required)
    res = {"ok": ok, "at": time.time(), "ms": ms, "checks": checks, "needs_sudo": needs_sudo, "sudoers": sudoers_line(t, cfg)}
    _save(t, res)
    return res


def _save(t: dict, res: dict) -> None:
    if t["id"] == 0:
        db.sset("local_checks", res)
    else:
        db.execute("UPDATE targets SET checks=?, verified=?, checked_at=? WHERE id=?", (json.dumps(res), 1 if res["ok"] else 0, res["at"], t["id"]))


def sudoers_line(t: dict, cfg: dict) -> str:
    needs, rest, _ = restart_parts(t.get("restart_cmd") or cfg["restart_cmd"])
    if not needs:
        return ""
    user = t.get("user") or os.environ.get("USER") or "deploy"
    return f"{user} ALL=(root) NOPASSWD: {rest}"


def grant_sudo(t: dict, cfg: dict, password: str) -> dict:
    """Install a sudoers rule allowing exactly the restart command without a password.
    The password is used once (piped to sudo -S), never stored or logged."""
    needs, rest, _ = restart_parts(t.get("restart_cmd") or cfg["restart_cmd"])
    if not needs:
        raise TargetError("The restart command does not use sudo, nothing to grant.")
    if not password:
        raise TargetError("Enter the sudo password of that user")
    first, _, args = rest.partition(" ")
    user = t.get("user") or os.environ.get("USER") or "deploy"
    safe = re.sub(r"[^a-z0-9_-]", "", user)
    inner = f"""set -e
B=$(command -v {shlex.quote(first)}) || {{ echo "command not found: {first}" >&2; exit 20; }}
F=/etc/sudoers.d/zz-nazar-ops-{safe}
printf '%s\\n' "{user} ALL=(root) NOPASSWD: $B {args}" > "$F.tmp"
chmod 440 "$F.tmp"
visudo -cf "$F.tmp" >/dev/null
mv "$F.tmp" "$F"
echo GRANTED"""
    remote = "sudo -S -p '' sh -c " + shlex.quote(inner)
    argv = ["bash", "-c", remote] if t["kind"] == "local" else ssh_base(t) + [remote]
    try:
        r = subprocess.run(argv, input=password + "\n", capture_output=True, text=True, timeout=40, errors="replace")
    except subprocess.TimeoutExpired:
        raise TargetError("Timed out while talking to the server")
    out = r.stdout + r.stderr
    if "GRANTED" in r.stdout:
        return {"granted": True}
    low = out.lower()
    if "incorrect password" in low or "sorry, try again" in low:
        raise TargetError("Wrong sudo password")
    if "not in the sudoers" in low or "may not run sudo" in low:
        raise TargetError(f"User {user} is not allowed to use sudo at all. Log in as an administrator on that server and add the line shown below.")
    raise TargetError("Could not install the rule: " + out.strip()[-250:])


# ---- database test --------------------------------------------------------------------------
def _tcp(host: str, port: int, timeout: float = 6.0) -> dict:
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": int((time.time() - t0) * 1000), "error": ""}
    except OSError as e:
        return {"ok": False, "ms": None, "error": str(e)[:120]}


def db_test(t: dict, cfg_in: dict) -> dict:
    stored = db_cfg(t)
    c = {**stored, **{k: v for k, v in cfg_in.items() if k != "password"}}
    c["password"] = cfg_in.get("password") or stored["password"]
    host = (c.get("host") or "").strip()
    try:
        port = int(c.get("port") or 3306)
    except (TypeError, ValueError):
        raise TargetError("Port must be a number")
    if not host or not re.fullmatch(r"[A-Za-z0-9._-]+|[0-9a-fA-F:]+", host):
        raise TargetError("Enter the database host first")
    res = {"panel": _tcp(host, port), "auth": None, "from_target": None}
    if res["panel"]["ok"] and c["engine"] in ("mysql", "mariadb"):
        try:
            import pymysql
            kw = dict(host=host, port=port, user=c["user"], password=c["password"], database=c["name"] or None, connect_timeout=6, read_timeout=6)
            if c.get("ssl"):
                kw["ssl"] = {"check_hostname": False}
            conn = pymysql.connect(**kw)
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                ver = cur.fetchone()[0]
            conn.close()
            res["auth"] = {"ok": True, "detail": f"Logged in, server version {ver}"}
        except ImportError:
            res["auth"] = {"ok": None, "detail": "Install PyMySQL on the ops server (pip install PyMySQL) to test the login."}
        except Exception as e:
            msg = str(e)
            res["auth"] = {"ok": False, "detail": msg[:200], "fix": "Check user and password, and that this user may connect from the ops server's IP address." if "denied" in msg.lower() else ""}
    elif res["panel"]["ok"] and c["engine"] == "postgres":
        try:
            with socket.create_connection((host, port), timeout=6) as s:
                s.sendall(bytes.fromhex("0000000804d2162f"))
                s.settimeout(4)
                b = s.recv(16)
            good = len(b) == 1 and b in (b"S", b"N")
            res["auth"] = {"ok": good, "detail": "PostgreSQL server answered" if good else "That port does not speak the PostgreSQL protocol"}
        except OSError as e:
            res["auth"] = {"ok": False, "detail": str(e)[:120]}
    rc, out = exec_script(t, f'timeout 6 bash -c "</dev/tcp/{host}/{port}" >/dev/null 2>&1 && echo OPEN || echo CLOSED\n', 30)
    res["from_target"] = {"ok": "OPEN" in out, "where": t["name"],
                          "error": "" if "OPEN" in out else ("connection refused or blocked from that server" if "CLOSED" in out else out.strip()[-120:])}
    return res
