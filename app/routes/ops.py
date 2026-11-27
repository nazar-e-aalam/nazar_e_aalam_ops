"""The ops panel API (served on ops.<root domain>)."""
import hashlib
import hmac
import secrets
import time
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import os
import re
import shutil
import threading

from ..core import config, db, envstore, mailer, security, totp
from ..services import deploy, library, serverconf, setup, targets as tgs, workflows
from ..services import github as gh

router = APIRouter()
_DUMMY_HASH = security.hash_password("not-a-real-password")  # equalises timing for unknown emails


# ---- auth helpers ----------------------------------------------------------------
def ip_of(request: Request) -> str:
    return request.client.host if request.client else ""


def current_user(request: Request) -> dict:
    user = security.user_for_token(request.cookies.get(security.COOKIE))
    if not user:
        raise HTTPException(401, "Not signed in")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # CSRF: browsers cannot add this header cross-site without a CORS preflight, which we never allow
        if request.headers.get("x-requested-with") != "ops":
            raise HTTPException(403, "Missing CSRF header")
        origin = request.headers.get("origin")
        if origin and urllib.parse.urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-origin request blocked")
    if must_setup_2fa(user):
        path = request.url.path
        path = path[path.index("/api/"):] if "/api/" in path else path
        if not (path == "/api/me" or path.startswith(("/api/auth/", "/api/2fa/"))):
            raise HTTPException(403, "Two-factor authentication must be set up first")
    return user


def security_cfg() -> dict:
    return {"require_2fa": bool((db.sget("security_cfg") or {}).get("require_2fa"))}


def must_setup_2fa(user: dict) -> bool:
    return security_cfg()["require_2fa"] and not user.get("totp_enabled")


def csrf(request: Request) -> None:
    """For public (signed-out) POST endpoints: same protection as signed-in ones."""
    if request.headers.get("x-requested-with") != "ops":
        raise HTTPException(403, "Missing CSRF header")
    origin = request.headers.get("origin")
    if origin and urllib.parse.urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin request blocked")


def start_session(user_id: int, request: Request, response: Response) -> None:
    token = security.create_session(user_id, ip_of(request), request.headers.get("user-agent", ""))
    db.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))
    response.set_cookie(security.COOKIE, token, max_age=config.SESSION_HOURS * 3600, httponly=True,
                        samesite="lax", secure=config.HTTPS, path="/")


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return user


def operator_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("admin", "operator"):
        raise HTTPException(403, "You have read-only access")
    return user


def gh_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except gh.GitHubError as e:
        raise HTTPException(e.status if 400 <= e.status < 600 else 502, e.message)
    except ValueError as e:
        raise HTTPException(400, str(e))


def need_app(slug: str) -> dict:
    a = library.get_app(slug)
    if not a:
        raise HTTPException(404, "App not found")
    return a


def repo_key(key: str) -> str:
    if key not in ("app", "panel"):
        raise HTTPException(404, "Unknown repository")
    return key


def user_view(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
            "first_name": u.get("first_name", ""), "last_name": u.get("last_name", ""),
            "totp_enabled": bool(u.get("totp_enabled")), "must_setup_2fa": must_setup_2fa(u)}


# ---- session ---------------------------------------------------------------------
class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    csrf(request)
    email = body.email.strip().lower()
    key = f"{ip_of(request)}|{email}"
    wait = security.throttled(key)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {wait // 60 + 1} min.")
    u = db.one("SELECT * FROM users WHERE email=?", (email,))
    ok = security.verify_password(body.password, u["password_hash"] if u else _DUMMY_HASH)
    if not u or not ok or u["disabled"]:
        security.note_failure(key)
        db.audit(email, "login.failed", "", ip_of(request))
        raise HTTPException(401, "Wrong email or password")
    security.clear_failures(key)
    if u["totp_enabled"]:
        challenge = security.new_token()
        db.execute("DELETE FROM login_challenges WHERE expires_at<?", (time.time(),))
        db.execute("INSERT INTO login_challenges(token_hash,user_id,expires_at) VALUES(?,?,?)",
                   (security.token_hash(challenge), u["id"], time.time() + 300))
        return {"need_2fa": True, "challenge": challenge}
    start_session(u["id"], request, response)
    db.audit(email, "login", "", ip_of(request))
    return {"user": user_view(u)}


class TwoFABody(BaseModel):
    challenge: str
    code: str


@router.post("/api/auth/2fa")
def login_2fa(body: TwoFABody, request: Request, response: Response):
    csrf(request)
    th = security.token_hash(body.challenge)
    key = f"{ip_of(request)}|2fa"
    wait = security.throttled(key)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {wait // 60 + 1} min.")
    ch = db.one("SELECT * FROM login_challenges WHERE token_hash=?", (th,))
    if not ch or ch["expires_at"] < time.time():
        raise HTTPException(401, "This sign-in expired. Enter your password again.")
    u = db.one("SELECT * FROM users WHERE id=?", (ch["user_id"],))
    if not u or u["disabled"] or not u["totp_enabled"]:
        raise HTTPException(401, "This sign-in expired. Enter your password again.")
    if not accept_second_factor(u, body.code):
        security.note_failure(key)
        db.execute("UPDATE login_challenges SET attempts=attempts+1 WHERE token_hash=?", (th,))
        if ch["attempts"] + 1 >= 5:
            db.execute("DELETE FROM login_challenges WHERE token_hash=?", (th,))
            db.audit(u["email"], "login.2fa_locked", "", ip_of(request))
            raise HTTPException(401, "Too many wrong codes. Enter your password again.")
        db.audit(u["email"], "login.2fa_failed", "", ip_of(request))
        raise HTTPException(401, "That code is not right")
    security.clear_failures(key)
    db.execute("DELETE FROM login_challenges WHERE token_hash=?", (th,))
    start_session(u["id"], request, response)
    db.audit(u["email"], "login", "2fa", ip_of(request))
    return {"user": user_view(db.one("SELECT * FROM users WHERE id=?", (u["id"],)))}


def accept_second_factor(u: dict, code: str, allow_recovery: bool = True) -> bool:
    """Checks an authenticator code (replay-protected) or, if allowed, a single-use recovery code."""
    code = (code or "").strip()
    step = totp.verify(u["totp_secret"] or "", code, u["totp_last_step"])
    if step is not None:
        return db.rowcount("UPDATE users SET totp_last_step=? WHERE id=? AND totp_last_step<?", (step, u["id"], step)) == 1
    if allow_recovery and totp.norm_recovery(code):
        return db.rowcount("UPDATE recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                           (time.time(), u["id"], totp.hash_recovery(code))) == 1
    return False


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, user: dict = Depends(current_user)):
    security.destroy_session(request.cookies.get(security.COOKIE))
    response.delete_cookie(security.COOKIE, path="/")
    db.audit(user["email"], "logout", "", ip_of(request))
    return {"ok": True}


@router.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"user": user_view(user), "version": config.VERSION, "org": gh.org(), "repos": gh.repos(),
            "public_url": config.PUBLIC_URL, "ops_url": config.OPS_URL, "smtp_enabled": mailer.configured(),
            "setup_done": setup.is_done(), "domain": config.ROOT_DOMAIN}


class ProfileBody(BaseModel):
    first_name: str
    last_name: str


@router.patch("/api/me")
def update_profile(body: ProfileBody, request: Request, user: dict = Depends(current_user)):
    first, last = body.first_name.strip()[:40], body.last_name.strip()[:40]
    if not first:
        raise HTTPException(400, "First name is required")
    db.execute("UPDATE users SET first_name=?, last_name=?, name=? WHERE id=?", (first, last, f"{first} {last}".strip(), user["id"]))
    db.audit(user["email"], "profile.updated", "", ip_of(request))
    return {"user": user_view(db.one("SELECT * FROM users WHERE id=?", (user["id"],)))}


class PasswordBody(BaseModel):
    current: str
    new: str


@router.post("/api/me/password")
def change_password(body: PasswordBody, request: Request, user: dict = Depends(current_user)):
    row = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
    if not security.verify_password(body.current, row["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    err = security.check_password_strength(body.new)
    if err:
        raise HTTPException(400, err)
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(body.new), user["id"]))
    me_tok = request.cookies.get(security.COOKIE)
    keep = hashlib.sha256(me_tok.encode()).hexdigest() if me_tok else ""
    db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user["id"], keep))
    db.audit(user["email"], "password.changed", "", ip_of(request))
    return {"ok": True}


@router.get("/api/me/sessions")
def my_sessions(request: Request, user: dict = Depends(current_user)):
    tok = request.cookies.get(security.COOKIE) or ""
    mine = hashlib.sha256(tok.encode()).hexdigest()
    rows = db.query("SELECT token_hash,created_at,last_seen,ip,ua FROM sessions WHERE user_id=? ORDER BY last_seen DESC", (user["id"],))
    return [{"id": r["token_hash"][:12], "created_at": r["created_at"], "last_seen": r["last_seen"], "ip": r["ip"],
             "ua": r["ua"], "current": r["token_hash"] == mine} for r in rows]


@router.delete("/api/me/sessions/{sid}")
def revoke_session(sid: str, user: dict = Depends(current_user)):
    db.execute("DELETE FROM sessions WHERE user_id=? AND substr(token_hash,1,12)=?", (user["id"], sid))
    return {"ok": True}


# ---- overview --------------------------------------------------------------------
@router.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    out = {"online": db.query("""SELECT u.email,u.name,u.role,MAX(s.last_seen) AS last_seen FROM sessions s JOIN users u ON u.id=s.user_id
                                  WHERE s.last_seen>? AND s.expires_at>? GROUP BY u.id ORDER BY last_seen DESC""",
                              (time.time() - 300, time.time())),
           "token": {"set": bool(gh.token()), "rate": gh.rate},
           "server": deploy.server_status(),
           "last_deploys": [deploy.shape(d) for d in db.query("SELECT * FROM deployments ORDER BY id DESC LIMIT 5")],
           "activity": db.query("SELECT ts,user_email,action,detail FROM audit ORDER BY id DESC LIMIT 12"),
           "library": library.public_apps(), "runs": [], "runs_error": None, "running": 0}
    try:
        data = gh.jget(gh.repo_path("app") + "/actions/runs", params={"per_page": 8}, ttl=8)
        tracked = {r["run_id"]: r for r in db.query("SELECT * FROM tracked_runs WHERE repo='app' ORDER BY run_id DESC LIMIT 200")}
        out["runs"] = [workflows.shape_run(r, tracked.get(r["id"])) for r in data.get("workflow_runs", [])]
        out["running"] = sum(1 for r in out["runs"] if r["status"] in ("running", "queued"))
    except gh.GitHubError as e:
        out["runs_error"] = e.message
    return out


# ---- GitHub: token + repos -----------------------------------------------------------
@router.get("/api/github/verify")
def github_verify(user: dict = Depends(current_user)):
    return gh.verify()


class TokenBody(BaseModel):
    token: str = ""
    org: str | None = None
    repo_app: str | None = None
    repo_panel: str | None = None


@router.put("/api/github/settings")
def github_settings(body: TokenBody, request: Request, user: dict = Depends(admin_only)):
    if body.token.strip():
        db.sset("github_token", body.token.strip())
    for key, val in (("github_org", body.org), ("repo_app", body.repo_app), ("repo_panel", body.repo_panel)):
        if val is not None and val.strip():
            if not all(c.isalnum() or c in "-_." for c in val.strip()):
                raise HTTPException(400, f"Invalid name: {val}")
            db.sset(key, val.strip())
    gh.clear_cache()
    db.audit(user["email"], "github.settings", "token updated" if body.token.strip() else "names updated", ip_of(request))
    return gh.verify()


@router.delete("/api/github/token")
def github_clear_token(request: Request, user: dict = Depends(admin_only)):
    db.sdel("github_token")
    gh.clear_cache()
    db.audit(user["email"], "github.token.removed", "", ip_of(request))
    return {"ok": True, "env_fallback": bool(config.GITHUB_TOKEN)}


@router.get("/api/github/branches/{key}")
def branches(key: str, user: dict = Depends(current_user)):
    data = gh_call(gh.jget, gh.repo_path(repo_key(key)) + "/branches", params={"per_page": 50}, ttl=30)
    default = gh_call(gh.jget, gh.repo_path(key), ttl=60).get("default_branch")
    return {"default": default, "branches": [b["name"] for b in data]}


# ---- workflows -------------------------------------------------------------------------
@router.get("/api/workflows/{key}")
def list_workflows(key: str, user: dict = Depends(current_user)):
    rp = gh.repo_path(repo_key(key))
    data = gh_call(gh.jget, rp + "/actions/workflows", params={"per_page": 50}, ttl=15)
    default = gh_call(gh.jget, rp, ttl=60).get("default_branch")
    out = []
    for w in data.get("workflows", []):
        last = None
        try:
            runs = gh.jget(f"{rp}/actions/workflows/{w['id']}/runs", params={"per_page": 1}, ttl=10).get("workflow_runs", [])
            last = workflows.shape_run(runs[0]) if runs else None
        except gh.GitHubError:
            pass
        defn = workflows.get_definition(key, w["path"], default) if w.get("state") == "active" else None
        out.append({"id": w["id"], "name": w["name"], "path": w["path"], "state": w["state"], "url": w.get("html_url"),
                    "dispatch": bool(defn and defn["dispatch"]), "inputs": defn["inputs"] if defn else [],
                    "last_run": last})
    return {"workflows": out, "default_branch": default}


class DispatchBody(BaseModel):
    ref: str
    inputs: dict = {}


@router.post("/api/workflows/{key}/{wid}/dispatch")
def dispatch(key: str, wid: int, body: DispatchBody, request: Request, user: dict = Depends(operator_only)):
    rp = gh.repo_path(repo_key(key))
    wf = gh_call(gh.jget, f"{rp}/actions/workflows/{wid}", ttl=10)
    ref = body.ref.strip()
    if not ref:
        raise HTTPException(400, "Choose a branch or tag")
    defn = workflows.get_definition(key, wf["path"], ref)
    if defn.get("error"):
        raise HTTPException(400, f"Could not read the workflow at '{ref}': {defn['error']}")
    if not defn["dispatch"]:
        raise HTTPException(400, "This workflow has no workflow_dispatch trigger")
    try:
        inputs = workflows.validate_inputs(defn, body.inputs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t0, run_id = gh_call(gh.dispatch, key, wid, ref, inputs)
    # the dispatch API returns no run id, so find the run that appeared right after
    deadline = time.time() + 20
    while not run_id and time.time() < deadline:
        time.sleep(1.5)
        try:
            runs = gh.jget(f"{rp}/actions/workflows/{wid}/runs", params={"event": "workflow_dispatch", "per_page": 10}, use_cache=False)
        except gh.GitHubError:
            continue
        taken = {r["run_id"] for r in db.query("SELECT run_id FROM tracked_runs WHERE repo=?", (key,))}
        cands = [r for r in runs.get("workflow_runs", [])
                 if r["id"] not in taken and (gh.iso_to_epoch(r["created_at"]) or 0) >= t0 - 3]
        if cands:
            run_id = min(cands, key=lambda r: r["id"])["id"]
    if run_id:
        db.execute("INSERT OR IGNORE INTO tracked_runs(repo,run_id,workflow,ref,inputs,triggered_by,triggered_at) VALUES(?,?,?,?,?,?,?)",
                   (key, run_id, wf["name"], ref, repr(inputs), user["email"], time.time()))
    db.audit(user["email"], "workflow.run", f"{wf['name']} @ {ref} {inputs} -> run {run_id or '?'}", ip_of(request))
    return {"run_id": run_id, "message": None if run_id else "Dispatched. The run has not shown up yet, check the Runs page."}


# ---- runs -----------------------------------------------------------------------------
@router.get("/api/runs/{key}")
def list_runs(key: str, workflow: int = 0, status: str = "", page: int = 1, user: dict = Depends(current_user)):
    rp = gh.repo_path(repo_key(key))
    params = {"per_page": 25, "page": max(1, page)}
    if status in ("queued", "in_progress", "completed", "success", "failure", "cancelled"):
        params["status"] = status
    path = f"{rp}/actions/workflows/{workflow}/runs" if workflow else f"{rp}/actions/runs"
    data = gh_call(gh.jget, path, params=params, ttl=4)
    tracked = {r["run_id"]: r for r in db.query("SELECT * FROM tracked_runs WHERE repo=? ORDER BY run_id DESC LIMIT 500", (key,))}
    return {"total": data.get("total_count", 0), "page": page,
            "runs": [workflows.shape_run(r, tracked.get(r["id"])) for r in data.get("workflow_runs", [])]}


@router.get("/api/runs/{key}/{run_id}")
def run_detail(key: str, run_id: int, user: dict = Depends(current_user)):
    rp = gh.repo_path(repo_key(key))
    run = gh_call(gh.jget, f"{rp}/actions/runs/{run_id}", ttl=2.5)
    jobs = gh_call(gh.jget, f"{rp}/actions/runs/{run_id}/jobs", params={"per_page": 100, "filter": "latest"}, ttl=2.5)["jobs"]
    wf = workflows.get_definition(key, run.get("path", "").split("@")[0], run["head_sha"])
    tracked = db.one("SELECT * FROM tracked_runs WHERE repo=? AND run_id=?", (key, run_id))
    arts = []
    try:
        arts = gh.jget(f"{rp}/actions/runs/{run_id}/artifacts", params={"per_page": 50}, ttl=5).get("artifacts", [])
    except gh.GitHubError:
        pass
    shaped = workflows.shape_run(run, tracked)
    inputs = None
    if tracked and tracked["inputs"]:
        inputs = tracked["inputs"]
    return {"run": shaped, "tree": workflows.build_tree(run, jobs, wf), "definition_error": wf.get("error"),
            "tracked": {"by": tracked["triggered_by"], "inputs": inputs, "ref": tracked["ref"]} if tracked else None,
            "artifacts": [{"id": a["id"], "name": a["name"], "size": a["size_in_bytes"], "expired": a["expired"],
                           "created_at": a["created_at"]} for a in arts],
            "jobs": [{"id": j["id"], "name": j["name"], "status": workflows.status_of(j.get("status"), j.get("conclusion")),
                      "url": j.get("html_url")} for j in jobs]}


@router.post("/api/runs/{key}/{run_id}/{action}")
def run_action(key: str, run_id: int, action: str, request: Request, user: dict = Depends(operator_only)):
    rp = gh.repo_path(repo_key(key))
    paths = {"cancel": "cancel", "rerun": "rerun", "rerun-failed": "rerun-failed-jobs", "force-cancel": "force-cancel"}
    if action not in paths:
        raise HTTPException(404, "Unknown action")
    gh_call(gh.request, "POST", f"{rp}/actions/runs/{run_id}/{paths[action]}", body={} if action != "cancel" else None)
    db.audit(user["email"], f"run.{action}", f"{key} #{run_id}", ip_of(request))
    return {"ok": True}


@router.get("/api/runs/{key}/jobs/{job_id}/log")
def job_log(key: str, job_id: int, user: dict = Depends(current_user)):
    import re
    import tempfile
    rp = gh.repo_path(repo_key(key))
    with tempfile.NamedTemporaryFile(dir=config.TMP_DIR, delete=True) as tmp:
        try:
            gh.download(f"{rp}/actions/jobs/{job_id}/logs", tmp.name, max_bytes=60 * 1024 * 1024)
        except gh.GitHubError as e:
            if e.status in (404, 410):
                return PlainTextResponse("Logs are not available yet. They appear once the job finishes "
                                         "(or have expired on GitHub).", status_code=200, headers={"X-Log-State": "pending"})
            raise HTTPException(e.status if 400 <= e.status < 600 else 502, e.message)
        data = Path(tmp.name).read_bytes()
    truncated = len(data) > 2_000_000
    text = data[-2_000_000:].decode("utf-8", "replace")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return PlainTextResponse(("…(earlier output truncated)…\n" if truncated else "") + text, headers={"X-Log-State": "ok"})


@router.get("/api/runs/{key}/{run_id}/artifact/{artifact_id}/download")
def artifact_download(key: str, run_id: int, artifact_id: int, request: Request, user: dict = Depends(operator_only)):
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    rp = gh.repo_path(repo_key(key))
    art = gh_call(gh.jget, f"{rp}/actions/artifacts/{artifact_id}", ttl=10)
    if art.get("expired"):
        raise HTTPException(410, "This artifact has expired")
    dest = config.TMP_DIR / f"dl-{artifact_id}-{secrets.token_hex(4)}.zip"
    gh_call(gh.download, f"{rp}/actions/artifacts/{artifact_id}/zip", str(dest), max_bytes=config.MAX_UPLOAD_MB * 1024 * 1024)
    db.audit(user["email"], "artifact.download", f"{key} {art['name']}", ip_of(request))
    return FileResponse(dest, filename=f"{art['name']}.zip", media_type="application/zip",
                        background=BackgroundTask(lambda: dest.unlink(missing_ok=True)))


# ---- deployments ---------------------------------------------------------------------
@router.get("/api/deploy/status")
def deploy_status(target: int = 0, user: dict = Depends(current_user)):
    return {"status": deploy.server_status(target), "cfg": deploy.get_cfg(), "repo": gh.repos()["panel"],
            "targets": [{"id": t["id"], "name": t["name"], "kind": t["kind"], "verified": bool(t["verified"]), "host": t.get("host") or ""} for t in tgs.rows()],
            "preflight": deploy.preflight(target),
            "webhook": {"url": f"{config.OPS_URL}/hooks/github", "has_secret": bool(db.sget("webhook_secret"))}}


@router.get("/api/deploy/list")
def deploy_list(limit: int = 30, offset: int = 0, target: int = -1, user: dict = Depends(current_user)):
    if target >= 0:
        rows = db.query("SELECT * FROM deployments WHERE target_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (target, min(limit, 100), offset))
    else:
        rows = db.query("SELECT * FROM deployments ORDER BY id DESC LIMIT ? OFFSET ?", (min(limit, 100), offset))
    return {"deployments": [deploy.shape(r) for r in rows], "releases": deploy.releases_on_disk(max(target, 0))}


@router.get("/api/deploy/preflight")
def deploy_preflight(target: int = 0, user: dict = Depends(current_user)):
    return deploy.preflight(target)


@router.get("/api/deploy/head")
def deploy_head(ref: str = "", user: dict = Depends(current_user)):
    cfg = deploy.get_cfg()
    c = gh_call(deploy.resolve_commit, ref or cfg["branch"])
    c["deployed"] = bool(db.one("SELECT id FROM deployments WHERE sha=? AND kind='deploy' AND status='success'", (c["sha"],)))
    return c


class DeployBody(BaseModel):
    ref: str = ""
    target_id: int = 0


@router.post("/api/deploy")
def deploy_now(body: DeployBody, request: Request, user: dict = Depends(operator_only)):
    cfg = deploy.get_cfg()
    commit = gh_call(deploy.resolve_commit, body.ref.strip() or cfg["branch"])
    try:
        dep_id = deploy.enqueue(commit, user["email"], "manual", target_id=body.target_id)
    except deploy.Blocked as b:
        raise HTTPException(409, str(b))
    db.audit(user["email"], "deploy.queued", f"{commit['sha'][:7]} {commit['message']} -> server {body.target_id}", ip_of(request))
    return {"id": dep_id}


@router.get("/api/deploy/{dep_id}")
def deploy_get(dep_id: int, user: dict = Depends(current_user)):
    d = db.one("SELECT * FROM deployments WHERE id=?", (dep_id,))
    if not d:
        raise HTTPException(404, "Deployment not found")
    return deploy.shape(d)


@router.get("/api/deploy/{dep_id}/log")
def deploy_log(dep_id: int, offset: int = 0, user: dict = Depends(current_user)):
    text, nxt = deploy.read_log(dep_id, max(0, offset))
    d = db.one("SELECT status FROM deployments WHERE id=?", (dep_id,))
    return {"text": text, "next": nxt, "status": d["status"] if d else "unknown"}


@router.post("/api/deploy/{dep_id}/cancel")
def deploy_cancel(dep_id: int, request: Request, user: dict = Depends(operator_only)):
    try:
        msg = deploy.cancel(dep_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "deploy.cancel", f"#{dep_id}", ip_of(request))
    return {"message": msg}


class RollbackBody(BaseModel):
    release: str
    target_id: int = 0


@router.post("/api/deploy/rollback")
def deploy_rollback(body: RollbackBody, request: Request, user: dict = Depends(operator_only)):
    t = tgs.get(body.target_id)
    if not t:
        raise HTTPException(404, "Server not found")
    cfg = deploy.eff_cfg(deploy.get_cfg(), t)
    tg, root = deploy.executor(t), cfg["deploy_root"]
    name = Path(body.release).name
    if not tg.is_dir(f"{root}/releases/{name}"):
        raise HTTPException(404, "That release is not on the server anymore")
    if name == tg.active(root):
        raise HTTPException(400, "That release is already active")
    src = db.one("SELECT sha,short_sha,message,author,branch FROM deployments WHERE release_name=? AND target_id=? ORDER BY id DESC LIMIT 1", (name, t["id"])) or {}
    commit = {"sha": src.get("sha"), "message": f"Roll back to {name}", "author": src.get("author", ""), "branch": src.get("branch", "")}
    dep_id = deploy.enqueue(commit, user["email"], "rollback", kind="rollback", release=name, target_id=t["id"])
    db.audit(user["email"], "deploy.rollback", f"{name} on {t['name']}", ip_of(request))
    return {"id": dep_id}


class DeployCfgBody(BaseModel):
    cfg: dict


@router.put("/api/deploy/config")
def deploy_config(body: DeployCfgBody, request: Request, user: dict = Depends(admin_only)):
    try:
        cfg = deploy.set_cfg(body.cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "deploy.config", "updated", ip_of(request))
    return cfg


@router.post("/api/deploy/webhook-secret")
def webhook_secret(request: Request, user: dict = Depends(admin_only)):
    secret = secrets.token_hex(24)
    db.sset("webhook_secret", secret)
    db.audit(user["email"], "webhook.secret.rotated", "", ip_of(request))
    return {"secret": secret, "url": f"{config.OPS_URL}/hooks/github"}


@router.post("/hooks/github")
async def github_hook(request: Request):
    """Public endpoint for GitHub webhooks. Authenticated by HMAC signature only."""
    secret = db.sget("webhook_secret")
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    if not secret:
        raise HTTPException(503, "Webhook secret not configured")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, "Bad signature")
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return {"pong": True}
    if event != "push":
        return Response(status_code=202)
    import json
    code, msg = deploy.handle_push(json.loads(raw.decode("utf-8")))
    return Response(content=json.dumps({"message": msg}), status_code=code, media_type="application/json")


# ---- .env manager -----------------------------------------------------------------------
@router.get("/api/env")
def env_current(user: dict = Depends(current_user)):
    cur = envstore.current()
    return {"current": envstore.view(cur) if cur else None, "versions": envstore.versions(),
            "target": deploy.get_cfg()["env_target"]}


@router.get("/api/env/diff")
def env_diff(a: int, b: int, user: dict = Depends(current_user)):
    ra, rb = envstore.get(a), envstore.get(b)
    if not ra or not rb:
        raise HTTPException(404, "Version not found")
    return envstore.diff(ra, rb)


@router.get("/api/env/versions/{version}")
def env_version(version: int, user: dict = Depends(current_user)):
    row = envstore.get(version)
    if not row:
        raise HTTPException(404, "Version not found")
    return envstore.view(row)


@router.post("/api/env/reveal")
def env_reveal(request: Request, user: dict = Depends(admin_only)):
    cur = envstore.current()
    if not cur:
        raise HTTPException(404, "No .env yet")
    db.audit(user["email"], "env.reveal", f"v{cur['version']}", ip_of(request))
    return {"content": cur["content"], "version": cur["version"]}


class EnvBody(BaseModel):
    content: str
    note: str = ""


@router.put("/api/env")
def env_save(body: EnvBody, request: Request, user: dict = Depends(admin_only)):
    try:
        res = envstore.save(body.content, user["email"], body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "env.saved", f"v{res['version']} ({res['keys']} keys)", ip_of(request))
    return res


@router.post("/api/env/upload")
async def env_upload(request: Request, file: UploadFile = File(...), note: str = Form(""), user: dict = Depends(admin_only)):
    raw = await file.read(envstore.MAX_BYTES + 1)
    try:
        res = envstore.save(raw.decode("utf-8"), user["email"], note or f"uploaded {file.filename}")
    except UnicodeDecodeError:
        raise HTTPException(400, "The file is not UTF-8 text")
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "env.uploaded", f"v{res['version']} ({res['keys']} keys)", ip_of(request))
    return res


@router.post("/api/env/restore/{version}")
def env_restore(version: int, request: Request, user: dict = Depends(admin_only)):
    row = envstore.get(version)
    if not row:
        raise HTTPException(404, "Version not found")
    try:
        res = envstore.save(row["content"], user["email"], f"restored from v{version}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "env.restored", f"v{version} -> v{res['version']}", ip_of(request))
    return res


@router.get("/api/env/download")
def env_download(request: Request, user: dict = Depends(admin_only)):
    cur = envstore.current()
    if not cur:
        raise HTTPException(404, "No .env yet")
    db.audit(user["email"], "env.download", f"v{cur['version']}", ip_of(request))
    return PlainTextResponse(cur["content"], headers={"Content-Disposition": 'attachment; filename=".env"', "Cache-Control": "no-store"})


# ---- release library -----------------------------------------------------------------------
@router.get("/api/library")
def library_list(user: dict = Depends(current_user)):
    return {"apps": db.query("SELECT * FROM apps ORDER BY name"), "releases": library.list_releases(public=False),
            "public_url": config.PUBLIC_URL}


class AppBody(BaseModel):
    name: str
    slug: str = ""
    description: str = ""


@router.post("/api/library/apps")
def library_app(body: AppBody, request: Request, user: dict = Depends(admin_only)):
    try:
        a = library.create_app(body.name, body.slug, body.description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "app.created", a["slug"], ip_of(request))
    return a


@router.get("/api/library/github-releases")
def library_gh_releases(user: dict = Depends(current_user)):
    return gh_call(library.github_releases)


class SyncBody(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = None
    app: str | None = None
    prereleases: bool | None = None


def _sync_state() -> dict:
    return {"cfg": library.sync_cfg(), "status": db.sget("sync_status"), "busy": library.sync_busy(),
            "forgotten": len(library._skip_set()), "importing": db.one("SELECT COUNT(*) AS n FROM releases WHERE status='importing'")["n"]}


@router.get("/api/library/sync")
def sync_get(user: dict = Depends(current_user)):
    return _sync_state()


@router.put("/api/library/sync")
def sync_put(body: SyncBody, request: Request, user: dict = Depends(admin_only)):
    try:
        library.set_sync_cfg(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "library.sync.config", str(body.model_dump(exclude_none=True)), ip_of(request))
    return _sync_state()


@router.post("/api/library/sync/run")
def sync_run(request: Request, user: dict = Depends(operator_only)):
    if library.sync_busy():
        raise HTTPException(409, "A sync is already running")
    threading.Thread(target=library.sync_github, args=(user["email"],), daemon=True, name="manual-sync").start()
    db.audit(user["email"], "library.sync.run", "", ip_of(request))
    return {"started": True}


@router.post("/api/library/sync/forget-deleted")
def sync_forget(request: Request, user: dict = Depends(admin_only)):
    n = library.forget_skipped()
    db.audit(user["email"], "library.sync.forget", f"{n} deleted release(s) will sync again", ip_of(request))
    return {"cleared": n}


# ---- download proxy (Releases tab) ----------------------------------------------------
class ProxyBody(BaseModel):
    url: str = ""


def _proxy_url_ok(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if u and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?", u):
        raise HTTPException(400, "Enter a proxy like https://gh-proxy.org (or leave empty to use GitHub directly)")
    return u


def _proxy_sample():
    a = db.one("SELECT direct_url FROM assets WHERE direct_url IS NOT NULL ORDER BY id DESC LIMIT 1")
    return a["direct_url"] if a else None


@router.get("/api/library/proxy")
def proxy_get(user: dict = Depends(current_user)):
    return {"url": gh.proxy_base(), "default": config.GITHUB_PROXY, "custom": isinstance(db.sget("proxy_url"), str)}


@router.put("/api/library/proxy")
def proxy_put(body: ProxyBody, request: Request, user: dict = Depends(admin_only)):
    u = _proxy_url_ok(body.url)
    db.sset("proxy_url", u)
    gh._chk.clear()
    db.audit(user["email"], "library.proxy", u or "(direct only)", ip_of(request))
    return {"url": u, "report": gh.proxy_report(_proxy_sample())}


@router.post("/api/library/proxy/test")
def proxy_test(body: ProxyBody, user: dict = Depends(operator_only)):
    base = _proxy_url_ok(body.url) if body.url is not None else None
    return gh.proxy_report(_proxy_sample(), base)


@router.delete("/api/library/proxy")
def proxy_reset(request: Request, user: dict = Depends(admin_only)):
    db.sdel("proxy_url")
    gh._chk.clear()
    db.audit(user["email"], "library.proxy.reset", "", ip_of(request))
    return {"url": gh.proxy_base()}


class ImportReleaseBody(BaseModel):
    tag: str
    app: str
    notes: str | None = None


@router.post("/api/library/import-release")
def library_import_release(body: ImportReleaseBody, request: Request, user: dict = Depends(operator_only)):
    app = need_app(body.app)
    ids = gh_call(library.import_github_release, body.tag, app, user["email"], body.notes)
    db.audit(user["email"], "library.import", f"github release {body.tag} -> {app['slug']}", ip_of(request))
    return {"ids": ids}


class ImportArtifactBody(BaseModel):
    repo: str = "app"
    artifact_id: int
    app: str
    version: str
    platform: str = "android"
    channel: str = "stable"
    notes: str = ""


@router.post("/api/library/import-artifact")
def library_import_artifact(body: ImportArtifactBody, request: Request, user: dict = Depends(operator_only)):
    app = need_app(body.app)
    rid = gh_call(library.import_artifact, repo_key(body.repo), body.artifact_id, app, body.version, body.platform,
                  body.channel, body.notes, user["email"])
    db.audit(user["email"], "library.import", f"artifact {body.artifact_id} -> {app['slug']} {body.version}", ip_of(request))
    return {"id": rid}


@router.post("/api/library/upload")
def library_upload(request: Request, app: str = Form(...), version: str = Form(...), platform: str = Form("android"),
                   channel: str = Form("stable"), notes: str = Form(""), files: list[UploadFile] = File(...),
                   user: dict = Depends(operator_only)):
    a = library.get_app(app)
    if not a:
        raise HTTPException(404, "App not found")
    try:
        rid = library.add_upload(a, version, platform, channel, notes, [(f.filename or "file", f.file) for f in files], user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "library.upload", f"{a['slug']} {version} {platform}", ip_of(request))
    return {"id": rid}


class ReleasePatch(BaseModel):
    visible: bool | None = None
    notes: str | None = None
    channel: str | None = None


@router.patch("/api/library/releases/{rid}")
def library_patch(rid: int, body: ReleasePatch, request: Request, user: dict = Depends(operator_only)):
    r = db.one("SELECT id FROM releases WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "Release not found")
    if body.visible is not None:
        db.execute("UPDATE releases SET visible=? WHERE id=?", (1 if body.visible else 0, rid))
    if body.notes is not None:
        db.execute("UPDATE releases SET notes=? WHERE id=?", (body.notes[:20000], rid))
    if body.channel in ("stable", "beta"):
        db.execute("UPDATE releases SET channel=? WHERE id=?", (body.channel, rid))
    db.audit(user["email"], "library.edit", f"release {rid} {body.model_dump(exclude_none=True)}", ip_of(request))
    return {"ok": True}


@router.delete("/api/library/releases/{rid}")
def library_delete(rid: int, request: Request, user: dict = Depends(admin_only)):
    library.delete_release(rid, ignore=True)
    db.audit(user["email"], "library.delete", f"release {rid}", ip_of(request))
    return {"ok": True}


# ---- servers (deploy targets) ---------------------------------------------------------------------
class TargetBody(BaseModel):
    name: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    root: str = ""
    restart_cmd: str = ""
    health_url: str = ""


class KeyBody(BaseModel):
    mode: str = "generate"
    private_key: str = ""


class SudoBody(BaseModel):
    password: str = ""


class DbBody(BaseModel):
    mode: str = "local"
    engine: str = "mysql"
    host: str = ""
    port: int = 3306
    name: str = ""
    user: str = ""
    password: str = ""
    ssl: bool = False
    keys: dict = {}


def _target(tid: int) -> dict:
    t = tgs.get(tid)
    if not t:
        raise HTTPException(404, "Server not found")
    return t


def _terr(fn, *a):
    try:
        return fn(*a)
    except tgs.TargetError as e:
        raise HTTPException(400, str(e))


@router.get("/api/targets")
def targets_list(user: dict = Depends(current_user)):
    cfg = deploy.get_cfg()
    return {"targets": [tgs.shape(t) for t in tgs.rows()], "defaults": {"root": cfg["deploy_root"], "restart_cmd": cfg["restart_cmd"], "health_url": cfg["health_url"]},
            "ssh_ready": bool(shutil.which("ssh") and shutil.which("ssh-keygen")), "ops_user": os.environ.get("USER") or "", "auto_target": cfg["auto_target"]}


@router.get("/api/targets/{tid}")
def targets_get(tid: int, user: dict = Depends(current_user)):
    return tgs.shape(_target(tid))


@router.post("/api/targets")
def targets_create(body: TargetBody, request: Request, user: dict = Depends(admin_only)):
    tid = _terr(tgs.create, body.model_dump())
    db.audit(user["email"], "server.add", body.name, ip_of(request))
    return {"id": tid}


@router.put("/api/targets/{tid}")
def targets_update(tid: int, body: TargetBody, request: Request, user: dict = Depends(admin_only)):
    _target(tid)
    _terr(tgs.update, tid, body.model_dump())
    db.audit(user["email"], "server.edit", body.name, ip_of(request))
    return tgs.shape(_target(tid))


@router.delete("/api/targets/{tid}")
def targets_delete(tid: int, request: Request, user: dict = Depends(admin_only)):
    t = _target(tid)
    if deploy.get_cfg()["auto_target"] == tid:
        deploy.set_cfg({"auto_target": 0})
    _terr(tgs.delete, tid)
    db.audit(user["email"], "server.remove", t["name"], ip_of(request))
    return {"ok": True}


@router.post("/api/targets/{tid}/key")
def targets_key(tid: int, body: KeyBody, request: Request, user: dict = Depends(admin_only)):
    t = _target(tid)
    if t["kind"] != "ssh":
        raise HTTPException(400, "The local server needs no key")
    if body.mode == "paste":
        _terr(tgs.set_private_key, tid, body.private_key)
    else:
        _terr(tgs.generate_key, tid)
    db.audit(user["email"], "server.key", f"{t['name']} ({body.mode})", ip_of(request))
    return tgs.shape(_target(tid))


@router.post("/api/targets/{tid}/check")
def targets_check(tid: int, user: dict = Depends(operator_only)):
    t = _target(tid)
    res = tgs.check(t, deploy.eff_cfg(deploy.get_cfg(), t))
    return {"result": res, "target": tgs.shape(_target(tid))}


@router.post("/api/targets/{tid}/grant-sudo")
def targets_grant(tid: int, body: SudoBody, request: Request, user: dict = Depends(admin_only)):
    t = _target(tid)
    _terr(tgs.grant_sudo, t, deploy.eff_cfg(deploy.get_cfg(), t), body.password)
    db.audit(user["email"], "server.sudo", t["name"], ip_of(request))
    res = tgs.check(t, deploy.eff_cfg(deploy.get_cfg(), t))
    return {"result": res, "target": tgs.shape(_target(tid))}


@router.post("/api/targets/{tid}/forget-host")
def targets_forget(tid: int, user: dict = Depends(admin_only)):
    t = _target(tid)
    tgs.forget_host(t)
    return tgs.shape(_target(tid))


@router.put("/api/targets/{tid}/db")
def targets_db_save(tid: int, body: DbBody, request: Request, user: dict = Depends(admin_only)):
    _target(tid)
    _terr(tgs.save_db, tid, body.model_dump())
    db.audit(user["email"], "server.db", f"{_target(tid)['name']} mode={body.mode}", ip_of(request))
    return tgs.shape(_target(tid))


@router.post("/api/targets/{tid}/db/test")
def targets_db_test(tid: int, body: DbBody, user: dict = Depends(operator_only)):
    return _terr(tgs.db_test, _target(tid), body.model_dump())


@router.put("/api/targets/auto/{tid}")
def targets_auto(tid: int, user: dict = Depends(admin_only)):
    _target(tid)
    deploy.set_cfg({"auto_target": tid})
    return {"auto_target": tid}


# ---- first-run setup wizard ---------------------------------------------------------------------
class SetupBody(BaseModel):
    domain: str = ""
    sub: str = "ops"
    https: bool = True
    ip: str = ""
    keep_access: bool = True


@router.get("/api/setup")
def setup_get(user: dict = Depends(current_user)):
    return {"done": setup.is_done(), "domain": config.ROOT_DOMAIN, "sub": config.OPS_SUBDOMAIN, "https": config.HTTPS,
            "placeholder": config.ROOT_DOMAIN in ("localhost", "example.com"), "panel_host": config.PANEL_HOST,
            "public_url": config.PUBLIC_URL, "ops_url": config.OPS_URL, "env_file": str(setup.env_path()),
            "env_writable": setup.can_write_env(), "keep_access": config.DEV, "systemd": setup.under_systemd(), "server_ip": setup.server_ip(),
            "github": {"token": bool(gh.token()), "org": gh.org(), "repos": gh.repos()}, "dev": config.DEV}


@router.post("/api/setup/dns")
def setup_dns(body: SetupBody, user: dict = Depends(admin_only)):
    try:
        d, s = setup.clean(body.domain, body.sub)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ip = body.ip.strip() or setup.server_ip()
    return {"ip": ip, "records": setup.dns_check(d, s, ip)}


@router.post("/api/setup/save")
def setup_save(body: SetupBody, request: Request, user: dict = Depends(admin_only)):
    try:
        d, s = setup.clean(body.domain, body.sub)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not setup.can_write_env():
        raise HTTPException(500, f"Cannot write {setup.env_path()}. Fix its permissions or edit it by hand.")
    path = setup.write_env({"OPS_ROOT_DOMAIN": d, "OPS_SUBDOMAIN": s, "OPS_HTTPS": "true" if body.https else "false",
                            "OPS_DEV": "true" if body.keep_access else "false"})
    changed = (d, s, bool(body.https)) != (config.ROOT_DOMAIN, config.OPS_SUBDOMAIN, bool(config.HTTPS))
    db.audit(user["email"], "setup.domain", f"{s}.{d} https={body.https}", ip_of(request))
    return {"saved": True, "env_file": path, "restart_needed": changed, "panel_host": f"{s}.{d}",
            "ops_url": f"{'https' if body.https else 'http'}://{s}.{d}", "public_url": f"{'https' if body.https else 'http'}://{d}"}


@router.post("/api/setup/restart")
def setup_restart(request: Request, user: dict = Depends(admin_only)):
    if not setup.under_systemd():
        raise HTTPException(400, "Not running under systemd. Restart it yourself: sudo systemctl restart nazar-ops (or stop and start uvicorn).")
    db.audit(user["email"], "setup.restart", "", ip_of(request))
    setup.schedule_restart()
    return {"restarting": True}


@router.post("/api/setup/finish")
def setup_finish(request: Request, user: dict = Depends(admin_only)):
    setup.mark_done(user["email"])
    db.audit(user["email"], "setup.done", "", ip_of(request))
    return {"done": True}


@router.post("/api/setup/reopen")
def setup_reopen(request: Request, user: dict = Depends(admin_only)):
    db.sdel("setup_done")
    return {"done": False}


# ---- server setup (generated configs) ----------------------------------------------------------
@router.get("/api/server/configs")
def server_configs(domain: str = "", sub: str = "", panel_cmd: str = "", panel_port: int = 8000, service: str = "nazar-panel", linux_user: str = "nazar",
                   ops_dir: str = "/opt/nazar-ops", ops_port: int = 8100, user: dict = Depends(admin_only)):
    cfg = deploy.get_cfg()
    try:
        r = serverconf.render(domain or (config.ROOT_DOMAIN if config.ROOT_DOMAIN != "localhost" else "example.com"), sub or config.OPS_SUBDOMAIN,
                              linux_user, ops_dir, ops_port, panel_port, cfg["deploy_root"], service, panel_cmd, config.MAX_UPLOAD_MB)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["domain"] = config.ROOT_DOMAIN
    r["restart_cmd"] = f"sudo systemctl restart {service}"
    r["placeholder_domain"] = config.ROOT_DOMAIN == "localhost"
    return r


# ---- users + audit ----------------------------------------------------------------------------
@router.get("/api/users")
def users_list(user: dict = Depends(admin_only)):
    return db.query("SELECT id,email,name,first_name,last_name,role,disabled,totp_enabled,created_at,last_login FROM users ORDER BY id")


class NewUser(BaseModel):
    email: str
    name: str = ""
    password: str
    role: str = "operator"


@router.post("/api/users")
def users_create(body: NewUser, request: Request, user: dict = Depends(admin_only)):
    email = body.email.strip().lower()
    if "@" not in email or len(email) > 120:
        raise HTTPException(400, "Enter a valid email")
    if body.role not in ("admin", "operator", "viewer"):
        raise HTTPException(400, "Role must be admin, operator or viewer")
    err = security.check_password_strength(body.password)
    if err:
        raise HTTPException(400, err)
    if db.one("SELECT id FROM users WHERE email=?", (email,)):
        raise HTTPException(400, "A user with this email already exists")
    db.execute("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
               (email, body.name.strip()[:60] or email.split("@")[0], security.hash_password(body.password), body.role, time.time()))
    db.audit(user["email"], "user.created", f"{email} ({body.role})", ip_of(request))
    return {"ok": True}


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None


@router.patch("/api/users/{uid}")
def users_patch(uid: int, body: UserPatch, request: Request, user: dict = Depends(admin_only)):
    target = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "User not found")
    admins = db.one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0")["n"]
    losing_admin = target["role"] == "admin" and ((body.role and body.role != "admin") or body.disabled)
    if losing_admin and admins <= 1:
        raise HTTPException(400, "You cannot remove the last active admin")
    if body.role:
        if body.role not in ("admin", "operator", "viewer"):
            raise HTTPException(400, "Invalid role")
        db.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
    if body.disabled is not None:
        db.execute("UPDATE users SET disabled=? WHERE id=?", (1 if body.disabled else 0, uid))
        if body.disabled:
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    if body.password:
        err = security.check_password_strength(body.password)
        if err:
            raise HTTPException(400, err)
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(body.password), uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    db.audit(user["email"], "user.edited", f"{target['email']} {body.model_dump(exclude={'password'}, exclude_none=True)}"
             + (" password reset" if body.password else ""), ip_of(request))
    return {"ok": True}


@router.get("/api/audit")
def audit_list(limit: int = 50, offset: int = 0, q: str = "", user: dict = Depends(current_user)):
    like = f"%{q}%"
    rows = db.query("SELECT * FROM audit WHERE (?='' OR action LIKE ? OR detail LIKE ? OR user_email LIKE ?) ORDER BY id DESC LIMIT ? OFFSET ?",
                    (q, like, like, like, min(limit, 200), offset))
    return {"items": rows}
