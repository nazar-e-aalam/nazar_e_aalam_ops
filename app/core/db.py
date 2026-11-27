"""Tiny SQLite layer (stdlib only). One short-lived connection per call,
WAL mode, so it is safe to use from request threads and worker threads."""
import json
import sqlite3
import time
from contextlib import closing, contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'operator',
  disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, last_seen REAL NOT NULL, ip TEXT, ua TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, user_email TEXT, action TEXT NOT NULL,
  detail TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
CREATE TABLE IF NOT EXISTS tracked_runs(
  repo TEXT NOT NULL, run_id INTEGER NOT NULL, workflow TEXT, ref TEXT, inputs TEXT,
  triggered_by TEXT, triggered_at REAL NOT NULL, PRIMARY KEY(repo, run_id));
CREATE TABLE IF NOT EXISTS deployments(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'deploy', status TEXT NOT NULL,
  sha TEXT, short_sha TEXT, message TEXT, author TEXT, branch TEXT,
  trigger TEXT, triggered_by TEXT, queued_at REAL NOT NULL, started_at REAL, finished_at REAL,
  release_name TEXT, prev_release TEXT, env_version INTEGER, rolled_back INTEGER NOT NULL DEFAULT 0,
  error TEXT, stages TEXT);
CREATE TABLE IF NOT EXISTS targets(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'ssh',
  host TEXT, port INTEGER NOT NULL DEFAULT 22, user TEXT, root TEXT NOT NULL DEFAULT '',
  restart_cmd TEXT NOT NULL DEFAULT '', health_url TEXT NOT NULL DEFAULT '',
  key_mode TEXT NOT NULL DEFAULT 'generated', db_json TEXT NOT NULL DEFAULT '{}',
  checks TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0, checked_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS env_versions(
  id INTEGER PRIMARY KEY, version INTEGER NOT NULL, content TEXT NOT NULL, sha TEXT NOT NULL,
  note TEXT, created_by TEXT, created_at REAL NOT NULL, key_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
  version TEXT NOT NULL, platform TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'stable',
  notes TEXT NOT NULL DEFAULT '', visible INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ready', error TEXT, source TEXT, source_ref TEXT,
  created_by TEXT, created_at REAL NOT NULL, published_at REAL,
  UNIQUE(app_id, version, platform));
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY, release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
  content_type TEXT, downloads INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, email TEXT NOT NULL, role TEXT NOT NULL,
  invited_by TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, revoked INTEGER NOT NULL DEFAULT 0,
  emailed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS password_resets(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS login_challenges(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS recovery_codes(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL, used_at REAL);
CREATE INDEX IF NOT EXISTS recovery_user ON recovery_codes(user_id);
"""

# columns added after 1.0 (applied to existing databases by init())
USER_COLUMNS = {
    "first_name": "TEXT NOT NULL DEFAULT ''", "last_name": "TEXT NOT NULL DEFAULT ''",
    "totp_secret": "TEXT", "totp_pending": "TEXT", "totp_enabled": "INTEGER NOT NULL DEFAULT 0",
    "totp_last_step": "INTEGER NOT NULL DEFAULT 0",
}


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init() -> None:
    new = not config.DB_PATH.exists()
    with closing(conn()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in USER_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        dcols = {r["name"] for r in c.execute("PRAGMA table_info(deployments)")}
        if "target_id" not in dcols:
            c.execute("ALTER TABLE deployments ADD COLUMN target_id INTEGER NOT NULL DEFAULT 0")
        # releases can now be stored as a link (gh-proxy URL) instead of a file on this server
        acols = {r["name"] for r in c.execute("PRAGMA table_info(assets)")}
        if "remote_url" not in acols:
            c.execute("ALTER TABLE assets ADD COLUMN remote_url TEXT")
        if "direct_url" not in acols:
            c.execute("ALTER TABLE assets ADD COLUMN direct_url TEXT")
    if new:
        try:
            config.DB_PATH.chmod(0o600)
        except OSError:
            pass


def query(sql: str, args=()) -> list:
    with closing(conn()) as c:
        return [dict(r) for r in c.execute(sql, args)]


def one(sql: str, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()) -> int:
    with closing(conn()) as c:
        return c.execute(sql, args).lastrowid


def rowcount(sql: str, args=()) -> int:
    with closing(conn()) as c:
        return c.execute(sql, args).rowcount


@contextmanager
def transaction():
    with closing(conn()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


# ---- settings (JSON values) ------------------------------------------------
def sget(key: str, default=None):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def sset(key: str, value) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


def sdel(key: str) -> None:
    execute("DELETE FROM settings WHERE key=?", (key,))


def audit(user_email, action: str, detail: str = "", ip: str = "") -> None:
    execute("INSERT INTO audit(ts,user_email,action,detail,ip) VALUES(?,?,?,?,?)",
            (time.time(), user_email or "system", action, detail[:2000], ip or ""))
