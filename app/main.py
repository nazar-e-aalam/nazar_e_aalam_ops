"""Entry point:  uvicorn app.main:app --host 127.0.0.1 --port 8100 --proxy-headers

One process, two sites chosen by the Host header:
  ops.<ROOT_DOMAIN>   -> ops panel (login required)
  anything else       -> public site under /applications
In OPS_DEV mode the panel is also reachable at /ops on any host (http://localhost:8100/ops/)."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse
from starlette.routing import Host, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.applications import Starlette

from .core import config, db, security
from .services import deploy, library, setup
from .routes.auth import router as auth_router
from .routes.ops import router as ops_router
from .routes.public import router as public_router

WEB = Path(__file__).resolve().parents[1] / "web"

OPS_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src https://fonts.gstatic.com; img-src 'self' data: https://avatars.githubusercontent.com; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
PUBLIC_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
              "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _errors(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request: Request, exc: StarletteHTTPException):
        if request.url.path.endswith("/api") or "/api/" in request.url.path:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 404:
            return HTMLResponse("<!doctype html><title>Not found</title><body style='font:16px system-ui;padding:3rem'>"
                                "<h1>404</h1><p>Page not found.</p>", status_code=404)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def val_exc(request: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
        return JSONResponse({"detail": f"Invalid input{(' (' + loc + ')') if loc else ''}: {first.get('msg', '')}"}, status_code=422)


def _headers(app: FastAPI, csp: str) -> None:
    @app.middleware("http")
    async def sec(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", csp)
        if config.HTTPS:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith(("/api/", "/ops/api/")):
            resp.headers["Cache-Control"] = "no-store"
        return resp


# ---- ops site -------------------------------------------------------------------
ops = FastAPI(title="Nazar Ops", docs_url=None, redoc_url=None, openapi_url=None)
_errors(ops)
_headers(ops, OPS_CSP)
ops.include_router(ops_router)
ops.include_router(auth_router)
ops.mount("/static/ops", NoCacheStatic(directory=WEB / "ops"), name="ops-static")
ops.mount("/static/shared", NoCacheStatic(directory=WEB / "shared"), name="shared-static")


@ops.get("/")
def ops_index():
    html = (WEB / "ops" / "index.html").read_text(encoding="utf-8").replace("{{V}}", config.VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache", "X-Frame-Options": "DENY"})


@ops.get("/favicon.svg")
def ops_icon():
    return FileResponse(WEB / "shared" / "favicon.svg", media_type="image/svg+xml")


# ---- public site ----------------------------------------------------------------
public = FastAPI(title="Nazar Applications", docs_url=None, redoc_url=None, openapi_url=None)
_errors(public)
_headers(public, PUBLIC_CSP)
public.include_router(public_router)
public.mount("/applications/static/shared", NoCacheStatic(directory=WEB / "shared"), name="shared-static")
public.mount("/applications/static/public", NoCacheStatic(directory=WEB / "public"), name="public-static")


@public.get("/favicon.svg")
def public_icon():
    return FileResponse(WEB / "shared" / "favicon.svg", media_type="image/svg+xml")


# ---- wiring ---------------------------------------------------------------------
def startup() -> None:
    print(f"[ops] .env: {config.ENV_FILE or 'NOT FOUND (copy .env.example to .env in ' + str(config.BASE_DIR) + ')'}   database: {config.DB_PATH}")
    db.init()
    had_users = bool(db.one("SELECT id FROM users LIMIT 1"))
    security.seed_admin()
    if had_users and not setup.is_done():  # upgrading an existing install: nothing to set up again
        setup.mark_done("upgrade")
    library.ensure_default_app()
    library.recover_imports()
    deploy.start()
    library.start_sync()
    print(f"[ops] panel host: {config.PANEL_HOST}   public: {config.PUBLIC_URL}/applications   data: {config.DATA_DIR}")
    if config.DEV:
        print("[ops] DEV mode: panel also available at /ops/")


@asynccontextmanager
async def lifespan(_app):
    startup()
    yield


routes = [Host(config.PANEL_HOST, app=ops, name="ops-host")]
if config.DEV:
    routes.append(Route("/ops", lambda r: RedirectResponse("/ops/", status_code=307)))
    routes.append(Mount("/ops", app=ops, name="ops-dev"))
routes.append(Mount("/", app=public, name="public"))
app = Starlette(routes=routes, lifespan=lifespan)
