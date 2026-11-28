"""Server set-up: runs automatically right after sudo access is granted (or from the "Set up server" button).

  1 access    who are we, can we get root (root / passwordless sudo / the password typed once, kept in memory only)
  2 packages  python3, venv, pip, git, curl, tar, build tools   (apt, dnf or yum)
  3 python    pick an interpreter >= PYTHON_MIN that can create virtualenvs (installs a newer one if needed)
  4 folders   <deploy root>/releases + shared, owned by the deploy user
  5 clone     fetch the panel repo (latest commit of the branch) into a scratch folder
  6 venv + install deps (this warms the cache the first real deployment reuses)
  7 test      run the configured test command; a failure is reported and notified
  8 cleanup   remove the scratch folder

Nothing here touches the live release. Stages 5-7 are skipped (not failed) when no GitHub token is set yet."""
import shlex
import shutil
import time
from pathlib import Path

from ..core import config
from ..db import db
from ..db.models import Deployment, Target
from . import deploy, github as gh, targets as tgs
from .deploy import Failure

PYTHON_MIN = (3, 10)
APT_PKGS = "python3 python3-venv python3-pip git curl tar ca-certificates build-essential python3-dev libffi-dev libssl-dev"
RPM_PKGS = "python3 python3-pip git curl tar ca-certificates gcc make python3-devel libffi-devel openssl-devel"

# bash helpers shared by every stage. NZ_PW (the sudo password) is defined by the caller, only when needed.
PRELUDE = r'''
set -u
if [ "$(id -u)" = 0 ]; then NZ_MODE=root
elif sudo -n true 2>/dev/null; then NZ_MODE=nopass
elif [ -n "${NZ_PW:-}" ]; then NZ_MODE=pw
else NZ_MODE=none; fi
as_root() {
  case "$NZ_MODE" in
    root) "$@";;
    nopass) sudo -n "$@";;
    pw) printf '%s\n' "$NZ_PW" | sudo -S -p '' "$@";;
    *) echo "no root access (grant sudo first)" >&2; return 77;;
  esac
}
if command -v apt-get >/dev/null 2>&1; then NZ_PM=apt
elif command -v dnf >/dev/null 2>&1; then NZ_PM=dnf
elif command -v yum >/dev/null 2>&1; then NZ_PM=yum
else NZ_PM=none; fi
'''


def _script(body: str, password: str = "") -> str:
    head = f"NZ_PW={shlex.quote(password)}\n" if password else ""
    return head + PRELUDE + body


def _sh(ctx, tg, body: str, password: str, cwd: str, timeout: int, show: str) -> int:
    return tg.run_script(ctx, _script(body, password), cwd, timeout, show)


def _ver_tuple(text: str):
    import re
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# ---- the run ------------------------------------------------------------------------------------------------------------
def run(ctx, d: dict, cfg: dict, tg, t: dict, password: str) -> None:
    root = cfg["deploy_root"]
    q = shlex.quote
    cwd = "/"
    log = ctx.log.write

    # 1. access ----------------------------------------------------------------------------------------------------
    ctx.stage("access", "running")
    rc = _sh(ctx, tg, 'echo "user=$(whoami) uid=$(id -u) os=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-$(uname -sr)}")"\n'
                      'echo "root access: $NZ_MODE, package manager: $NZ_PM"\n'
                      'if [ "$NZ_MODE" = pw ]; then printf \'%s\\n\' "$NZ_PW" | sudo -S -p "" -v || { echo "WRONG_SUDO_PASSWORD"; exit 78; }; fi\n'
                      '[ "$NZ_MODE" != none ] || exit 77\n', password, cwd, 120, "check access")
    if rc != 0:
        ctx.stage("access", "failed", "no root access")
        raise Failure("No root access on the server. Press Grant access and enter the user's sudo password (it is used once and never stored).")
    ctx.stage("access", "success")

    # 2. packages --------------------------------------------------------------------------------------------------
    ctx.stage("packages", "running")
    body = f'''
case "$NZ_PM" in
  apt) as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq || echo "apt update had warnings"
       as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {APT_PKGS};;
  dnf) as_root dnf install -y -q {RPM_PKGS};;
  yum) as_root yum install -y -q {RPM_PKGS};;
  *) echo "No supported package manager (apt, dnf, yum). Install python3, venv, pip, git, curl and tar yourself."; exit 79;;
esac
'''
    if _sh(ctx, tg, body, password, cwd, 1200, "install system packages") != 0:
        ctx.stage("packages", "failed")
        raise Failure("Installing system packages failed (see the log above). Check the server has internet access and a working package mirror.")
    ctx.stage("packages", "success")

    # 3. python ----------------------------------------------------------------------------------------------------
    ctx.stage("python", "running")
    probe = r'''
best=""; bestv=0
for c in %s python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  v=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null) || continue
  "$c" -c 'import venv, ensurepip' >/dev/null 2>&1 || continue
  if [ "$v" -ge %d ] && [ "$v" -gt "$bestv" ]; then best="$c"; bestv="$v"; fi
done
[ -n "$best" ] && echo "FOUND=$best $("$best" --version 2>&1)" || echo "FOUND="
''' % (q(cfg.get("python") or "python3"), PYTHON_MIN[0] * 100 + PYTHON_MIN[1])
    rc, out = tg.capture(_script(probe, password), 60)
    found = next((l[6:].strip() for l in out.splitlines() if l.startswith("FOUND=")), "")
    if not found:
        log(f"No Python >= {PYTHON_MIN[0]}.{PYTHON_MIN[1]} with venv support yet, trying to install one…")
        _sh(ctx, tg, 'case "$NZ_PM" in apt) as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12 python3.12-venv python3.12-dev || '
                     'as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv || true;;\n'
                     '  dnf|yum) as_root "$NZ_PM" install -y -q python3.12 python3.12-pip || as_root "$NZ_PM" install -y -q python3.11 || true;; esac\n',
            password, cwd, 900, "install a newer Python")
        rc, out = tg.capture(_script(probe, password), 60)
        found = next((l[6:].strip() for l in out.splitlines() if l.startswith("FOUND=")), "")
    if not found:
        ctx.stage("python", "failed", f"need >= {PYTHON_MIN[0]}.{PYTHON_MIN[1]}")
        raise Failure(f"No Python {PYTHON_MIN[0]}.{PYTHON_MIN[1]}+ with the venv module on the server. Install one (e.g. sudo apt install python3.12 python3.12-venv) and run the set-up again.")
    exe, _, version = found.partition(" ")
    log(f"Python: {exe} ({version})")
    if exe != (cfg.get("python") or "python3") and t["id"]:
        db.update(Target, Target.id == t["id"], python=exe)
        cfg["python"] = exe
        log(f"This server will use {exe} for virtualenvs.")
    elif exe != (cfg.get("python") or "python3"):
        deploy.set_cfg({"python": exe})
        cfg["python"] = exe
    ctx.stage("python", "success", version)

    # 4. folders ---------------------------------------------------------------------------------------------------
    ctx.stage("folders", "running", root)
    body = f'''
R={q(root)}
as_root mkdir -p "$R/releases" "$R/shared/venvs" || exit 1
as_root chown -R "$(id -un)":"$(id -gn)" "$R" || exit 1
[ -w "$R/releases" ] && echo "deploy folder ready: $R" || exit 1
'''
    if _sh(ctx, tg, body, password, cwd, 120, "prepare deploy folder") != 0:
        ctx.stage("folders", "failed")
        raise Failure(f"Could not create or take ownership of {root}.")
    ctx.stage("folders", "success")
    db.sset(f"bootstrap:{t['id']}", {"at": time.time(), "ok": True, "tested": False, "dep": d["id"], "python": exe, "version": version})

    # 5-7. prove a real build works ------------------------------------------------------------------------------------
    scratch = f"{root}/shared/bootstrap/{d['id']}"
    lscratch = (config.DATA_DIR / "stage" / f"bootstrap-{d['id']}") if tg.remote else Path(scratch)
    try:
        if not gh.token():
            for k in ("fetch", "upload", "venv", "install", "test"):
                ctx.stage(k, "skipped", "no GitHub token yet")
            log("No GitHub token yet (Settings > GitHub): skipped the test build. Add a token and run the set-up again to check your tests.")
            return
        try:
            commit = deploy.resolve_commit(cfg["branch"])
        except gh.GitHubError as e:
            ctx.stage("fetch", "failed", e.message[:60])
            raise Failure("Could not read the repository: " + e.message)
        d2 = dict(d, sha=commit["sha"], short_sha=commit["sha"][:7], message=commit["message"], branch=commit["branch"])
        deploy._fetch(ctx, cfg, d2, lscratch)
        try:
            deploy._write_env(ctx, dict(cfg, require_env=False), d2, lscratch, t)   # tests may read .env; none yet is fine
        except Failure:
            pass
        ctx.stages.pop("env", None)
        if tg.remote:
            ctx.stage("upload", "running", t["host"])
            tg.upload(ctx, lscratch, scratch)
            ctx.stage("upload", "success", t["host"])
        else:
            ctx.stage("upload", "skipped", "same server")
        venv = deploy._prepare_venv(ctx, cfg, tg, root, scratch, lscratch)
        deploy.run_tests(ctx, cfg, tg, scratch, venv, "this commit would not have been deployed")
    finally:
        tg.capture(f"rm -rf {q(scratch)}", 60)
        if tg.remote:
            shutil.rmtree(lscratch, ignore_errors=True)
        ctx.stage("cleanup", "success")

    # remember + re-run the access check so the server shows as verified
    db.sset(f"bootstrap:{t['id']}", {"at": time.time(), "ok": True, "tested": True, "dep": d["id"], "python": exe, "version": version})
    try:
        tgs.check(t, deploy.eff_cfg(deploy.get_cfg(), tgs.get(t["id"]) or t))
    except Exception:  # noqa: BLE001
        pass


def state(tid: int) -> dict | None:
    return db.sget(f"bootstrap:{int(tid)}")


def start(t: dict, user: str, password: str = "") -> int:
    """Queue a set-up run. The sudo password (if any) is kept in memory only, until the run starts."""
    if db.exists(Deployment, Deployment.kind == "bootstrap", Deployment.target_id == t["id"],
                 Deployment.status.in_(("queued", "running"))):
        raise ValueError("A set-up is already running on this server.")
    return deploy.enqueue({"sha": "", "message": "Server set-up", "author": user, "branch": ""}, user, "setup", kind="bootstrap",
                          target_id=t["id"], secret=password)
