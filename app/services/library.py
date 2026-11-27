"""Release library: the files served publicly at /applications.
Sources: manual upload, a GitHub Release of the app repo, or a workflow-run artifact."""
import hashlib
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import zipfile
from pathlib import Path

from ..core import config, db
from . import github as gh, setup

ALLOWED_EXT = {".apk": "android", ".aab": "android", ".ipa": "ios", ".zip": "other", ".dmg": "other",
               ".exe": "other", ".msi": "other", ".deb": "other", ".appimage": "other"}
PLATFORMS = ("android", "ios", "other")
RESERVED_SLUGS = {"api", "download", "static", "latest"}
MIME = {".apk": "application/vnd.android.package-archive", ".aab": "application/octet-stream",
        ".ipa": "application/octet-stream", ".zip": "application/zip"}


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40]


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._ +()-]", "_", name).strip(" .")
    if not name:
        raise ValueError("Invalid file name")
    return name[:120]


def clean_version(v: str) -> str:
    v = (v or "").strip()
    if v[:1] in "vV" and v[1:2].isdigit():
        v = v[1:]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}", v):
        raise ValueError("Version may contain letters, digits and . _ + - only (e.g. 1.4.2 or 1.4.2+15)")
    return v


def ensure_default_app() -> None:
    if not db.one("SELECT id FROM apps LIMIT 1"):
        db.execute("INSERT INTO apps(slug,name,description,created_at) VALUES(?,?,?,?)",
                   ("nazar-e-aalam", "Nazar e Aalam", "The Nazar e Aalam Islamic app.", time.time()))


def get_app(slug_or_id):
    if isinstance(slug_or_id, int) or str(slug_or_id).isdigit():
        return db.one("SELECT * FROM apps WHERE id=?", (int(slug_or_id),))
    return db.one("SELECT * FROM apps WHERE slug=?", (slug_or_id,))


def create_app(name: str, slug: str = "", description: str = "") -> dict:
    name = name.strip()
    if not name:
        raise ValueError("App name is required")
    slug = slugify(slug or name)
    if not slug or slug in RESERVED_SLUGS:
        raise ValueError("Choose a different slug")
    if db.one("SELECT id FROM apps WHERE slug=?", (slug,)):
        raise ValueError("An app with this slug already exists")
    db.execute("INSERT INTO apps(slug,name,description,created_at) VALUES(?,?,?,?)",
               (slug, name[:80], description.strip()[:400], time.time()))
    return db.one("SELECT * FROM apps WHERE slug=?", (slug,))


def _asset_dir(app: dict, version: str, platform: str) -> Path:
    d = config.APPS_DIR / app["slug"] / version / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def hash_file(path: Path):
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while True:
            c = f.read(1 << 20)
            if not c:
                break
            h.update(c)
            size += len(c)
    return size, h.hexdigest()


def copy_limited(src, dest: Path):
    """Copy a file-like object to dest while enforcing the size limit. Returns (size, sha256)."""
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    h, size = hashlib.sha256(), 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"File is larger than the {config.MAX_UPLOAD_MB} MB limit")
                h.update(chunk)
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return size, h.hexdigest()


def _add_remote_asset(release_id: int, proxy_url, direct_url: str, size: int, sha: str, filename: str) -> None:
    """Store only URLs (gh-proxy + direct GitHub). Nothing is downloaded to this server."""
    ext = Path(filename).suffix.lower()
    ctype = MIME.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    db.execute("INSERT INTO assets(release_id,filename,size,sha256,path,content_type,remote_url,direct_url,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (release_id, filename, int(size or 0), sha, "", ctype, proxy_url or direct_url, direct_url, time.time()))


def _attach_release_asset(rid: int, app: dict, version: str, platform: str, a: dict) -> bool:
    """Copy the version + URLs of a GitHub release asset. Proxy URL is preferred, the direct GitHub URL is the
    fallback (chosen live when a visitor downloads). Never downloads the file."""
    proxy_url, direct_url = gh.asset_links(a)
    if not direct_url:
        raise ValueError(f"{a['name']} has no download URL")
    digest = a.get("digest") or ""
    sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    _add_remote_asset(rid, proxy_url, direct_url, a.get("size", 0), sha, safe_filename(a["name"]))
    return True


def _add_asset(release_id: int, path: Path, size: int, sha: str, filename: str) -> None:
    ext = Path(filename).suffix.lower()
    ctype = MIME.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    db.execute("INSERT INTO assets(release_id,filename,size,sha256,path,content_type,created_at) VALUES(?,?,?,?,?,?,?)",
               (release_id, filename, size, sha, str(path), ctype, time.time()))


def _new_release(app: dict, version: str, platform: str, channel: str, notes: str, user: str, source: str,
                 source_ref: str = "", status: str = "ready", published_at: float = None) -> int:
    if platform not in PLATFORMS:
        raise ValueError("Platform must be android, ios or other")
    if channel not in ("stable", "beta"):
        raise ValueError("Channel must be stable or beta")
    old = db.one("SELECT id,status FROM releases WHERE app_id=? AND version=? AND platform=?", (app["id"], version, platform))
    if old:
        if old["status"] == "failed":
            delete_release(old["id"])
        else:
            raise ValueError(f"{app['name']} {version} ({platform}) already exists in the library")
    return db.execute(
        """INSERT INTO releases(app_id,version,platform,channel,notes,visible,status,source,source_ref,created_by,created_at,published_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (app["id"], version, platform, channel, notes.strip()[:20000], 1, status, source, source_ref, user,
         time.time(), published_at or time.time()))


def add_upload(app: dict, version: str, platform: str, channel: str, notes: str, files: list, user: str) -> int:
    """files: list of (filename, file-like). Blocking, call from a worker thread."""
    version = clean_version(version)
    if not files:
        raise ValueError("Choose at least one file")
    rid = _new_release(app, version, platform, channel, notes, user, "upload", status="importing")
    folder = _asset_dir(app, version, platform)
    try:
        for fname, fobj in files:
            fname = safe_filename(fname)
            if Path(fname).suffix.lower() not in ALLOWED_EXT:
                raise ValueError(f"'{fname}': allowed types are {', '.join(sorted(ALLOWED_EXT))}")
            dest = folder / fname
            size, sha = copy_limited(fobj, dest)
            _add_asset(rid, dest, size, sha, fname)
        db.execute("UPDATE releases SET status='ready' WHERE id=?", (rid,))
    except BaseException:
        delete_release(rid)
        raise
    return rid


def delete_release(rid: int, ignore: bool = False) -> None:
    """ignore=True: a GitHub-synced release the admin deleted on purpose is not re-imported by the next sync."""
    r = db.one("""SELECT r.version,r.platform,r.source,r.app_id,a.slug FROM releases r JOIN apps a ON a.id=r.app_id WHERE r.id=?""", (rid,))
    if r and ignore and r["source"] == "github":
        _skip_add(f"{r['app_id']}|{r['version']}|{r['platform']}")
    db.execute("DELETE FROM releases WHERE id=?", (rid,))
    if r:
        shutil.rmtree(config.APPS_DIR / r["slug"] / r["version"] / r["platform"], ignore_errors=True)
        parent = config.APPS_DIR / r["slug"] / r["version"]
        try:
            parent.rmdir()
        except OSError:
            pass


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _fail(rids: list, err: str) -> None:
    for rid in rids:
        db.execute("UPDATE releases SET status='failed', error=? WHERE id=?", (err[:300], rid))


# ---- import: GitHub release ------------------------------------------------------
def github_releases() -> list:
    items = gh.jget(gh.repo_path("app") + "/releases", params={"per_page": 30}, ttl=20)
    imported = {(r["source_ref"]) for r in db.query("SELECT source_ref FROM releases WHERE source='github' AND status!='failed'")}
    out = []
    for r in items:
        assets = [{"id": a["id"], "name": a["name"], "size": a["size"], "downloads": a.get("download_count", 0)}
                  for a in r.get("assets", []) if Path(a["name"]).suffix.lower() in ALLOWED_EXT]
        out.append({"id": r["id"], "tag": r["tag_name"], "name": r.get("name") or r["tag_name"], "draft": r.get("draft"),
                    "prerelease": r.get("prerelease"), "published_at": r.get("published_at"), "url": r.get("html_url"),
                    "body": (r.get("body") or "")[:4000], "assets": assets, "imported": r["tag_name"] in imported,
                    "author": (r.get("author") or {}).get("login")})
    return out


def import_github_release(tag: str, app: dict, user: str, notes: str = None) -> list:
    rel = gh.jget(f"{gh.repo_path('app')}/releases/tags/{urllib.parse.quote(tag, safe='')}", use_cache=False)
    assets = [a for a in rel.get("assets", []) if Path(a["name"]).suffix.lower() in ALLOWED_EXT]
    if not assets:
        raise ValueError("This GitHub release has no installable files (.apk, .aab, .ipa, .zip).")
    version = clean_version(rel["tag_name"])
    channel = "beta" if rel.get("prerelease") else "stable"
    body = notes if notes is not None else (rel.get("body") or "")
    pub = gh.iso_to_epoch(rel.get("published_at"))
    groups: dict = {}
    for a in assets:
        groups.setdefault(ALLOWED_EXT[Path(a["name"]).suffix.lower()], []).append(a)
    rids = {p: _new_release(app, version, p, channel, body, user, "github", rel["tag_name"], "importing", pub) for p in groups}

    def work():
        try:
            for platform, items in groups.items():
                for a in items:
                    _attach_release_asset(rids[platform], app, version, platform, a)
                db.execute("UPDATE releases SET status='ready' WHERE id=?", (rids[platform],))
        except Exception as e:
            traceback.print_exc()
            _fail(list(rids.values()), getattr(e, "message", str(e)))
    _spawn(work)
    return list(rids.values())


# ---- import: workflow artifact -----------------------------------------------------
def import_artifact(repo_key: str, artifact_id: int, app: dict, version: str, platform: str, channel: str,
                    notes: str, user: str) -> int:
    version = clean_version(version)
    art = gh.jget(f"{gh.repo_path(repo_key)}/actions/artifacts/{int(artifact_id)}", use_cache=False)
    if art.get("expired"):
        raise ValueError("This artifact has expired on GitHub.")
    rid = _new_release(app, version, platform, channel, notes, user, "artifact", f"{repo_key}:{artifact_id}", "importing")

    def work():
        tmp = config.TMP_DIR / f"artifact-{artifact_id}-{int(time.time())}.zip"
        try:
            gh.download(f"{gh.repo_path(repo_key)}/actions/artifacts/{int(artifact_id)}/zip", str(tmp),
                        max_bytes=config.MAX_UPLOAD_MB * 1024 * 1024)
            folder = _asset_dir(app, version, platform)
            limit, total, count = config.MAX_UPLOAD_MB * 1024 * 1024, 0, 0
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    if info.is_dir() or Path(info.filename).suffix.lower() not in ALLOWED_EXT:
                        continue
                    total += info.file_size
                    if total > limit:
                        raise ValueError("Artifact contents exceed the size limit")
                    fname = safe_filename(info.filename)
                    dest = folder / fname
                    with z.open(info) as src:
                        size, sha = copy_limited(src, dest)
                    _add_asset(rid, dest, size, sha, fname)
                    count += 1
            if not count:  # nothing recognisable inside: publish the zip itself
                fname = safe_filename(art["name"] + ".zip")
                dest = folder / fname
                shutil.copyfile(tmp, dest)
                size, sha = hash_file(dest)
                _add_asset(rid, dest, size, sha, fname)
            db.execute("UPDATE releases SET status='ready' WHERE id=?", (rid,))
        except Exception as e:
            traceback.print_exc()
            _fail([rid], getattr(e, "message", str(e)))
        finally:
            tmp.unlink(missing_ok=True)
    _spawn(work)
    return rid


# ---- listing -------------------------------------------------------------------------
def _asset_view(a: dict) -> dict:
    ext = Path(a["filename"]).suffix.lower().lstrip(".")
    return {"id": a["id"], "filename": a["filename"], "size": a["size"], "sha256": a["sha256"],
            "downloads": a["downloads"], "kind": ext,
            "url": f"/applications/download/{a['id']}/{urllib.parse.quote(a['filename'])}",
            "remote": bool(a.get("remote_url")),
            "source_url": gh.pick_url(a.get("remote_url"), a.get("direct_url")) if a.get("remote_url") else None,
            "direct_url": a.get("direct_url")}


def _vkey(v: str) -> tuple:
    """Sort key so 1.10.0 > 1.9.0 and latest means highest version, not last published."""
    nums = [int(x) for x in re.findall(r"\d+", (v or "").split("+")[0].split("-")[0])[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def list_releases(app_id: int = None, public: bool = True) -> list:
    where, args = [], []
    if app_id:
        where.append("r.app_id=?")
        args.append(app_id)
    if public:
        where.append("r.visible=1 AND r.status='ready'")
    rows = db.query(f"""SELECT r.*, a.slug AS app_slug, a.name AS app_name FROM releases r JOIN apps a ON a.id=r.app_id
                        {'WHERE ' + ' AND '.join(where) if where else ''}
                        ORDER BY COALESCE(r.published_at,r.created_at) DESC, r.id DESC""", tuple(args))
    if not rows:
        return []
    rows.sort(key=lambda r: (_vkey(r["version"]), r["published_at"] or r["created_at"], r["id"]), reverse=True)
    ids = ",".join(str(r["id"]) for r in rows)
    by_rel: dict = {}
    for a in db.query(f"SELECT * FROM assets WHERE release_id IN ({ids}) ORDER BY id"):
        by_rel.setdefault(a["release_id"], []).append(_asset_view(a))
    seen = set()
    for r in rows:
        r["assets"] = by_rel.get(r["id"], [])
        r["downloads"] = sum(a["downloads"] for a in r["assets"])
        key = (r["app_id"], r["platform"])
        r["latest"] = key not in seen and r["channel"] == "stable" and bool(r["visible"]) and r["status"] == "ready"
        if r["latest"]:
            seen.add(key)
    return rows


def public_apps() -> list:
    apps = db.query("SELECT id,slug,name,description FROM apps ORDER BY name")
    rels = list_releases(public=True)
    for a in apps:
        mine = [r for r in rels if r["app_id"] == a["id"]]
        a["release_count"] = len(mine)
        a["downloads"] = sum(r["downloads"] for r in mine)
        a["latest"] = {p: next(({"version": r["version"], "published_at": r["published_at"], "assets": r["assets"]}
                                for r in mine if r["platform"] == p and r["latest"]), None) for p in PLATFORMS[:2]}
    return apps


# ---- automatic sync from GitHub Releases ---------------------------------------------------
# Every non-draft release of the app repo is mirrored: version, notes and each .apk/.aab/.ipa/.zip asset URL are
# copied (as gh-proxy + direct GitHub URLs; nothing is ever downloaded),
# notes come from the release body, pre-releases become the "beta" channel.
SYNC_DEFAULTS = {"enabled": True, "interval_min": 10, "app": "", "prereleases": True}
_sync_lock = threading.Lock()
_sync_started = False


def sync_cfg() -> dict:
    c = dict(SYNC_DEFAULTS)
    c.update(db.sget("sync_cfg") or {})
    return c


def set_sync_cfg(new: dict) -> dict:
    c = sync_cfg()
    for k in ("enabled", "prereleases"):
        if k in new:
            c[k] = bool(new[k])
    if "interval_min" in new:
        try:
            c["interval_min"] = max(2, min(1440, int(new["interval_min"])))
        except (TypeError, ValueError):
            raise ValueError("Interval must be a number of minutes")
    if "app" in new:
        a = (new["app"] or "").strip()
        if a and not get_app(a):
            raise ValueError("Unknown app")
        c["app"] = a
    db.sset("sync_cfg", c)
    return c


def _skip_set() -> set:
    return set(db.sget("sync_skip") or [])


def _skip_add(key: str) -> None:
    s = _skip_set()
    s.add(key)
    db.sset("sync_skip", sorted(s))


def forget_skipped() -> int:
    n = len(_skip_set())
    db.sset("sync_skip", [])
    return n


def sync_busy() -> bool:
    return _sync_lock.locked()


def _sync_group(app: dict, rel: dict, version: str, platform: str, assets: list, user: str, st: dict) -> None:
    existing = db.one("SELECT * FROM releases WHERE app_id=? AND version=? AND platform=?", (app["id"], version, platform))
    if existing and existing["status"] == "importing":
        return
    body = rel.get("body") or ""
    created, have = False, set()
    if not existing or existing["status"] == "failed":
        rid = _new_release(app, version, platform, "beta" if rel.get("prerelease") else "stable", body, user, "github",
                           rel["tag_name"], "importing", gh.iso_to_epoch(rel.get("published_at")))
        created = True
    else:
        rid = existing["id"]
        have = {a["filename"] for a in db.query("SELECT filename FROM assets WHERE release_id=?", (rid,))}
        if existing["source"] == "github" and not existing["notes"] and body.strip():
            db.execute("UPDATE releases SET notes=? WHERE id=?", (body.strip()[:20000], rid))
    if have:  # keep already-copied links current (GitHub may have re-uploaded or resized a file)
        pub = gh.iso_to_epoch(rel.get("published_at"))
        if pub:
            db.execute("UPDATE releases SET published_at=? WHERE id=? AND source='github'", (pub, rid))
        for a in assets:
            if safe_filename(a["name"]) in have:
                proxy_url, direct_url = gh.asset_links(a)
                digest = a.get("digest") or ""
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                if direct_url:
                    db.execute("UPDATE assets SET size=?, remote_url=?, direct_url=?, sha256=COALESCE(?,sha256) "
                               "WHERE release_id=? AND filename=? AND remote_url IS NOT NULL",
                               (int(a.get("size", 0)), proxy_url or direct_url, direct_url, sha, rid, safe_filename(a["name"])))
    todo = [a for a in assets if safe_filename(a["name"]) not in have]
    if not todo:
        return
    try:
        for a in todo:
            _attach_release_asset(rid, app, version, platform, a)
            st["new_files"] += 1
        if created:
            st["new_releases"] += 1
        db.execute("UPDATE releases SET status='ready', error=NULL WHERE id=?", (rid,))
    except Exception as e:
        msg = getattr(e, "message", str(e))
        st["errors"].append(f"{rel['tag_name']} ({platform}): {msg}")
        if created:  # a failed row is deleted and retried by the next sync
            db.execute("UPDATE releases SET status='failed', error=? WHERE id=?", (msg[:300], rid))


def sync_github(user: str = "github-sync") -> dict:
    """Mirror all GitHub releases into the library. Safe to call repeatedly: only new releases/files are fetched."""
    if not _sync_lock.acquire(blocking=False):
        return {"busy": True}
    st = {"at": time.time(), "ok": True, "checked": 0, "new_releases": 0, "new_files": 0, "removed": 0, "errors": []}
    try:
        cfg = sync_cfg()
        app = get_app(cfg["app"]) if cfg["app"] else db.one("SELECT * FROM apps ORDER BY id LIMIT 1")
        if not app:
            raise ValueError("No app to sync into")
        st["app"] = app["slug"]
        releases = []
        for page in range(1, 6):
            batch = gh.jget(gh.repo_path("app") + "/releases", params={"per_page": 100, "page": page})
            releases += batch
            if len(batch) < 100:
                break
        skip = _skip_set()
        wanted = set()
        for rel in releases:
            if rel.get("draft") or (rel.get("prerelease") and not cfg["prereleases"]):
                continue
            try:
                version = clean_version(rel["tag_name"])
            except ValueError:
                st["errors"].append(f"{rel['tag_name']}: tag cannot be used as a version")
                continue
            groups: dict = {}
            for a in rel.get("assets", []):
                ext = Path(a["name"]).suffix.lower()
                if ext in ALLOWED_EXT and a.get("state", "uploaded") == "uploaded":
                    groups.setdefault(ALLOWED_EXT[ext], []).append(a)
            for platform in groups:
                wanted.add((version, platform))
            for platform, assets in groups.items():
                st["checked"] += 1
                if f"{app['id']}|{version}|{platform}" in skip:
                    continue
                try:
                    _sync_group(app, rel, version, platform, assets, user, st)
                except ValueError as e:
                    st["errors"].append(f"{rel['tag_name']} ({platform}): {e}")
        # a release deleted/un-published (or whose files were removed) on GitHub disappears here too
        for r in db.query("SELECT id,version,platform FROM releases WHERE app_id=? AND source='github' AND status='ready'", (app["id"],)):
            if (r["version"], r["platform"]) not in wanted:
                delete_release(r["id"])
                st["removed"] += 1
    except Exception as e:
        st["ok"] = False
        st["errors"].append(getattr(e, "message", str(e)))
    finally:
        st["finished"] = time.time()
        st["ok"] = st["ok"] and not st["errors"]
        db.sset("sync_status", st)
        _sync_lock.release()
    return st


def recover_imports() -> None:
    """After a restart, imports that were mid-download can never finish: mark them failed so they are retried."""
    db.execute("UPDATE releases SET status='failed', error='Interrupted by a restart' WHERE status='importing'")


def start_sync() -> None:
    global _sync_started
    if _sync_started:
        return
    _sync_started = True

    def loop():
        time.sleep(5)
        while True:
            try:
                cfg = sync_cfg()
                last = (db.sget("sync_status") or {}).get("at", 0)
                if cfg["enabled"] and setup.is_done() and time.time() - last >= cfg["interval_min"] * 60:
                    sync_github()
            except Exception:
                traceback.print_exc()
            time.sleep(30)
    threading.Thread(target=loop, name="release-sync", daemon=True).start()
