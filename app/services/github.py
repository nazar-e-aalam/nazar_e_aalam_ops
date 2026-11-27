"""GitHub REST client (stdlib urllib, so it works with no extra packages).
Features: ETag caching (304s are free against the rate limit), short TTL cache so
many open browsers share one upstream call, redirect-safe downloads (the token is
never sent to the signed storage URL), and token verification."""
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from ..core import config, db

REDIRECTS = (301, 302, 303, 307, 308)


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_cache: dict = {}
_lock = threading.Lock()
rate = {"limit": None, "remaining": None, "reset": None}


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def text(self):
        return self.body.decode("utf-8", "replace")


def token() -> str:
    return (db.sget("github_token") or config.GITHUB_TOKEN or "").strip()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def org() -> str:
    return db.sget("github_org") or config.GITHUB_ORG


def repos() -> dict:
    return {"app": db.sget("repo_app") or config.APP_REPO, "panel": db.sget("repo_panel") or config.PANEL_REPO}


def repo_path(key: str) -> str:
    r = repos()
    if key not in r:
        raise GitHubError(400, "Unknown repository key")
    return f"/repos/{org()}/{r[key]}"


def _friendly(status: int, body: bytes) -> str:
    try:
        msg = json.loads(body.decode()).get("message", "")
    except Exception:
        msg = body.decode("utf-8", "replace")[:200]
    if status == 401:
        return "GitHub rejected the token (bad credentials or expired)."
    if status == 403 and "rate limit" in msg.lower():
        return "GitHub API rate limit reached. Try again in a few minutes."
    if status == 403:
        return msg or "Forbidden: the token lacks the required permission."
    if status == 404:
        return "Not found (or the token cannot see this repository)."
    return msg or f"GitHub error {status}"


def request(method: str, path: str, *, body=None, params=None, accept="application/vnd.github+json",
            ttl: float = 0, timeout: int = 25, use_cache: bool = True) -> Resp:
    url = path if path.startswith("http") else config.GITHUB_API_URL + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    tok = token()
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": f"nazar-ops/{config.VERSION}"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    ckey = (url, accept)
    cacheable = method == "GET" and use_cache
    if cacheable:
        with _lock:
            hit = _cache.get(ckey)
        if hit and ttl and time.time() - hit["t"] < ttl:
            return Resp(200, hit["h"], hit["b"])
        if hit and hit.get("etag"):
            headers["If-None-Match"] = hit["etag"]
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        r = _opener.open(req, timeout=timeout)
        status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
        if status == 304 and cacheable and hit:
            with _lock:
                hit["t"] = time.time()
            _note_rate(hdrs)
            return Resp(200, hit["h"], hit["b"])
        if status not in REDIRECTS:
            _note_rate(hdrs)
            raise GitHubError(status, _friendly(status, raw))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GitHubError(0, f"Cannot reach GitHub: {getattr(e, 'reason', e)}")
    _note_rate(hdrs)
    if cacheable and status == 200:
        with _lock:
            if len(_cache) > 500:
                for k in list(_cache)[:100]:
                    _cache.pop(k, None)
            _cache[ckey] = {"etag": hdrs.get("ETag"), "h": hdrs, "b": raw, "t": time.time()}
    return Resp(status, hdrs, raw)


def _note_rate(h) -> None:
    try:
        if h.get("X-RateLimit-Remaining") is not None:
            rate.update(limit=int(h["X-RateLimit-Limit"]), remaining=int(h["X-RateLimit-Remaining"]),
                        reset=int(h["X-RateLimit-Reset"]))
    except (TypeError, ValueError, KeyError):
        pass


def jget(path: str, **kw):
    return request("GET", path, **kw).json()


def text_file(repo_key: str, file_path: str, ref: str) -> str:
    r = request("GET", f"{repo_path(repo_key)}/contents/{urllib.parse.quote(file_path)}",
                params={"ref": ref}, accept="application/vnd.github.raw+json", ttl=30)
    return r.text()


def download(path: str, dest: str, accept="application/vnd.github+json", max_bytes: int = 1 << 30) -> int:
    """Download a GitHub URL that answers with a redirect to signed storage."""
    resp = request("GET", path, accept=accept, use_cache=False, timeout=60)
    total = 0
    if resp.status in REDIRECTS:
        loc = resp.headers.get("Location")
        if not loc:
            raise GitHubError(502, "GitHub gave no download location")
        req = urllib.request.Request(loc, headers={"User-Agent": f"nazar-ops/{config.VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise GitHubError(413, "Download exceeds the size limit")
                    out.write(chunk)
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, f"Download failed ({e.code})")
        except (urllib.error.URLError, OSError) as e:
            raise GitHubError(0, f"Download failed: {e}")
    else:
        with open(dest, "wb") as out:
            out.write(resp.body)
        total = len(resp.body)
    return total


def proxy_base() -> str:
    """Proxy chosen in the panel (Releases tab) wins over OPS_GITHUB_PROXY. Empty string = proxy off."""
    v = db.sget("proxy_url")
    return (v if isinstance(v, str) else config.GITHUB_PROXY).strip().rstrip("/")


def proxied(url: str) -> str:
    """Wrap a github.com / raw.githubusercontent.com URL with the gh-proxy mirror."""
    px = proxy_base()
    if not px or url.startswith(px + "/"):
        return url
    host = urllib.parse.urlparse(url).netloc
    if host in ("github.com", "raw.githubusercontent.com", "gist.githubusercontent.com", "codeload.github.com"):
        return f"{px}/{url}"
    return url


def download_proxy(url: str, dest: str, max_bytes: int = 1 << 30) -> int:
    """Plain download through gh-proxy. No Authorization header, so no token leaves this server."""
    req = urllib.request.Request(proxied(url), headers={"User-Agent": f"nazar-ops/{config.VERSION}"})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise GitHubError(413, "Download exceeds the size limit")
                out.write(chunk)
    except urllib.error.HTTPError as e:
        raise GitHubError(e.code, f"Proxy download failed ({e.code})")
    except (urllib.error.URLError, OSError) as e:
        raise GitHubError(0, f"Proxy download failed: {e}")
    return total


_chk: dict = {}
_chk_lock = threading.Lock()


def check_url(url: str, timeout: int = 15, ttl: float = 0) -> dict:
    """HEAD the URL (following redirects). Some hosts reject HEAD, so a 1-byte ranged GET is the second try.
    Returns {ok, status, ms, error}. Nothing is downloaded. ttl>0 reuses a recent result."""
    now = time.time()
    if ttl:
        with _chk_lock:
            hit = _chk.get(url)
        if hit and now - hit["t"] < ttl:
            return hit["r"]
    ua = {"User-Agent": f"nazar-ops/{config.VERSION}"}
    res = {"ok": False, "status": 0, "ms": None, "error": ""}
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method=method, headers={**ua, **extra})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = {"ok": r.status < 400, "status": r.status, "ms": int((time.time() - t0) * 1000), "error": ""}
                break
        except urllib.error.HTTPError as e:
            res = {"ok": False, "status": e.code, "ms": int((time.time() - t0) * 1000), "error": f"HTTP {e.code}"}
            if e.code in (405, 403, 501, 400) and method == "HEAD":
                continue  # HEAD refused, try the ranged GET
            break
        except Exception as e:
            res = {"ok": False, "status": 0, "ms": None, "error": str(getattr(e, "reason", e))[:120]}
            break
    with _chk_lock:
        if len(_chk) > 300:
            _chk.clear()
        _chk[url] = {"t": now, "r": res}
    return res


def probe(url: str) -> bool:
    return check_url(url)["ok"]


def pick_url(proxy_url, direct_url):
    """The URL a visitor is sent to. direct_url is the truth; the proxy URL is derived from it with the
    current proxy setting, so changing the proxy takes effect at once. The proxy is used only if a HEAD
    check of THAT file through the proxy works (cached 2 min); otherwise the plain GitHub URL."""
    if direct_url:
        purl = proxied(direct_url)
        if purl != direct_url and check_url(purl, timeout=8, ttl=120)["ok"]:
            return purl
        return direct_url
    return proxy_url


def asset_links(asset: dict):
    """(proxy_url, direct_url) for a release asset. Never downloads anything."""
    direct = asset.get("browser_download_url")
    if not direct:
        return None, None
    purl = proxied(direct)
    return (purl if purl != direct else None), direct


def proxy_report(sample_direct: str = None, base: str = None) -> dict:
    """Live test used by the Releases tab: GitHub directly, the proxy, and which one will be used."""
    base = proxy_base() if base is None else base.strip().rstrip("/")
    target = sample_direct or "https://github.com/"
    out = {"proxy": base, "sample": target, "direct": check_url(target, ttl=0), "via_proxy": None}
    if base:
        out["via_proxy"] = check_url(f"{base}/{target}", ttl=0)
    out["using"] = "proxy" if (out["via_proxy"] and out["via_proxy"]["ok"]) else "direct"
    return out


def dispatch(repo_key: str, workflow_id, ref: str, inputs: dict):
    """Fire workflow_dispatch. Returns (server_time_epoch, run_id_or_None)."""
    r = request("POST", f"{repo_path(repo_key)}/actions/workflows/{workflow_id}/dispatches",
                body={"ref": ref, "inputs": inputs})
    server_time = time.time()
    try:
        server_time = parsedate_to_datetime(r.headers.get("Date")).timestamp()
    except Exception:
        pass
    run_id = None
    try:
        body = r.json() or {}
        run_id = body.get("workflow_run_id") or body.get("id")
    except Exception:
        pass
    return server_time, run_id


def iso_to_epoch(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def verify() -> dict:
    """Check the token end to end and report in a UI friendly shape."""
    checks, info = [], {"token_set": bool(token())}
    if not token():
        return {"ok": False, "checks": [{"name": "Token", "ok": False,
                                        "detail": "No GitHub token configured. Add one in Settings or OPS_GITHUB_TOKEN."}],
                "info": info}
    tok = token()
    info["kind"] = ("fine-grained" if tok.startswith("github_pat_") else "classic" if tok.startswith("ghp_")
                    else "app/OAuth" if tok[:4] in ("ghs_", "ghu_", "gho_") else "unknown")
    info["fingerprint"] = "…" + hashlib.sha256(tok.encode()).hexdigest()[:6]
    ok_all = True
    try:
        r = request("GET", "/user", use_cache=False)
        u = r.json()
        info.update(login=u.get("login"), name=u.get("name"), avatar=u.get("avatar_url"), url=u.get("html_url"))
        scopes = r.headers.get("X-OAuth-Scopes")
        info["scopes"] = [s.strip() for s in scopes.split(",") if s.strip()] if scopes is not None else None
        info["expires"] = r.headers.get("github-authentication-token-expiration")
        checks.append({"name": "Token valid", "ok": True, "detail": f"Authenticated as @{u.get('login')} ({info['kind']} token)"})
    except GitHubError as e:
        return {"ok": False, "checks": [{"name": "Token valid", "ok": False, "detail": e.message}], "info": info}
    for key, name in repos().items():
        label = f"{org()}/{name}"
        try:
            rp = jget(repo_path(key), use_cache=False)
            perms = rp.get("permissions") or {}
            can_push = bool(perms.get("push") or perms.get("admin"))
            info.setdefault("repos", {})[key] = {"name": name, "private": rp.get("private"),
                                                  "default_branch": rp.get("default_branch"), "push": can_push}
            checks.append({"name": f"Repo {label}", "ok": True,
                           "detail": ("private" if rp.get("private") else "public") + f", default branch {rp.get('default_branch')}"})
        except GitHubError as e:
            ok_all = False
            checks.append({"name": f"Repo {label}", "ok": False, "detail": e.message})
            continue
        try:
            w = jget(repo_path(key) + "/actions/workflows", params={"per_page": 1}, use_cache=False)
            checks.append({"name": f"Actions read ({name})", "ok": True, "detail": f"{w.get('total_count', 0)} workflow(s) visible"})
        except GitHubError as e:
            ok_all = False
            checks.append({"name": f"Actions read ({name})", "ok": False, "detail": e.message + " Needs Actions: read."})
        if key == "app":
            checks.append({"name": "Run workflows", "ok": bool(info["repos"][key]["push"]),
                           "detail": "Write access present, dispatch should work." if info["repos"][key]["push"]
                           else "Token looks read-only. Dispatching needs Actions: write (fine-grained) or the workflow scope (classic)."})
            ok_all = ok_all and bool(info["repos"][key]["push"])
    try:
        rl = jget("/rate_limit", use_cache=False)["resources"]["core"]
        info["rate"] = {"limit": rl["limit"], "remaining": rl["remaining"], "reset": rl["reset"]}
        checks.append({"name": "Rate limit", "ok": rl["remaining"] > 50, "detail": f"{rl['remaining']} / {rl['limit']} requests left this hour"})
    except GitHubError:
        pass
    return {"ok": ok_all, "checks": checks, "info": info}
