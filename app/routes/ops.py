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

from sqlalchemy import func, or_, select

from ..core import config, envstore, mailer, security, totp
from ..db import db
from ..db.models import (App, Asset, Audit, Deployment, LoginChallenge, RecoveryCode, Release, TrackedRun, User,
                         UserSession)
from ..services import bootstrap, deploy, library, notify, serverconf, setup, targets as tgs, workflows
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


_SEC_CACHE: list = [0.0, None]       # [fetched_at, value]; one worker process, so a short cache is safe


def security_cfg(fresh: bool = False) -> dict:
    now = time.time()
    if fresh or _SEC_CACHE[1] is None or now - _SEC_CACHE[0] > 5:
        _SEC_CACHE[:] = [now, {"require_2fa": bool((db.sget("security_cfg") or {}).get("require_2fa"))}]
    return dict(_SEC_CACHE[1])


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
    db.update(User, User.id == user_id, last_login=time.time())
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
    u = db.get(User, User.email == email)
    ok = security.verify_password(body.password, u["password_hash"] if u else _DUMMY_HASH)
    if not u or not ok or u["disabled"]:
        security.note_failure(key)
        db.audit(email, "login.failed", "", ip_of(request))
        raise HTTPException(401, "Wrong email or password")
    security.clear_failures(key)
    if u["totp_enabled"]:
        challenge = security.new_token()
        db.delete(LoginChallenge, LoginChallenge.expires_at < time.time())
        db.insert(LoginChallenge, token_hash=security.token_hash(challenge), user_id=u["id"], expires_at=time.time() + 300)
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
    ch = db.get(LoginChallenge, LoginChallenge.token_hash == th)
    if not ch or ch["expires_at"] < time.time():
        raise HTTPException(401, "This sign-in expired. Enter your password again.")
    u = db.get(User, User.id == ch["user_id"])
    if not u or u["disabled"] or not u["totp_enabled"]:
        raise HTTPException(401, "This sign-in expired. Enter your password again.")
    if not accept_second_factor(u, body.code):
        security.note_failure(key)
        db.update(LoginChallenge, LoginChallenge.token_hash == th, attempts=LoginChallenge.attempts + 1)
        if ch["attempts"] + 1 >= 5:
            db.delete(LoginChallenge, LoginChallenge.token_hash == th)
            db.audit(u["email"], "login.2fa_locked", "", ip_of(request))
            raise HTTPException(401, "Too many wrong codes. Enter your password again.")
        db.audit(u["email"], "login.2fa_failed", "", ip_of(request))
        raise HTTPException(401, "That code is not right")
    security.clear_failures(key)
    db.delete(LoginChallenge, LoginChallenge.token_hash == th)
    start_session(u["id"], request, response)
    db.audit(u["email"], "login", "2fa", ip_of(request))
    return {"user": user_view(db.get(User, User.id == u["id"]))}


def accept_second_factor(u: dict, code: str, allow_recovery: bool = True) -> bool:
    """Checks an authenticator code (replay-protected) or, if allowed, a single-use recovery code."""
    code = (code or "").strip()
    step = totp.verify(u["totp_secret"] or "", code, u["totp_last_step"])
    if step is not None:
        return db.update(User, User.id == u["id"], User.totp_last_step < step, totp_last_step=step) == 1
    if allow_recovery and totp.norm_recovery(code):
        return db.update(RecoveryCode, RecoveryCode.user_id == u["id"], RecoveryCode.code_hash == totp.hash_recovery(code),
                         RecoveryCode.used_at.is_(None), used_at=time.time()) == 1
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
    db.update(User, User.id == user["id"], first_name=first, last_name=last, name=f"{first} {last}".strip())
    db.audit(user["email"], "profile.updated", "", ip_of(request))
    return {"user": user_view(db.get(User, User.id == user["id"]))}


class PasswordBody(BaseModel):
    current: str
    new: str


@router.post("/api/me/password")
def change_password(body: PasswordBody, request: Request, user: dict = Depends(current_user)):
    row = db.get(User, User.id == user["id"], cols=[User.password_hash])
    if not security.verify_password(body.current, row["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    err = security.check_password_strength(body.new)
    if err:
        raise HTTPException(400, err)
    db.update(User, User.id == user["id"], password_hash=security.hash_password(body.new))
    me_tok = request.cookies.get(security.COOKIE)
    keep = hashlib.sha256(me_tok.encode()).hexdigest() if me_tok else ""
    db.delete(UserSession, UserSession.user_id == user["id"], UserSession.token_hash != keep)
    db.audit(user["email"], "password.changed", "", ip_of(request))
    return {"ok": True}


@router.get("/api/me/sessions")
def my_sessions(request: Request, user: dict = Depends(current_user)):
    tok = request.cookies.get(security.COOKIE) or ""
    mine = hashlib.sha256(tok.encode()).hexdigest()
    rows = db.all(UserSession, UserSession.user_id == user["id"], order=UserSession.last_seen.desc(),
                  cols=[UserSession.token_hash, UserSession.created_at, UserSession.last_seen, UserSession.ip, UserSession.ua])
    return [{"id": r["token_hash"][:12], "created_at": r["created_at"], "last_seen": r["last_seen"], "ip": r["ip"],
             "ua": r["ua"], "current": r["token_hash"] == mine} for r in rows]


@router.delete("/api/me/sessions/{sid}")
def revoke_session(sid: str, user: dict = Depends(current_user)):
    if len(sid) == 12:      # token hashes are 64 chars, so only a 12-char id can equal their first 12
        db.delete(UserSession, UserSession.user_id == user["id"], UserSession.token_hash.startswith(sid, autoescape=True))
    return {"ok": True}


# ---- overview --------------------------------------------------------------------
@router.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    out = {"online": db.select_rows(
        select(User.email, User.name, User.role, func.max(UserSession.last_seen).label("last_seen"))
        .select_from(UserSession.__table__.join(User.__table__, User.id == UserSession.user_id))
        .where(UserSession.last_seen > time.time() - 300, UserSession.expires_at > time.time())
        .group_by(User.id, User.email, User.name, User.role).order_by(func.max(UserSession.last_seen).desc())),
           "token": {"set": bool(gh.token()), "rate": gh.rate},
           "server": deploy.server_status(),
           "last_deploys": [deploy.shape(d) for d in db.all(Deployment, order=Deployment.id.desc(), limit=5)],
           "activity": db.all(Audit, order=Audit.id.desc(), limit=12, cols=[Audit.ts, Audit.user_email, Audit.action, Audit.detail]),
           "library": library.public_apps(), "runs": [], "runs_error": None, "running": 0}
    try:
        data = gh.jget(gh.repo_path("app") + "/actions/runs", params={"per_page": 8}, ttl=8)
        tracked = {r["run_id"]: r for r in db.all(TrackedRun, TrackedRun.repo == "app", order=TrackedRun.run_id.desc(), limit=200)}
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
        taken = {r["run_id"] for r in db.all(TrackedRun, TrackedRun.repo == key, cols=[TrackedRun.run_id])}
        cands = [r for r in runs.get("workflow_runs", [])
                 if r["id"] not in taken and (gh.iso_to_epoch(r["created_at"]) or 0) >= t0 - 3]
        if cands:
            run_id = min(cands, key=lambda r: r["id"])["id"]
    if run_id:
        db.insert_ignore(TrackedRun, repo=key, run_id=run_id, workflow=wf["name"], ref=ref, inputs=repr(inputs),
                         triggered_by=user["email"], triggered_at=time.time())
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
    tracked = {r["run_id"]: r for r in db.all(TrackedRun, TrackedRun.repo == key, order=TrackedRun.run_id.desc(), limit=500)}
    return {"total": data.get("total_count", 0), "page": page,
            "runs": [workflows.shape_run(r, tracked.get(r["id"])) for r in data.get("workflow_runs", [])]}


@router.get("/api/runs/{key}/{run_id}")
def run_detail(key: str, run_id: int, user: dict = Depends(current_user)):
    rp = gh.repo_path(repo_key(key))
    run = gh_call(gh.jget, f"{rp}/actions/runs/{run_id}", ttl=2.5)
    jobs = gh_call(gh.jget, f"{rp}/actions/runs/{run_id}/jobs", params={"per_page": 100, "filter": "latest"}, ttl=2.5)["jobs"]
    wf = workflows.get_definition(key, run.get("path", "").split("@")[0], run["head_sha"])
    tracked = db.get(TrackedRun, TrackedRun.repo == key, TrackedRun.run_id == run_id)
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
        rows = db.all(Deployment, Deployment.target_id == target, order=Deployment.id.desc(), limit=max(1, min(limit, 100)), offset=max(0, offset))
    else:
        rows = db.all(Deployment, order=Deployment.id.desc(), limit=max(1, min(limit, 100)), offset=max(0, offset))
    return {"deployments": [deploy.shape(r) for r in rows], "releases": deploy.releases_on_disk(max(target, 0))}


@router.get("/api/deploy/preflight")
def deploy_preflight(target: int = 0, user: dict = Depends(current_user)):
    return deploy.preflight(target)


@router.get("/api/deploy/head")
def deploy_head(ref: str = "", user: dict = Depends(current_user)):
    cfg = deploy.get_cfg()
    c = gh_call(deploy.resolve_commit, ref or cfg["branch"])
    c["deployed"] = db.exists(Deployment, Deployment.sha == c["sha"], Deployment.kind == "deploy", Deployment.status == "success")
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
    d = db.get(Deployment, Deployment.id == dep_id)
    if not d:
        raise HTTPException(404, "Deployment not found")
    return deploy.shape(d)


@router.get("/api/deploy/{dep_id}/log")
def deploy_log(dep_id: int, offset: int = 0, user: dict = Depends(current_user)):
    text, nxt = deploy.read_log(dep_id, max(0, offset))
    d = db.get(Deployment, Deployment.id == dep_id, cols=[Deployment.status])
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
    src = db.get(Deployment, Deployment.release_name == name, Deployment.target_id == t["id"], order=Deployment.id.desc(),
                 cols=[Deployment.sha, Deployment.short_sha, Deployment.message, Deployment.author, Deployment.branch]) or {}
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
    if not hmac.compare_digest(sig.encode("utf-8", "replace"), expected.encode()):
        raise HTTPException(401, "Bad signature")
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return {"pong": True}
    if event != "push":
        return Response(status_code=202)
    import json
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise HTTPException(400, "Body is not valid JSON")
    code, msg = deploy.handle_push(payload)
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
    return {"apps": db.all(App, order=App.name), "releases": library.list_releases(public=False),
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
            "forgotten": len(library._skip_set()), "importing": db.count(Release, Release.status == "importing")}


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
    a = db.get(Asset, Asset.direct_url.isnot(None), order=Asset.id.desc(), cols=[Asset.direct_url])
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
    r = db.get(Release, Release.id == rid, cols=[Release.id])
    if not r:
        raise HTTPException(404, "Release not found")
    vals = {}
    if body.visible is not None:
        vals["visible"] = 1 if body.visible else 0
    if body.notes is not None:
        vals["notes"] = body.notes[:20000]
    if body.channel in ("stable", "beta"):
        vals["channel"] = body.channel
    db.update(Release, Release.id == rid, **vals)
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
    # sudo works now: set the machine up automatically (packages, Python, folders, test build). The password stays in memory only.
    setup_id = None
    try:
        setup_id = bootstrap.start(t, user["email"], body.password)
    except ValueError:
        pass
    return {"result": res, "target": tgs.shape(_target(tid)), "bootstrap_id": setup_id}


@router.post("/api/targets/{tid}/bootstrap")
def targets_bootstrap(tid: int, body: SudoBody, request: Request, user: dict = Depends(admin_only)):
    """Run the server set-up again (root / passwordless sudo need no password)."""
    t = _target(tid)
    try:
        dep_id = bootstrap.start(t, user["email"], body.password)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.audit(user["email"], "server.setup", t["name"], ip_of(request))
    return {"id": dep_id}


@router.post("/api/targets/{tid}/forget-host")
def targets_forget(tid: int, user: dict = Depends(admin_only)):
    t = _target(tid)
    tgs.forget_host(t)
    return tgs.shape(_target(tid))


@router.put("/api/targets/auto/{tid}")
def targets_auto(tid: int, user: dict = Depends(admin_only)):
    _target(tid)
    deploy.set_cfg({"auto_target": tid})
    return {"auto_target": tid}


# ---- notifications, email, database ----------------------------------------------------------------------
@router.get("/api/notifications")
def notifications(user: dict = Depends(current_user)):
    return notify.feed(user["id"])


@router.post("/api/notifications/seen")
def notifications_seen(user: dict = Depends(current_user)):
    notify.mark_seen(user["id"])
    return {"ok": True}


@router.get("/api/notifications/config")
def notify_cfg_get(user: dict = Depends(admin_only)):
    return {**notify.get_cfg(), "mail_ready": mailer.configured(), "recipients": notify.recipients()}


class NotifyCfgBody(BaseModel):
    email: bool = True
    on_success: bool = False
    extra_emails: str = ""


@router.put("/api/notifications/config")
def notify_cfg_put(body: NotifyCfgBody, request: Request, user: dict = Depends(admin_only)):
    try:
        cfg = notify.set_cfg(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user["email"], "notify.config", "", ip_of(request))
    return {**cfg, "mail_ready": mailer.configured(), "recipients": notify.recipients()}


@router.get("/api/email/preview/{kind}")
def email_preview(kind: str, user: dict = Depends(admin_only)):
    try:
        return Response(mailer.preview(kind), media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
                                 "X-Frame-Options": "SAMEORIGIN"})
    except KeyError:
        raise HTTPException(404, "Unknown template")


@router.get("/api/system/database")
def system_database(user: dict = Depends(admin_only)):
    return db.info()


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
            "ops_url": f"{'https' if body.https else 'http'}://{s}.{d}", "public_url": f"{'https' if body.https else 'http'}://{s}.{d}"}


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
    return db.all(User, order=User.id, cols=[User.id, User.email, User.name, User.first_name, User.last_name, User.role, User.disabled,
                                      User.totp_enabled, User.created_at, User.last_login])


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
    if db.exists(User, User.email == email):
        raise HTTPException(400, "A user with this email already exists")
    db.insert(User, email=email, name=body.name.strip()[:60] or email.split("@")[0],
              password_hash=security.hash_password(body.password), role=body.role, created_at=time.time())
    db.audit(user["email"], "user.created", f"{email} ({body.role})", ip_of(request))
    return {"ok": True}


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None


@router.patch("/api/users/{uid}")
def users_patch(uid: int, body: UserPatch, request: Request, user: dict = Depends(admin_only)):
    target = db.get(User, User.id == uid)
    if not target:
        raise HTTPException(404, "User not found")
    admins = db.count(User, User.role == "admin", User.disabled == 0)
    losing_admin = target["role"] == "admin" and not target["disabled"] and ((body.role and body.role != "admin") or body.disabled)
    if losing_admin and admins <= 1:
        raise HTTPException(400, "You cannot remove the last active admin")
    if body.role and body.role not in ("admin", "operator", "viewer"):
        raise HTTPException(400, "Invalid role")
    if body.password:
        err = security.check_password_strength(body.password)
        if err:
            raise HTTPException(400, err)
    ph = security.hash_password(body.password) if body.password else None
    with db.transaction() as tx:                      # all the changes together or none (a half-applied edit helped nobody)
        vals = {}
        if body.role:
            vals["role"] = body.role
        if body.disabled is not None:
            vals["disabled"] = 1 if body.disabled else 0
        if ph:
            vals["password_hash"] = ph
        tx.update(User, User.id == uid, **vals)
        if body.disabled or ph:
            tx.delete(UserSession, UserSession.user_id == uid)
            tx.delete(LoginChallenge, LoginChallenge.user_id == uid)
    db.audit(user["email"], "user.edited", f"{target['email']} {body.model_dump(exclude={'password'}, exclude_none=True)}"
             + (" password reset" if body.password else ""), ip_of(request))
    return {"ok": True}


@router.get("/api/audit")
def audit_list(limit: int = 50, offset: int = 0, q: str = "", user: dict = Depends(current_user)):
    conds = []
    if q:
        conds.append(or_(Audit.action.contains(q, autoescape=True), Audit.detail.contains(q, autoescape=True),
                         Audit.user_email.contains(q, autoescape=True)))
    rows = db.all(Audit, *conds, order=Audit.id.desc(), limit=max(1, min(limit, 200)), offset=max(0, offset))
    return {"items": rows}
