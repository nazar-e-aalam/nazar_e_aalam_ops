"""Database access for the whole project: one SQLAlchemy engine (MySQL / MariaDB), plus a few small helpers.

The connection comes from OPS_DB_HOST / OPS_DB_PORT / OPS_DB_NAME / OPS_DB_USER / OPS_DB_PASSWORD in .env
(scripts/setup.sh fills them in). The database and every table, column and index are created automatically.

Helpers take a model class from `models.py` and SQLAlchemy conditions, and hand back plain dicts:

    db.get(User, User.email == email)                      -> dict | None
    db.all(Audit, order=Audit.id.desc(), limit=50)         -> [dict]
    db.insert(User, email=..., password_hash=...)          -> new primary key
    db.update(User, User.id == uid, disabled=1)            -> rows changed
    db.delete(UserSession, UserSession.user_id == uid)     -> rows deleted
    with db.transaction() as tx:                           # all-or-nothing; tx has the same helpers
        tx.update(...); tx.insert(...)

Nothing here is specific to one database engine: no hand-written SQL, no lastrowid, no INSERT OR IGNORE.
Run ONE uvicorn worker: the deploy queue and login throttling live in memory.
"""
import json
import logging
import threading
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, delete as sa_delete, func, inspect, select, text, update as sa_update
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.schema import CreateColumn, CreateIndex

from ..core import config
from .models import Audit, Base, Setting

log = logging.getLogger("ops.db")

_engine: Engine | None = None
_lock = threading.Lock()


class DatabaseError(RuntimeError):
    """Raised with a message meant for the person running the server."""


# ---- URL & engine ------------------------------------------------------------------------------------------------
def resolve_url(database: str | None = None) -> URL:
    """The MySQL URL built from .env (database=None -> OPS_DB_NAME; pass '' to connect without selecting one)."""
    q = {"charset": "utf8mb4"}
    if config.DB_SOCKET:
        q["unix_socket"] = config.DB_SOCKET
    name = config.DB_NAME if database is None else database
    return URL.create("mysql+pymysql", username=config.DB_USER, password=config.DB_PASSWORD or None,
                      host=None if config.DB_SOCKET else config.DB_HOST, port=None if config.DB_SOCKET else config.DB_PORT,
                      database=name or None, query=q)


def redacted_url(url: URL | None = None) -> str:
    return (url or resolve_url()).render_as_string(hide_password=True)


def _build(url: URL) -> Engine:
    try:
        return create_engine(url, pool_pre_ping=True, echo=config.DB_ECHO, pool_size=config.DB_POOL_SIZE,
                             max_overflow=config.DB_POOL_SIZE, pool_recycle=config.DB_POOL_RECYCLE)
    except ModuleNotFoundError as e:
        raise DatabaseError(f"The MySQL driver is missing ({e.name}). Run: scripts/setup.sh") from e


def engine() -> Engine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _build(resolve_url())
    return _engine


def dialect() -> str:
    return engine().dialect.name


def dispose() -> None:
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None


def ping() -> dict:
    """Used by /health and Settings > Database."""
    t0 = time.time()
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1"))
        return {"ok": True, "ms": int((time.time() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001 - anything means "not healthy"
        return {"ok": False, "ms": None, "error": _clean(e)}


def info() -> dict:
    version = ""
    try:
        with engine().connect() as c:
            version = str(c.execute(text("SELECT VERSION()")).scalar() or "")
    except Exception:  # noqa: BLE001
        pass
    return {"engine": "MariaDB" if "mariadb" in version.lower() else "MySQL", "url": redacted_url(), "version": version.split("\n")[0][:80],
            "source": ".env (OPS_DB_*)", "remote": False,
            "pool": {"size": config.DB_POOL_SIZE, "recycle": config.DB_POOL_RECYCLE}, **ping()}


def _clean(e: Exception) -> str:
    msg = str(getattr(e, "orig", None) or e).split("\n")[0]
    return msg[:240]


# ---- schema ---------------------------------------------------------------------------------------------------
def _create_database() -> None:
    """CREATE DATABASE IF NOT EXISTS (needs the CREATE privilege; if it is missing we simply carry on and let the real error show)."""
    name = config.DB_NAME.replace("`", "")
    eng = _build(resolve_url(""))
    try:
        with eng.begin() as c:
            c.execute(text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    except SQLAlchemyError as e:
        log.debug("could not auto-create database: %s", _clean(e))
    finally:
        eng.dispose()


def init(wait: int = 30) -> None:
    """Connect (waiting for MySQL to come up), create the database if needed, then create missing tables, columns and indexes."""
    deadline, attempt, created = time.time() + wait, 0, False
    while True:
        try:
            with engine().connect() as c:
                c.execute(text("SELECT 1"))
            break
        except OperationalError as e:
            msg = _clean(e)
            if not created and ("1049" in str(e) or "unknown database" in msg.lower()):
                created = True
                dispose()
                _create_database()
                continue
            if "1045" in str(e):          # wrong user/password: waiting will not help
                raise DatabaseError(f"MySQL refused the login for user '{config.DB_USER}': check OPS_DB_USER / OPS_DB_PASSWORD in .env "
                                    "(or just run scripts/setup.sh again).") from e
            attempt += 1
            if time.time() > deadline:
                raise DatabaseError(f"Cannot connect to MySQL at {redacted_url()}: {msg}.  Run scripts/setup.sh to install and create it, "
                                    "or check OPS_DB_* in .env.") from e
            if attempt == 1:
                log.warning("waiting for MySQL (%s)", msg)
            dispose()
            time.sleep(min(5, attempt))
    Base.metadata.create_all(engine())
    _sync_columns()
    _sync_indexes()
    sdel("local_db")      # leftover of the removed per-server external database setting


def _sync_columns() -> None:
    """Upgrade an existing database: add columns that models.py has and the table does not (never drops or alters anything)."""
    insp = inspect(engine())
    with engine().begin() as c:
        for table in Base.metadata.sorted_tables:
            have = {col["name"] for col in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.server_default is None and not col.primary_key:
                    raise DatabaseError(f"Cannot add column {table.name}.{col.name}: NOT NULL without a default")
                ddl = CreateColumn(col).compile(dialect=c.dialect)
                c.exec_driver_sql(f"ALTER TABLE {c.dialect.identifier_preparer.format_table(table)} ADD COLUMN {ddl}")
                log.info("added column %s.%s", table.name, col.name)


def _sync_indexes() -> None:
    insp = inspect(engine())
    with engine().begin() as c:
        for table in Base.metadata.sorted_tables:
            have = {i["name"] for i in insp.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name not in have:
                    try:
                        c.execute(CreateIndex(idx))
                    except SQLAlchemyError as e:          # an index is an optimisation, never a reason not to start
                        log.warning("could not create index %s: %s", idx.name, _clean(e))


# ---- query helpers -----------------------------------------------------------------------------------------------
def _flat(rows) -> list:
    return [dict(r._mapping) for r in rows]


class Tx:
    """The helpers bound to one connection (used by `transaction()`; the module-level functions open one per call)."""

    def __init__(self, conn):
        self.c = conn

    # -- reading
    def select(self, stmt) -> list:
        return _flat(self.c.execute(stmt))

    def first(self, stmt):
        r = self.c.execute(stmt).first()
        return dict(r._mapping) if r else None

    def all(self, model, *where, order=None, limit=None, offset=None, cols=None) -> list:
        stmt = select(*cols) if cols else select(model.__table__)
        return self.select(_shape(stmt, model, where, order, limit, offset))

    def get(self, model, *where, order=None, cols=None):
        stmt = select(*cols) if cols else select(model.__table__)
        return self.first(_shape(stmt, model, where, order, 1, None))

    def count(self, model, *where) -> int:
        return int(self.c.execute(_shape(select(func.count()).select_from(model.__table__), model, where, None, None, None)).scalar() or 0)

    def exists(self, model, *where) -> bool:
        return self.get(model, *where, cols=[list(model.__table__.primary_key.columns)[0]]) is not None

    # -- writing
    def insert(self, model, **values):
        """Insert one row, return its (single-column) primary key."""
        res = self.c.execute(sa_insert(model.__table__).values(**values))
        pk = res.inserted_primary_key
        return pk[0] if pk is not None and len(pk) == 1 else None

    def insert_ignore(self, model, **values) -> bool:
        """Insert unless the key already exists. True = inserted."""
        res = self.c.execute(sa_insert(model.__table__).prefix_with("IGNORE").values(**values))
        return res.rowcount == 1

    def update(self, model, *where, **values) -> int:
        if not values:
            return 0
        stmt = sa_update(model.__table__).values(**values)
        for w in where:
            stmt = stmt.where(w)
        return self.c.execute(stmt).rowcount

    def delete(self, model, *where) -> int:
        stmt = sa_delete(model.__table__)
        for w in where:
            stmt = stmt.where(w)
        return self.c.execute(stmt).rowcount

    def upsert(self, model, key_col, key, **values) -> None:
        """Update the row whose key_col == key, or create it. Safe against two writers racing."""
        if self.update(model, key_col == key, **values) == 0:
            if not self.insert_ignore(model, **{key_col.key: key}, **values):
                self.update(model, key_col == key, **values)


def _shape(stmt, model, where, order, limit, offset):
    for w in where:
        stmt = stmt.where(w)
    if order is not None:
        stmt = stmt.order_by(*(order if isinstance(order, (list, tuple)) else [order]))
    if limit is not None:
        stmt = stmt.limit(int(limit))
    if offset:
        stmt = stmt.offset(int(offset))
    return stmt


@contextmanager
def transaction():
    """All-or-nothing: everything inside commits together, or rolls back if an exception escapes."""
    with engine().begin() as conn:
        yield Tx(conn)


def _one_shot(name):
    def call(*a, **kw):
        with engine().begin() as conn:
            return getattr(Tx(conn), name)(*a, **kw)
    call.__name__, call.__doc__ = name, getattr(Tx, name).__doc__
    return call


select_rows = _one_shot("select")      # any SQLAlchemy select() -> [dict]  (joins, aggregates)
select_one = _one_shot("first")        # any SQLAlchemy select() -> dict | None
all = _one_shot("all")
get = _one_shot("get")
count = _one_shot("count")
exists = _one_shot("exists")
insert = _one_shot("insert")
insert_ignore = _one_shot("insert_ignore")
update = _one_shot("update")
delete = _one_shot("delete")
upsert = _one_shot("upsert")


# ---- settings (JSON values) -----------------------------------------------------------------------------------
def sget(key: str, default=None):
    row = get(Setting, Setting.key == key)
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def sset(key: str, value) -> None:
    upsert(Setting, Setting.key, key, value=json.dumps(value))


def sdel(key: str) -> None:
    delete(Setting, Setting.key == key)


# ---- audit log ------------------------------------------------------------------------------------------------
def audit(user_email, action: str, detail: str = "", ip: str = "") -> None:
    try:
        insert(Audit, ts=time.time(), user_email=(user_email or "system")[:254], action=action[:100], detail=(detail or "")[:2000], ip=(ip or "")[:64])
    except SQLAlchemyError:
        log.exception("could not write audit entry %s", action)
