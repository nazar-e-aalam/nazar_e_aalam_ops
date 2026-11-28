"""Runtime configuration. Everything is read from environment variables
(or a `.env` file next to the project) and every variable is prefixed OPS_
so it can be stripped from the environment of deployed apps."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
VERSION = "1.3.0"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_env_candidates = [Path(os.environ["OPS_ENV_FILE"])] if os.environ.get("OPS_ENV_FILE") else [BASE_DIR / ".env", Path.cwd() / ".env"]
ENV_FILE = next((c for c in _env_candidates if c.is_file()), None)
if ENV_FILE:
    _load_env_file(ENV_FILE)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _get(name)
    return default if v == "" else v.lower() in ("1", "true", "yes", "on")


ROOT_DOMAIN = _get("OPS_ROOT_DOMAIN", "localhost").lower()
OPS_SUBDOMAIN = _get("OPS_SUBDOMAIN", "ops").lower()
PANEL_HOST = f"{OPS_SUBDOMAIN}.{ROOT_DOMAIN}"

# DEV: also serve the ops panel under /ops on any host (handy on localhost)
DEV = _bool("OPS_DEV", ROOT_DOMAIN == "localhost")
HTTPS = _bool("OPS_HTTPS", not DEV)
_scheme = "https" if HTTPS else "http"
PUBLIC_URL = _get("OPS_PUBLIC_URL", ("http://localhost:8100" if DEV and ROOT_DOMAIN == "localhost" else f"{_scheme}://{PANEL_HOST}")).rstrip("/")
# Base address used in emailed links. Only ever taken from the server's own config, never from request headers.
OPS_URL = _get("OPS_URL", "http://localhost:8100/ops" if DEV and ROOT_DOMAIN == "localhost" else f"{_scheme}://{PANEL_HOST}").rstrip("/")

# Uploaded builds, staged files and SSH keys live here. There are NO log files: everything is printed to the terminal.
DATA_DIR = Path(_get("OPS_DATA_DIR", str(BASE_DIR / "data"))).resolve()
APPS_DIR = DATA_DIR / "apps"
TMP_DIR = DATA_DIR / "tmp"
for _d in (DATA_DIR, APPS_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass

# --- database (MySQL / MariaDB) ---------------------------------------------------------------------------------
# scripts/setup.sh installs MySQL/MariaDB, creates the database + user and writes these values into .env for you.
# The database itself is also created on start if the user is allowed to (CREATE DATABASE IF NOT EXISTS).
DB_HOST = _get("OPS_DB_HOST", "127.0.0.1")
DB_PORT = int(_get("OPS_DB_PORT", "3306") or 3306)
DB_NAME = _get("OPS_DB_NAME", "nazar_ops")
DB_USER = _get("OPS_DB_USER", "nazar_ops")
DB_PASSWORD = os.environ.get("OPS_DB_PASSWORD", "")          # not stripped: spaces may be intentional
DB_SOCKET = _get("OPS_DB_SOCKET")                            # optional unix socket, used instead of host/port when set
DB_POOL_SIZE = int(_get("OPS_DB_POOL_SIZE", "10") or 10)
DB_POOL_RECYCLE = int(_get("OPS_DB_POOL_RECYCLE", "1800") or 1800)
DB_ECHO = _bool("OPS_DB_ECHO", False)
LOG_LEVEL = _get("OPS_LOG_LEVEL", "INFO").upper()

ADMIN_EMAIL = _get("OPS_ADMIN_EMAIL", "admin@example.com").lower()
ADMIN_PASSWORD = _get("OPS_ADMIN_PASSWORD")
ADMIN_NAME = _get("OPS_ADMIN_NAME", "Admin")
# true = at every start, set the password of OPS_ADMIN_EMAIL to OPS_ADMIN_PASSWORD (handy if you are locked out)
ADMIN_FORCE = _get("OPS_ADMIN_FORCE_PASSWORD").lower() in ("1", "true", "yes")

GITHUB_TOKEN = _get("OPS_GITHUB_TOKEN")
GITHUB_ORG = _get("OPS_GITHUB_ORG", "nazar-e-aalam")
PANEL_REPO = _get("OPS_PANEL_REPO", "nazar_e_aalam_panel")
APP_REPO = _get("OPS_APP_REPO", "nazar_e_aalam_app")
GITHUB_API_URL = _get("OPS_GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_GIT_URL = _get("OPS_GITHUB_GIT_URL", "https://github.com").rstrip("/")
# Mirror for git clones and release/file downloads (no API token is ever sent to it). Empty = go direct.
GITHUB_PROXY = _get("OPS_GITHUB_PROXY", "https://gh-proxy.org").rstrip("/")
# If the proxy fails, retry direct from GitHub (uses the token, so private repos still work).
GITHUB_PROXY_FALLBACK = _get("OPS_GITHUB_PROXY_FALLBACK", "true").lower() in ("1", "true", "yes")

SESSION_HOURS = int(_get("OPS_SESSION_HOURS", "168"))
MAX_UPLOAD_MB = int(_get("OPS_MAX_UPLOAD_MB", "800"))

# Optional SMTP defaults (the panel can also store them: Settings > Email). Empty host = email disabled.
SMTP = dict(host=_get("OPS_SMTP_HOST"), port=int(_get("OPS_SMTP_PORT", "587") or 587),
            security=_get("OPS_SMTP_SECURITY", "starttls").lower(), username=_get("OPS_SMTP_USER"),
            password=_get("OPS_SMTP_PASSWORD"), from_email=_get("OPS_SMTP_FROM"), from_name=_get("OPS_SMTP_FROM_NAME", "Nazar Ops"))
