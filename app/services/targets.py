"""Deploy targets: this server or remote servers over SSH (key based), access checks incl. sudo,
and the one-time server bootstrap that runs after sudo access is granted (see services/bootstrap.py)."""
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..core import config
from ..db import db
from ..db.models import Target

SSH_DIR = config.DATA_DIR / "ssh"
KNOWN = SSH_DIR / "known_hosts"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_HOST = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$|^[0-9a-fA-F:]+$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")

class TargetError(Exception):
    pass


# ---- storage ---------------------------------------------------------------------------
def local_target() -> dict:
    chk = db.sget("local_checks") or {}
    return {"id": 0, "name": "This server", "kind": "local", "host": "", "port": 22, "user": "", "root": "", "restart_cmd": "",
            "health_url": "", "key_mode": "", "checks": json.dumps(chk or None),
            "verified": 1 if chk.get("ok") else 0, "checked_at": chk.get("at")}


def get(tid: int):
    if int(tid) == 0:
        return local_target()
    return db.get(Target, Target.id == int(tid))


def rows() -> list:
    return [local_target()] + db.all(Target, order=Target.id)


def shape(t: dict, with_key: bool = True) -> dict:
    try:
        checks = json.loads(t.get("checks") or "null")
    except ValueError:
        checks = None
    out = {"id": t["id"], "name": t["name"], "kind": t["kind"], "host": t.get("host") or "", "port": t.get("port") or 22,
           "user": t.get("user") or "", "root": t.get("root") or "", "restart_cmd": t.get("restart_cmd") or "",
           "health_url": t.get("health_url") or "", "key_mode": t.get("key_mode") or "", "verified": bool(t.get("verified")),
           "checked_at": t.get("checked_at"), "checks": checks, "python": t.get("python") or "",
           "bootstrap": db.sget(f"bootstrap:{int(t['id'])}")}
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
    if db.exists(Target, Target.name == v["name"]):
        raise TargetError("A server with this name already exists")
    tid = db.insert(Target, name=v["name"], kind="ssh", host=v["host"], port=v["port"], user=v["user"], root=v["root"],
                    restart_cmd=v["restart_cmd"], health_url=v["health_url"], created_at=time.time())
    try:
        generate_key(tid)
    except TargetError:
        db.delete(Target, Target.id == tid)
        raise
    return tid


def update(tid: int, body: dict) -> None:
    t = get(tid)
    if not t or t["kind"] != "ssh":
        raise TargetError("Server not found")
    v = validate(body)
    if db.exists(Target, Target.name == v["name"], Target.id != tid):
        raise TargetError("A server with this name already exists")
    changed = (v["host"], v["port"], v["user"], v["root"], v["restart_cmd"]) != (t["host"], t["port"], t["user"], t["root"], t["restart_cmd"])
    db.update(Target, Target.id == tid, name=v["name"], host=v["host"], port=v["port"], user=v["user"], root=v["root"],
              restart_cmd=v["restart_cmd"], health_url=v["health_url"], verified=0 if changed else t["verified"])


def delete(tid: int) -> None:
    if tid == 0:
        raise TargetError("The local server cannot be removed")
    db.delete(Target, Target.id == tid)
    for p in (key_path(tid), key_path(tid).with_suffix(".pub")):
        try:
            p.unlink()
        except OSError:
            pass


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
    db.update(Target, Target.id == tid, key_mode="generated", verified=0)
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
    db.update(Target, Target.id == tid, key_mode="imported", verified=0)
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
    db.update(Target, Target.id == t["id"], verified=0)


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
        db.update(Target, Target.id == t["id"], checks=json.dumps(res), verified=1 if res["ok"] else 0, checked_at=res["at"])


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
