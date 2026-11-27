"""Public pages served on the root domain under /applications."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..core import config, db
from ..services import github as gh, library

router = APIRouter()
WEB = Path(__file__).resolve().parents[2] / "web"
HEADERS = {"X-Content-Type-Options": "nosniff"}


def _page() -> HTMLResponse:
    html = (WEB / "public" / "index.html").read_text(encoding="utf-8").replace("{{V}}", config.VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache", "X-Frame-Options": "DENY", **HEADERS})


@router.get("/")
def root():
    return RedirectResponse("/applications", status_code=302)


@router.get("/applications")
@router.get("/applications/")
def apps_page():
    return _page()


@router.get("/applications/api/apps")
def api_apps():
    return {"apps": library.public_apps()}


@router.get("/applications/api/apps/{slug}")
def api_app(slug: str):
    app = db.one("SELECT id,slug,name,description FROM apps WHERE slug=?", (slug,))
    if not app:
        raise HTTPException(404, "App not found")
    rels = library.list_releases(app["id"], public=True)
    for r in rels:
        for k in ("created_by", "source", "source_ref", "error", "visible", "status", "app_id"):
            r.pop(k, None)
    return {"app": app, "releases": rels}


@router.get("/applications/download/{asset_id}/{filename}")
def download(asset_id: int, filename: str, request: Request):
    a = db.one("""SELECT a.*, r.visible, r.status FROM assets a JOIN releases r ON r.id=a.release_id WHERE a.id=?""", (asset_id,))
    if not a or not a["visible"] or a["status"] != "ready":
        raise HTTPException(404, "File not found")
    if a.get("remote_url"):  # linked release: count it, then send the visitor to the gh-proxy URL
        db.execute("UPDATE assets SET downloads=downloads+1 WHERE id=?", (asset_id,))
        return RedirectResponse(gh.pick_url(a["remote_url"], a.get("direct_url")), status_code=302, headers={"Cache-Control": "no-store"})
    path = Path(a["path"]).resolve()
    if config.APPS_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "File not found")
    rng = request.headers.get("range")
    if not rng or rng.startswith("bytes=0-"):  # count real downloads, not resumed chunks
        db.execute("UPDATE assets SET downloads=downloads+1 WHERE id=?", (asset_id,))
    return FileResponse(path, filename=a["filename"], media_type=a["content_type"] or "application/octet-stream",
                        headers={**HEADERS, "Cache-Control": "public, max-age=300"})


@router.get("/applications/{slug}/latest/{platform}")
def latest(slug: str, platform: str):
    """Stable link for QR codes / README badges: always the newest stable build."""
    app = db.one("SELECT id FROM apps WHERE slug=?", (slug,))
    if not app or platform not in library.PLATFORMS:
        raise HTTPException(404, "Not found")
    for r in library.list_releases(app["id"], public=True):
        if r["platform"] == platform and r["latest"] and r["assets"]:
            return RedirectResponse(r["assets"][0]["url"], status_code=302)
    raise HTTPException(404, "No release available yet")


@router.get("/applications/{slug}")
def app_page(slug: str):
    if slug in library.RESERVED_SLUGS or not db.one("SELECT id FROM apps WHERE slug=?", (slug,)):
        raise HTTPException(404, "App not found")
    return _page()
