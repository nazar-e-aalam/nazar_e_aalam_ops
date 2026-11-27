"""Deployment engine for the panel/API repo.

Per commit:   fetch exact commit -> write versioned .env -> (re)use virtualenv ->
              install -> tests -> pre-switch hook -> atomic `current` symlink swap ->
              restart -> health check -> prune.  If restart/health fails after the
              swap, the previous release is restored automatically.
Layout:       <deploy_root>/releases/<id>-<sha7>/   <deploy_root>/current -> release
              <deploy_root>/shared/venvs/<hash>/    (shared between releases)
"""
import base64
import hashlib
import json
import os
import posixpath
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..core import config, db, envstore
from . import github as gh, setup, targets as tgs

STAGES = [("fetch", "Fetch commit"), ("env", "Write .env"), ("upload", "Upload to server"), ("venv", "Virtualenv"), ("install", "Install deps"),
          ("test", "Run tests"), ("prepare", "Pre-switch"), ("switch", "Switch release"),
          ("restart", "Restart"), ("health", "Health check"), ("cleanup", "Cleanup")]

DEFAULTS = {
    "branch": "main",
    "deploy_root": "/srv/nazar_e_aalam_panel",
    "env_target": ".env",
    "python": "python3",
    "install_cmd": "pip install -r requirements.txt",
    "test_enabled": True,
    "test_cmd": "python -m pytest -q",
    "pre_switch_cmd": "",
    "restart_cmd": "sudo systemctl restart nazar-panel",
    "health_url": "http://127.0.0.1:8000/health",
    "health_timeout": 45,
    "keep_releases": 5,
    "command_timeout": 1800,
    "auto_deploy": False,
    "poll_seconds": 60,
    "webhook_enabled": True,
    "require_env": True,      # block deployments until every required env key has a value
    "required_env": "",       # extra keys that must be set (comma or newline separated)
    "auto_target": 0,         # server used by auto deploy and the webhook (0 = this server)
}

started_at = time.time()
_q: "queue.Queue[int]" = queue.Queue()
_ctxs: dict = {}
_threads_started = False


class Failure(Exception):
    pass


# ---- config --------------------------------------------------------------------
def get_cfg() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(db.sget("deploy_cfg") or {})
    return cfg


def set_cfg(new: dict) -> dict:
    cfg = get_cfg()
    for k, default in DEFAULTS.items():
        if k not in new:
            continue
        v = new[k]
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
        else:
            v = str(v).strip()
        cfg[k] = v
    if not os.path.isabs(cfg["deploy_root"]) or cfg["deploy_root"].strip("/") == "":
        raise ValueError("Deploy root must be an absolute path such as /srv/nazar_e_aalam_panel")
    if not re.fullmatch(r"[\w./\-]+", cfg["branch"]):
        raise ValueError("Branch name contains invalid characters")
    if os.path.isabs(cfg["env_target"]) or ".." in Path(cfg["env_target"]).parts or not cfg["env_target"]:
        raise ValueError("Env target must be a relative path inside the release, e.g. .env")
    cfg["keep_releases"] = max(2, min(50, cfg["keep_releases"]))
    cfg["health_timeout"] = max(5, min(600, cfg["health_timeout"]))
    cfg["poll_seconds"] = max(20, min(3600, cfg["poll_seconds"]))
    cfg["command_timeout"] = max(60, min(7200, cfg["command_timeout"]))
    db.sset("deploy_cfg", cfg)
    return cfg


# ---- logging / process helpers -------------------------------------------------
def _redact(s: str) -> str:
    tok = gh.token()
    if tok:
        s = s.replace(tok, "***").replace(base64.b64encode(f"x-access-token:{tok}".encode()).decode(), "***")
    return s


class Log:
    def __init__(self, dep_id: int):
        self.path = config.LOG_DIR / f"deploy-{dep_id}.log"
        self.f = open(self.path, "a", buffering=1, encoding="utf-8", errors="replace")

    def write(self, line: str) -> None:
        self.f.write(f"{time.strftime('%H:%M:%S')}  {_redact(line)}\n")

    def close(self) -> None:
        self.f.close()


def read_log(dep_id: int, offset: int = 0):
    p = config.LOG_DIR / f"deploy-{int(dep_id)}.log"
    if not p.exists():
        return "", 0
    with open(p, "rb") as f:
        f.seek(offset)
        chunk = f.read(256 * 1024)
    return chunk.decode("utf-8", "replace"), offset + len(chunk)


class Ctx:
    def __init__(self, dep_id: int, kind: str):
        self.id, self.kind = dep_id, kind
        self.log = Log(dep_id)
        self.proc = None
        self.cancelled = False
        self.stages = {k: {"key": k, "label": l, "status": "pending", "detail": "", "started": None, "finished": None}
                       for k, l in STAGES}

    def stage(self, key: str, status: str, detail: str = None, label: str = None) -> None:
        s = self.stages.setdefault(key, {"key": key, "label": label or key, "status": "pending", "detail": "",
                                         "started": None, "finished": None})
        if status == "running":
            s["started"] = time.time()
        if status in ("success", "failed", "skipped"):
            s["finished"] = time.time()
        s["status"] = status
        if detail is not None:
            s["detail"] = detail
        db.execute("UPDATE deployments SET stages=? WHERE id=?", (json.dumps(list(self.stages.values())), self.id))


def _child_env(venv: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OPS_") and k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    if venv:
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = f"{venv}/bin:" + env.get("PATH", "")
    return env


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        threading.Timer(5, lambda: _hard_kill(p)).start()
    except (ProcessLookupError, PermissionError):
        pass


def _hard_kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _stream(ctx: Ctx, args, cwd, env: dict, timeout: int, shown: str, shell: bool = False, stdin_text: str = None) -> int:
    ctx.log.write("$ " + shown)
    p = subprocess.Popen(args, cwd=str(cwd), env=env, shell=shell, executable="/bin/bash" if shell else None,
                         stdin=subprocess.PIPE if stdin_text is not None else None,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                         bufsize=1, start_new_session=True)
    ctx.proc = p
    timer = threading.Timer(timeout, lambda: (ctx.log.write(f"timed out after {timeout}s, killing"), _kill(p)))
    timer.start()
    try:
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text)
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        for line in p.stdout:
            ctx.log.write(line.rstrip("\n"))
        rc = p.wait()
    finally:
        timer.cancel()
        ctx.proc = None
    if ctx.cancelled:
        raise Failure("Cancelled by user")
    return rc


def run(ctx: Ctx, cmd, cwd: Path, env: dict, timeout: int, show: str = None) -> int:
    shell = isinstance(cmd, str)
    return _stream(ctx, cmd, cwd, env, timeout, show or (cmd if shell else " ".join(cmd)), shell=shell)


# ---- GitHub commit lookup ------------------------------------------------------
def resolve_commit(ref: str) -> dict:
    c = gh.jget(f"{gh.repo_path('panel')}/commits/{urllib.parse.quote(ref, safe='')}", ttl=5)
    commit = c.get("commit") or {}
    return {"sha": c["sha"], "message": (commit.get("message") or "").split("\n")[0][:200],
            "author": (commit.get("author") or {}).get("name") or (c.get("author") or {}).get("login") or "",
            "branch": ref if not re.fullmatch(r"[0-9a-f]{7,40}", ref) else get_cfg()["branch"]}


# ---- queue ---------------------------------------------------------------------
class Blocked(Exception):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("Deployment blocked: " + "; ".join(p["title"] for p in problems))


def enqueue(commit: dict, user: str, trigger: str = "manual", kind: str = "deploy", release: str = None, target_id: int = 0) -> int:
    if kind == "deploy":
        dup = db.one("SELECT id FROM deployments WHERE sha=? AND kind='deploy' AND target_id=? AND status IN ('queued','running')",
                     (commit["sha"], target_id))
        if dup:
            return dup["id"]
        pf = preflight(target_id)
        if not pf["ok"]:
            raise Blocked(pf["problems"])
    stages = [{"key": k, "label": l, "status": "pending", "detail": "", "started": None, "finished": None}
              for k, l in STAGES]
    dep_id = db.execute(
        """INSERT INTO deployments(kind,status,sha,short_sha,message,author,branch,trigger,triggered_by,queued_at,release_name,stages,target_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kind, "queued", commit.get("sha"), (commit.get("sha") or "")[:7], commit.get("message", ""),
         commit.get("author", ""), commit.get("branch", ""), trigger, user, time.time(), release, json.dumps(stages), int(target_id)))
    _q.put(dep_id)
    return dep_id


def cancel(dep_id: int) -> str:
    d = db.one("SELECT status FROM deployments WHERE id=?", (dep_id,))
    if not d:
        raise ValueError("Deployment not found")
    if d["status"] == "queued":
        db.execute("UPDATE deployments SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                   (time.time(), dep_id))
        return "Cancelled before it started."
    ctx = _ctxs.get(dep_id)
    if d["status"] != "running" or not ctx:
        raise ValueError("This deployment is not running.")
    if ctx.stages["switch"]["status"] != "pending" and ctx.kind == "deploy":
        raise ValueError("Too late to cancel: the release is already switched. Use Roll back instead.")
    ctx.cancelled = True
    if ctx.proc:
        _kill(ctx.proc)
    return "Cancelling…"


# ---- filesystem helpers --------------------------------------------------------
def _active_name(root: Path):
    cur = root / "current"
    if cur.is_symlink():
        return os.path.basename(os.readlink(cur).rstrip("/"))
    return None


def _switch(root: Path, rel: Path) -> None:
    cur, tmp = root / "current", root / ".current.new"
    if cur.exists() and not cur.is_symlink():
        raise Failure(f"{cur} exists and is a real directory. Move it away first (e.g. mv {cur} {cur}.old) "
                      "so it can become a symlink to the active release.")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(rel, tmp)
    os.replace(tmp, cur)  # atomic rename: the service never sees a half-switched state


def wait_health(ctx: Ctx, url: str, timeout: int) -> bool:
    end, last = time.time() + timeout, ""
    time.sleep(1)
    while time.time() < end:
        try:
            t0 = time.time()
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nazar-ops-health"}),
                                        timeout=4) as r:
                if 200 <= r.status < 400:
                    ctx.log.write(f"health OK: HTTP {r.status} in {int((time.time() - t0) * 1000)} ms")
                    return True
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(getattr(e, "reason", e))[:100]
        time.sleep(1.5)
    ctx.log.write(f"health check did not pass within {timeout}s (last: {last})")
    return False


# ---- where a deployment runs: this server, or another one over ssh -----------------------
def eff_cfg(cfg: dict, t: dict) -> dict:
    c = dict(cfg)
    if t.get("root"):
        c["deploy_root"] = t["root"]
    for k in ("restart_cmd", "health_url"):
        if (t.get(k) or "").strip():
            c[k] = t[k].strip()
    return c


class LocalExec:
    remote = False

    def __init__(self, t: dict):
        self.t = t

    def run(self, ctx, cmd, cwd, timeout, venv=None, show=None):
        return run(ctx, cmd, Path(cwd), _child_env(Path(venv) if venv else None), timeout, show)

    def capture(self, cmd, timeout=30):
        try:
            r = subprocess.run(cmd, shell=True, executable="/bin/bash", capture_output=True, text=True, timeout=timeout,
                               env=_child_env(), errors="replace")
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    def active(self, root):
        return _active_name(Path(root))

    def switch(self, root, rel):
        _switch(Path(root), Path(rel))

    def is_dir(self, p):
        return Path(p).is_dir()

    def mkdirs(self, p):
        Path(p).mkdir(parents=True, exist_ok=True)

    def upload(self, ctx, local, rel):
        return

    def health(self, ctx, url, timeout):
        return wait_health(ctx, url, timeout)

    def cleanup(self, ctx, cfg, root):
        _cleanup(ctx, cfg, Path(root))


class SshExec:
    remote = True

    def __init__(self, t: dict):
        self.t = t

    def _base(self):
        return tgs.ssh_base(self.t)

    def run(self, ctx, cmd, cwd, timeout, venv=None, show=None):
        text = cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
        pre = f"cd {shlex.quote(str(cwd))} || exit 97\n"
        if venv:
            pre += f"export VIRTUAL_ENV={shlex.quote(str(venv))}\nexport PATH={shlex.quote(str(venv))}/bin:$PATH\n"
        try:
            base = self._base()
        except tgs.TargetError as e:
            raise Failure(str(e))
        return _stream(ctx, base + ["bash -s"], "/", _child_env(), timeout, f"[{self.t['host']}] {show or text}", stdin_text=pre + text + "\n")

    def capture(self, cmd, timeout=30):
        try:
            r = subprocess.run(self._base() + ["bash -s"], input=cmd + "\n", capture_output=True, text=True, timeout=timeout, errors="replace")
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except tgs.TargetError as e:
            return 127, str(e)

    def active(self, root):
        rc, out = self.capture(f"readlink {shlex.quote(root)}/current 2>/dev/null")
        return os.path.basename(out.strip().rstrip("/")) if rc == 0 and out.strip() else None

    def switch(self, root, rel):
        q = shlex.quote
        rc, out = self.capture(f"cd {q(root)} && if [ -e current ] && [ ! -L current ]; then exit 9; fi; ln -sfn {q(str(rel))} .current.new && mv -Tf .current.new current")
        if rc == 9:
            raise Failure(f"{root}/current exists on the server and is a real folder. Move it away (mv {root}/current {root}/current.old) so it can become a link to the active release.")
        if rc != 0:
            raise Failure("Could not switch the release on the server: " + out[-200:])

    def is_dir(self, p):
        return self.capture(f"test -d {shlex.quote(str(p))}")[0] == 0

    def mkdirs(self, p):
        rc, out = self.capture(f"mkdir -p {shlex.quote(str(p))}")
        if rc != 0:
            raise Failure(f"Cannot create {p} on the server: {out[-160:]}. Run the access check on the Servers page.")

    def upload(self, ctx, local, rel):
        q = shlex.quote
        ctx.log.write(f"Uploading release to {self.t['user']}@{self.t['host']}:{rel}")
        try:
            base = self._base()
        except tgs.TargetError as e:
            raise Failure(str(e))
        tar = subprocess.Popen(["tar", "-C", str(local), "--exclude=__pycache__", "-cf", "-", "."], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ssh = subprocess.Popen(base + [f"rm -rf {q(rel)} && mkdir -p {q(rel)} && tar -C {q(rel)} -xf -"], stdin=tar.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        tar.stdout.close()
        ctx.proc = ssh
        try:
            out, _ = ssh.communicate(timeout=1800)
            terr = tar.stderr.read().decode(errors="replace")
            tar.wait()
        except subprocess.TimeoutExpired:
            _kill(ssh)
            raise Failure("Upload timed out")
        finally:
            ctx.proc = None
        if ctx.cancelled:
            raise Failure("Cancelled by user")
        if ssh.returncode != 0 or tar.returncode != 0:
            raise Failure("Upload failed: " + tgs._ssh_hint(out or terr))

    def health(self, ctx, url, timeout):
        q = shlex.quote(url)
        script = (f"if command -v curl >/dev/null 2>&1; then curl -s -m 4 -o /dev/null -w '%{{http_code}}' {q}; "
                  f"else wget -q -T 4 -O /dev/null {q} && echo 200 || echo 000; fi")
        end, last = time.time() + timeout, ""
        time.sleep(1)
        while time.time() < end:
            t0 = time.time()
            rc, out = self.capture(script, 20)
            code = re.findall(r"\d{3}$", out.strip())
            if code and 200 <= int(code[0]) < 400:
                ctx.log.write(f"health OK (checked from {self.t['host']}): HTTP {code[0]} in {int((time.time() - t0) * 1000)} ms")
                return True
            last = f"HTTP {code[0]}" if code else (out[-80:] or "no answer")
            time.sleep(1.5)
        ctx.log.write(f"health check did not pass within {timeout}s (last: {last})")
        return False

    def cleanup(self, ctx, cfg, root):
        q = shlex.quote
        script = f"""R={q(root)}; KEEP={int(cfg['keep_releases'])}
ACTIVE=$(basename "$(readlink "$R/current" 2>/dev/null)" 2>/dev/null)
cd "$R/releases" || exit 0
n=0
for d in $(ls -1 | sort -r); do n=$((n+1)); if [ $n -gt $KEEP ] && [ "$d" != "$ACTIVE" ]; then rm -rf -- "$d"; echo "removed $d"; fi; done
if [ -d "$R/shared/venvs" ]; then
  for v in "$R"/shared/venvs/*; do [ -d "$v" ] || continue; used=0
    for r in "$R"/releases/*; do [ "$(readlink -f "$r/.venv" 2>/dev/null)" = "$(readlink -f "$v")" ] && used=1; done
    if [ $used = 0 ] && [ $(( $(date +%s) - $(stat -c %Y "$v") )) -gt 3600 ]; then rm -rf -- "$v"; fi
  done
fi
"""
        rc, out = self.capture(script, 120)
        n = len([l for l in out.splitlines() if l.startswith("removed ")])
        ctx.log.write(f"Cleanup on {self.t['host']}: removed {n} old release(s).")
        ctx.stage("cleanup", "success", f"removed {n}")


def executor(t: dict):
    return LocalExec(t) if t["kind"] == "local" else SshExec(t)


# ---- readiness: nothing deploys until access + environment are complete ---------------------
def _prob(pid, title, detail="", href=""):
    return {"id": pid, "title": title, "detail": detail, "href": href}


def required_env_keys(cfg: dict) -> list:
    extra = [k for k in re.split(r"[\s,;]+", cfg.get("required_env") or "") if k]
    ex = db.sget("env_example_required") or []
    return sorted(set(extra) | set(ex))


def preflight(target_id: int = 0) -> dict:
    t = tgs.get(target_id)
    if not t:
        return {"ok": False, "problems": [_prob("target", "That server no longer exists", "", "#/servers")], "target": None}
    cfg = eff_cfg(get_cfg(), t)
    probs = []
    href = f"#/servers/{t['id']}"
    if t["kind"] == "ssh":
        if not tgs.public_key(t["id"]):
            probs.append(_prob("key", f"{t['name']} has no SSH key yet", "Open the server guide and create or paste a key.", href))
        elif not t["verified"]:
            probs.append(_prob("access", f"{t['name']} has not passed the access check", "Run the check: SSH login, write access and sudo for the restart command.", href))
    else:
        chk = db.sget("local_checks") or {}
        if time.time() - (chk.get("at") or 0) > 120:
            chk = tgs.check(t, cfg)
        if not chk.get("ok"):
            bad = [c["label"] for c in chk.get("checks", []) if c["ok"] is False]
            probs.append(_prob("access", "This server is not ready to deploy", ", ".join(bad) or "Run the access check.", href))
    miss = tgs.db_complete(t)
    if miss:
        probs.append(_prob("db", "External database settings are incomplete", "Missing: " + ", ".join(miss), href))
    if cfg["require_env"]:
        cur = envstore.current()
        if not cur:
            probs.append(_prob("env", "No environment (.env) has been added yet", "Add the variables your app needs in the Environment tab.", "#/env"))
        else:
            have = dict(envstore.parse(cur["content"])[0])
            have.update(tgs.env_overrides(t))
            missing = [k for k in required_env_keys(cfg) if not have.get(k)]
            if missing:
                probs.append(_prob("env_missing", f"{len(missing)} required environment variable(s) are empty or missing",
                                   ", ".join(missing[:12]) + (" …" if len(missing) > 12 else ""), "#/env"))
    return {"ok": not probs, "problems": probs, "target": {"id": t["id"], "name": t["name"], "kind": t["kind"]}}


def _restart_and_check(ctx: Ctx, cfg: dict, tg, root: str) -> None:
    ctx.stage("restart", "running")
    if cfg["restart_cmd"]:
        rc = tg.run(ctx, cfg["restart_cmd"], root, 300)
        if rc != 0:
            ctx.stage("restart", "failed", f"exit {rc}")
            hint = " (needs sudo without a password: open Servers and run the access check)" if "sudo" in cfg["restart_cmd"] else ""
            raise Failure(f"Restart command failed (exit {rc}){hint}")
        ctx.stage("restart", "success")
    else:
        ctx.log.write("No restart command configured, skipping.")
        ctx.stage("restart", "skipped", "not configured")
    if cfg["health_url"]:
        ctx.stage("health", "running", cfg["health_url"])
        if not tg.health(ctx, cfg["health_url"], cfg["health_timeout"]):
            ctx.stage("health", "failed", "unhealthy")
            raise Failure("Health check failed after restart")
        ctx.stage("health", "success")
    else:
        ctx.stage("health", "skipped", "not configured")


def _revert(ctx: Ctx, cfg: dict, tg, root: str, prev) -> bool:
    ctx.stage("rollback", "running", label="Rollback")
    if not prev or not tg.is_dir(f"{root}/releases/{prev}"):
        ctx.log.write("No previous release available to roll back to.")
        ctx.stage("rollback", "failed", "no previous release")
        return False
    try:
        tg.switch(root, f"{root}/releases/{prev}")
        ctx.log.write(f"Rolled `current` back to {prev}")
        if cfg["restart_cmd"]:
            tg.run(ctx, cfg["restart_cmd"], root, 300)
        ok = (not cfg["health_url"]) or tg.health(ctx, cfg["health_url"], cfg["health_timeout"])
        ctx.stage("rollback", "success" if ok else "failed", prev if ok else "previous release is unhealthy too")
        return ok
    except Exception as e:
        ctx.log.write(f"Rollback error: {e}")
        ctx.stage("rollback", "failed", str(e)[:100])
        return False


# ---- the deployment itself -----------------------------------------------------
def _fetch(ctx: Ctx, cfg: dict, d: dict, rel: Path) -> None:
    ctx.stage("fetch", "running", d["short_sha"])
    if rel.exists():
        shutil.rmtree(rel)
    rel.mkdir(parents=True)
    direct = f"{config.GITHUB_GIT_URL}/{gh.org()}/{gh.repos()['panel']}.git"
    attempts = []
    if gh.proxy_base():
        attempts.append((gh.proxied(direct), False))  # via gh-proxy, never sends the token
    if not gh.proxy_base() or config.GITHUB_PROXY_FALLBACK:
        attempts.append((direct, True))
    last_err = None
    for url, use_token in attempts:
        env, tok = _child_env(), gh.token()
        if use_token and tok and url.startswith("https://"):
            b64 = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraheader", GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {b64}")
        if rel.exists():
            shutil.rmtree(rel)
        rel.mkdir(parents=True)
        steps = [["git", "init", "-q"], ["git", "remote", "add", "origin", url],
                 ["git", "fetch", "--depth", "1", "-q", "origin", d["sha"]], ["git", "checkout", "-q", "FETCH_HEAD"]]
        last_err = None
        for argv in steps:
            if run(ctx, argv, rel, env, 600) != 0:
                last_err = argv
                break
        if last_err is None:
            ctx.log.write(f"Fetched via {'gh-proxy' if url != direct else 'github.com'}")
            break
        ctx.log.write("Fetch failed via " + ("gh-proxy" if url != direct else "github.com") + (", trying next source" if url != attempts[-1][0] else ""))
    if last_err is not None:
        ctx.stage("fetch", "failed")
        raise Failure("git failed (gh-proxy and direct both failed; check the repo/commit exists and the token can read it)")
    shutil.rmtree(rel / ".git", ignore_errors=True)
    (rel / ".release.json").write_text(json.dumps({"deployment": d["id"], "sha": d["sha"], "message": d["message"],
                                                   "branch": d["branch"], "deployed_at": time.time()}, indent=2))
    ctx.stage("fetch", "success", d["short_sha"])


def _write_env(ctx: Ctx, cfg: dict, d: dict, rel: Path, t: dict) -> None:
    ctx.stage("env", "running")
    example_required = []
    ex = rel / ".env.example"
    if ex.is_file():
        try:
            example_required = [k for k, v in envstore.parse(ex.read_text(errors="replace"))[0] if not v]
            db.sset("env_example_required", example_required)
        except OSError:
            pass
    cur = envstore.current()
    if not cur:
        if cfg["require_env"]:
            ctx.stage("env", "failed", "no .env added")
            raise Failure("No environment (.env) has been added yet. Add it in the Environment tab, nothing was changed on the server.")
        ctx.log.write("No .env has been uploaded in the panel yet, deploying without one.")
        ctx.stage("env", "skipped", "none uploaded")
        return
    content = cur["content"]
    over = tgs.env_overrides(t)
    if over:
        content = envstore.apply(content, over)
        c = tgs.db_cfg(t)
        ctx.log.write(f"External {c['engine']} database from server '{t['name']}': {c['host']}:{c['port']}/{c['name']} (user {c['user']}), {len(over)} keys written")
    if cfg["require_env"]:
        have = dict(envstore.parse(content)[0])
        need = sorted(set(required_env_keys(cfg)) | set(example_required))
        missing = [k for k in need if not have.get(k)]
        if missing:
            ctx.stage("env", "failed", f"{len(missing)} missing")
            raise Failure("Missing environment variables: " + ", ".join(missing) + ". Add them in the Environment tab, nothing was changed on the server.")
    target = (rel / cfg["env_target"]).resolve()
    if rel.resolve() not in target.parents:
        raise Failure("Env target escapes the release directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)
    db.execute("UPDATE deployments SET env_version=? WHERE id=?", (cur["version"], d["id"]))
    n = len(envstore.parse(content)[0])
    ctx.log.write(f"Wrote {cfg['env_target']} (version {cur['version']}, {n} keys)")
    ctx.stage("env", "success", f"v{cur['version']} · {n} keys")


def _prepare_venv(ctx: Ctx, cfg: dict, tg, root: str, rel: str, lrel: Path) -> str:
    ctx.stage("venv", "running")
    q = shlex.quote
    rc, pyver = tg.capture(f"{q(cfg['python'])} -c 'import sys;print(sys.version)'", 30)
    if rc != 0 or not pyver:
        ctx.stage("venv", "failed")
        raise Failure(f"Python interpreter '{cfg['python']}' not found" + (" on the server" if tg.remote else ""))
    req = lrel / "requirements.txt"
    blob = pyver.encode() + (req.read_bytes() if req.exists() else b"") + cfg["install_cmd"].encode()
    key = hashlib.sha256(blob).hexdigest()[:16]
    venv = f"{root}/shared/venvs/{key}"
    marker = f"{venv}/.nazar-ok"
    reuse = tg.capture(f"test -f {q(marker)}")[0] == 0
    if tg.run(ctx, f"test -x {q(venv)}/bin/python || (mkdir -p {q(root)}/shared/venvs && {q(cfg['python'])} -m venv {q(venv)})", rel, 600,
              show="create virtualenv") != 0:
        ctx.stage("venv", "failed")
        raise Failure("Could not create the virtualenv (is python3-venv installed?)")
    tg.run(ctx, f"rm -rf {q(rel)}/.venv && ln -s {q(venv)} {q(rel)}/.venv", rel, 60, show="link virtualenv")
    ctx.stage("venv", "success", "reused" if reuse else "created")
    if reuse:
        ctx.log.write("Dependencies unchanged, reusing the cached virtualenv.")
        ctx.stage("install", "skipped", "unchanged")
    else:
        ctx.stage("install", "running")
        if cfg["install_cmd"] and tg.run(ctx, cfg["install_cmd"], rel, cfg["command_timeout"], venv=venv) != 0:
            ctx.stage("install", "failed")
            raise Failure("Dependency install failed")
        tg.capture(f"touch {q(marker)}")
        ctx.stage("install", "success")
    return venv


def _do_deploy(ctx: Ctx, d: dict, cfg: dict, tg, t: dict) -> None:
    root = cfg["deploy_root"]
    tg.mkdirs(f"{root}/releases")
    name = f"{d['id']:05d}-{d['short_sha']}"
    rel = f"{root}/releases/{name}"
    lrel = (config.DATA_DIR / "stage" / name) if tg.remote else Path(rel)
    prev = tg.active(root)
    db.execute("UPDATE deployments SET release_name=?, prev_release=? WHERE id=?", (name, prev, d["id"]))
    try:
        _fetch(ctx, cfg, d, lrel)
        _write_env(ctx, cfg, d, lrel, t)
        if tg.remote:
            ctx.stage("upload", "running", t["host"])
            tg.upload(ctx, lrel, rel)
            ctx.stage("upload", "success", t["host"])
        else:
            ctx.stage("upload", "skipped", "same server")
        venv = _prepare_venv(ctx, cfg, tg, root, rel, lrel)

        if cfg["test_enabled"] and cfg["test_cmd"]:
            ctx.stage("test", "running")
            rc = tg.run(ctx, cfg["test_cmd"], rel, cfg["command_timeout"], venv=venv)
            if rc == 5 and "pytest" in cfg["test_cmd"]:
                ctx.log.write("pytest collected no tests; treating as a pass.")
                ctx.stage("test", "success", "no tests found")
            elif rc != 0:
                ctx.log.write("Tip: tests run inside the virtualenv built from requirements.txt, so the test tool "
                              "(e.g. pytest) must be listed there or installed by your install command.")
                ctx.stage("test", "failed", f"exit {rc}")
                raise Failure("Tests failed, the running version was not touched")
            else:
                ctx.stage("test", "success")
        else:
            ctx.stage("test", "skipped", "disabled")

        if cfg["pre_switch_cmd"]:
            ctx.stage("prepare", "running")
            if tg.run(ctx, cfg["pre_switch_cmd"], rel, cfg["command_timeout"], venv=venv) != 0:
                ctx.stage("prepare", "failed")
                raise Failure("Pre-switch command failed, the running version was not touched")
            ctx.stage("prepare", "success")
        else:
            ctx.stage("prepare", "skipped", "none")

        if ctx.cancelled:
            raise Failure("Cancelled by user")
        ctx.stage("switch", "running")
        tg.switch(root, rel)
        ctx.log.write(f"current -> {name}")
        ctx.stage("switch", "success", name)
        try:
            _restart_and_check(ctx, cfg, tg, root)
        except Failure as e:
            ctx.log.write(f"{e}. Restoring previous release…")
            ok = _revert(ctx, cfg, tg, root, prev)
            db.execute("UPDATE deployments SET rolled_back=? WHERE id=?", (1 if ok else 0, d["id"]))
            raise Failure(f"{e}. " + (f"Rolled back to {prev}." if ok else "Automatic rollback also failed, check the server!"))
        tg.cleanup(ctx, cfg, root)
    finally:
        if tg.remote:
            shutil.rmtree(lrel, ignore_errors=True)


def _do_rollback(ctx: Ctx, d: dict, cfg: dict, tg, t: dict) -> None:
    root = cfg["deploy_root"]
    target = d["release_name"]
    if not tg.is_dir(f"{root}/releases/{target}"):
        raise Failure(f"Release {target} no longer exists on {t['name']}")
    prev = tg.active(root)
    db.execute("UPDATE deployments SET prev_release=? WHERE id=?", (prev, d["id"]))
    for k in ("fetch", "env", "upload", "venv", "install", "test", "prepare", "cleanup"):
        ctx.stage(k, "skipped")
    ctx.stage("switch", "running")
    tg.switch(root, f"{root}/releases/{target}")
    ctx.stage("switch", "success", target)
    try:
        _restart_and_check(ctx, cfg, tg, root)
    except Failure as e:
        ctx.log.write(f"{e}. Restoring {prev}…")
        ok = _revert(ctx, cfg, tg, root, prev)
        raise Failure(f"{e}. " + (f"Restored {prev}." if ok else "Restore failed too, check the server!"))


def _cleanup(ctx: Ctx, cfg: dict, root: Path) -> None:
    ctx.stage("cleanup", "running")
    try:
        rels = sorted([p for p in (root / "releases").iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
        keep = {p.name for p in rels[: cfg["keep_releases"]]}
        keep.add(_active_name(root) or "")
        removed = 0
        for p in rels:
            if p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        used = set()
        for p in (root / "releases").iterdir():
            link = p / ".venv"
            if link.is_symlink():
                used.add(os.path.realpath(link))
        vdir = root / "shared" / "venvs"
        if vdir.is_dir():
            for v in vdir.iterdir():
                if str(v.resolve()) not in used and time.time() - v.stat().st_mtime > 3600:
                    shutil.rmtree(v, ignore_errors=True)
        ctx.log.write(f"Cleanup: removed {removed} old release(s).")
        ctx.stage("cleanup", "success", f"removed {removed}")
    except Exception as e:
        ctx.log.write(f"Cleanup warning: {e}")
        ctx.stage("cleanup", "success", "with warnings")


def execute(dep_id: int) -> None:
    d = db.one("SELECT * FROM deployments WHERE id=?", (dep_id,))
    if not d or d["status"] != "queued":
        return
    ctx = Ctx(dep_id, d["kind"])
    _ctxs[dep_id] = ctx
    db.execute("UPDATE deployments SET status='running', started_at=? WHERE id=?", (time.time(), dep_id))
    status, error = "success", None
    try:
        t = tgs.get(d.get("target_id") or 0)
        if not t:
            raise Failure("The server this deployment was meant for no longer exists")
        cfg = eff_cfg(get_cfg(), t)
        tg = executor(t)
        root = cfg["deploy_root"]
        tg.mkdirs(root)
        ctx.log.write(f"{'Rollback' if d['kind'] == 'rollback' else 'Deployment'} #{dep_id} "
                      f"{d['short_sha'] or ''} {d['message'] or ''} -> {t['name']}".strip())
        (_do_rollback if d["kind"] == "rollback" else _do_deploy)(ctx, d, cfg, tg, t)
        ctx.log.write("Done.")
    except Failure as e:
        status, error = ("cancelled" if ctx.cancelled else "failed"), str(e)
        ctx.log.write(f"FAILED: {error}")
    except Exception as e:  # unexpected
        status, error = "failed", f"{type(e).__name__}: {e}"
        ctx.log.write("UNEXPECTED ERROR:\n" + traceback.format_exc())
    finally:
        for s in ctx.stages.values():
            if s["status"] == "running":
                s["status"] = "failed" if status != "success" else "success"
        ctx.stage("fetch", ctx.stages["fetch"]["status"])  # persist final stage state
        for s in ctx.stages.values():
            if s["status"] == "pending" and status != "success":
                s["status"] = "skipped"
        db.execute("UPDATE deployments SET status=?, error=?, finished_at=?, stages=? WHERE id=?",
                   (status, error, time.time(), json.dumps(list(ctx.stages.values())), dep_id))
        ctx.log.close()
        _ctxs.pop(dep_id, None)
        db.audit(d["triggered_by"], f"deploy.{status}", f"#{dep_id} {d['short_sha'] or d['release_name'] or ''}")


# ---- workers -------------------------------------------------------------------
def _worker() -> None:
    while True:
        dep_id = _q.get()
        try:
            execute(dep_id)
        except Exception:
            traceback.print_exc()


def _poller() -> None:
    last = 0.0
    while True:
        time.sleep(10)
        try:
            cfg = get_cfg()
            if not cfg["auto_deploy"] or not gh.token() or not setup.is_done() or time.time() - last < cfg["poll_seconds"]:
                continue
            last = time.time()
            commit = resolve_commit(cfg["branch"])
            seen = db.sget("deploy_last_seen")
            if seen is None:
                row = db.one("SELECT sha FROM deployments WHERE kind='deploy' AND status='success' ORDER BY id DESC LIMIT 1")
                seen = row["sha"] if row else ""
            if commit["sha"] != seen:
                db.sset("deploy_last_seen", commit["sha"])
                try:
                    enqueue(commit, "auto-deploy", "poll", target_id=int(cfg["auto_target"]))
                    db.audit("auto-deploy", "deploy.queued", f"{commit['sha'][:7]} {commit['message']}")
                except Blocked as b:
                    db.audit("auto-deploy", "deploy.blocked", str(b)[:300])
                    db.sset("deploy_poll", {"ok": False, "at": time.time(), "error": str(b)[:200]})
                    continue
            db.sset("deploy_poll", {"ok": True, "at": time.time(), "sha": commit["sha"][:7]})
        except gh.GitHubError as e:
            db.sset("deploy_poll", {"ok": False, "at": time.time(), "error": e.message})
        except Exception:
            traceback.print_exc()


def start() -> None:
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    db.execute("UPDATE deployments SET status='failed', error='Interrupted: the ops panel restarted', finished_at=? "
               "WHERE status='running'", (time.time(),))
    for r in db.query("SELECT id FROM deployments WHERE status='queued' ORDER BY id"):
        _q.put(r["id"])
    threading.Thread(target=_worker, name="deploy-worker", daemon=True).start()
    threading.Thread(target=_poller, name="deploy-poller", daemon=True).start()


# ---- webhook -------------------------------------------------------------------
def handle_push(payload: dict):
    cfg = get_cfg()
    if not cfg["webhook_enabled"]:
        return 202, "webhook deploys are disabled"
    repo = (payload.get("repository") or {}).get("name")
    if repo != gh.repos()["panel"]:
        return 202, "ignored: other repository"
    if payload.get("ref") != f"refs/heads/{cfg['branch']}":
        return 202, f"ignored: not {cfg['branch']}"
    if payload.get("deleted"):
        return 202, "ignored: branch deleted"
    head = payload.get("head_commit") or {}
    sha = payload.get("after") or head.get("id")
    if not sha:
        return 400, "no commit in payload"
    commit = {"sha": sha, "message": (head.get("message") or "").split("\n")[0][:200],
              "author": (head.get("author") or {}).get("name", ""), "branch": cfg["branch"]}
    db.sset("deploy_last_seen", sha)
    try:
        dep_id = enqueue(commit, (payload.get("pusher") or {}).get("name") or "github", "webhook", target_id=int(cfg["auto_target"]))
    except Blocked as b:
        db.audit("webhook", "deploy.blocked", str(b)[:300])
        return 202, "blocked: " + str(b)
    db.audit("webhook", "deploy.queued", f"{sha[:7]} {commit['message']}")
    return 202, f"queued deployment #{dep_id}"


# ---- status for the UI ---------------------------------------------------------
def _meminfo():
    try:
        m = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
        return {"total": m["MemTotal"], "available": m.get("MemAvailable", m.get("MemFree", 0))}
    except Exception:
        return None


def probe(url: str):
    if not url:
        return None
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nazar-ops-health"}), timeout=3) as r:
            return {"ok": 200 <= r.status < 400, "code": r.status, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code, "ms": int((time.time() - t0) * 1000), "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(getattr(e, "reason", e))[:100]}


def releases_on_disk(target_id: int = 0) -> list:
    t = tgs.get(target_id) or tgs.get(0)
    cfg = eff_cfg(get_cfg(), t)
    root = cfg["deploy_root"]
    by_name = {r["release_name"]: r for r in db.query(
        "SELECT release_name,id,sha,short_sha,message,author,finished_at,status FROM deployments WHERE kind='deploy' AND release_name IS NOT NULL AND target_id=?", (t["id"],))}
    if t["kind"] == "local":
        rel_dir = Path(root) / "releases"
        if not rel_dir.is_dir():
            return []
        active, items = _active_name(Path(root)), []
        for p in sorted([x for x in rel_dir.iterdir() if x.is_dir()], key=lambda x: x.name, reverse=True):
            size = 0
            for dp, _, files in os.walk(p):
                for f in files:
                    try:
                        size += os.lstat(os.path.join(dp, f)).st_size
                    except OSError:
                        pass
            items.append((p.name, p.stat().st_mtime, size))
    else:
        key = ("rel", t["id"])
        hit = _rs_cache.get(key)
        if hit and time.time() - hit[0] < 10:
            active, items = hit[1]
        else:
            q = shlex.quote(root)
            rc, out = tgs.exec_script(t, f'cd {q}/releases 2>/dev/null || exit 0\necho "ACTIVE=$(basename "$(readlink {q}/current 2>/dev/null)" 2>/dev/null)"\n'
                                         f'for d in */; do d=${{d%/}}; [ -d "$d" ] && echo "REL $d $(stat -c %Y "$d") $(du -sk "$d" | cut -f1)"; done', 25)
            active, items = None, []
            for l in out.splitlines():
                if l.startswith("ACTIVE="):
                    active = l[7:].strip() or None
                elif l.startswith("REL "):
                    _, n, mt, kb = l.split()
                    items.append((n, float(mt), int(kb) * 1024))
            items.sort(key=lambda x: x[0], reverse=True)
            _rs_cache[key] = (time.time(), (active, items))
    out = []
    for name, mtime, size in items:
        dep = by_name.get(name) or {}
        out.append({"name": name, "active": name == active, "size": size, "mtime": mtime, "deployment": dep.get("id"),
                    "sha": dep.get("short_sha") or name.split("-")[-1], "message": dep.get("message", ""), "author": dep.get("author", "")})
    return out


_rs_cache: dict = {}


def _remote_status(t: dict, cfg: dict) -> dict:
    key = ("st", t["id"])
    hit = _rs_cache.get(key)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    q = shlex.quote
    root, hu = cfg["deploy_root"], cfg["health_url"]
    script = f"""R={q(root)}
echo "root=$([ -d "$R" ] && echo yes || echo no)"
echo "active=$(basename "$(readlink "$R/current" 2>/dev/null)" 2>/dev/null)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
echo "memt=$(awk '/MemTotal/{{print $2}}' /proc/meminfo 2>/dev/null)"; echo "mema=$(awk '/MemAvailable/{{print $2}}' /proc/meminfo 2>/dev/null)"
echo "disk=$(df -Pk "$R" 2>/dev/null || df -Pk / | tail -1)" | tail -1
"""
    if hu:
        script += f"echo \"health=$(curl -s -m 4 -o /dev/null -w '%{{http_code}}' {q(hu)} 2>/dev/null)\"\n"
    rc, out = tgs.exec_script(t, script, 25)
    kv = dict(l.split("=", 1) for l in out.splitlines() if re.match(r"^[a-z]+=", l))
    res = {"reachable": bool(kv)}
    try:
        disk = kv.get("disk", "").split()
        res["disk"] = {"total": int(disk[-5]) * 1024, "free": int(disk[-3]) * 1024} if len(disk) >= 6 else None
    except (ValueError, IndexError):
        res["disk"] = None
    try:
        res["mem"] = {"total": int(kv["memt"]) * 1024, "available": int(kv["mema"]) * 1024}
    except (KeyError, ValueError):
        res["mem"] = None
    try:
        res["load"] = [float(x) for x in kv.get("load", "").split()[:3]] or None
    except ValueError:
        res["load"] = None
    res["root_exists"], res["active"] = kv.get("root") == "yes", kv.get("active") or None
    if hu:
        code = kv.get("health", "")
        res["health"] = {"ok": code.isdigit() and 200 <= int(code) < 400, "code": int(code) if code.isdigit() and code != "000" else None,
                         "error": "" if code.isdigit() and 200 <= int(code) < 400 else ("no answer" if code in ("", "000") else f"HTTP {code}")}
    else:
        res["health"] = None
    _rs_cache[key] = (time.time(), res)
    return res


def server_status(target_id: int = 0) -> dict:
    t = tgs.get(target_id) or tgs.get(0)
    cfg = eff_cfg(get_cfg(), t)
    root = Path(cfg["deploy_root"])
    base = {"target": {"id": t["id"], "name": t["name"], "kind": t["kind"], "host": t.get("host") or ""}, "deploy_root": str(root),
            "ops_uptime": time.time() - started_at, "poll": db.sget("deploy_poll"),
            "busy": bool(db.one("SELECT id FROM deployments WHERE status IN ('queued','running') LIMIT 1"))}
    if t["kind"] == "ssh":
        r = _remote_status(t, cfg)
        active = r.get("active")
        disk, load, mem, health, root_exists = r.get("disk"), r.get("load"), r.get("mem"), r.get("health"), r.get("root_exists", False)
        base["unreachable"] = not r["reachable"]
    else:
        probe_root = root if root.exists() else root.parent
        try:
            du = shutil.disk_usage(probe_root if probe_root.exists() else "/")
            disk = {"total": du.total, "free": du.free}
        except OSError:
            disk = None
        active = _active_name(root) if root.exists() else None
        try:
            load = os.getloadavg()
        except OSError:
            load = None
        mem, health, root_exists = _meminfo(), probe(cfg["health_url"]), root.exists()
    cur = db.one("SELECT id,sha,short_sha,message,author,finished_at,env_version FROM deployments "
                 "WHERE kind='deploy' AND release_name=? AND target_id=? ORDER BY id DESC LIMIT 1", (active, t["id"])) if active else None
    base.update({"root_exists": root_exists, "active": active, "current": cur, "health": health, "load": load, "mem": mem, "disk": disk})
    return base


def shape(d: dict) -> dict:
    d = dict(d)
    try:
        d["stages"] = json.loads(d.get("stages") or "[]")
    except ValueError:
        d["stages"] = []
    tid = d.get("target_id") or 0
    t = tgs.get(tid) if tid else None
    d["target_name"] = t["name"] if t else ("This server" if not tid else f"server #{tid} (removed)")
    return d


