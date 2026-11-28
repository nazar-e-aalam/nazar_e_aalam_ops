"""Table definitions (SQLAlchemy 2.x declarative). This file is the single source of truth for the schema:
`db.init()` creates missing tables, missing columns and missing indexes from it, on MySQL / MariaDB.

Conventions
  * timestamps are unix seconds stored as DOUBLE (`Ts`) so the same code works on every database
  * on/off flags are small integers (0/1)
  * every NOT NULL column has a server default, so adding a column to an existing database is always possible
"""
from sqlalchemy import (BigInteger, Double, ForeignKey, Index, Integer, MetaData, String, Text, UniqueConstraint, text)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# a text column that can hold more than 64 KB on MySQL (.env files, release notes)
LongText = Text().with_variant(LONGTEXT(), "mysql")
Ts = Double

_NAMING = {"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
           "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING)


def _zero():
    return text("0")


def _empty():
    return text("''")


# ---- people & sessions -------------------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, server_default=_empty())
    first_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default=_empty())
    last_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default=_empty())
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="operator")
    disabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    last_login: Mapped[float | None] = mapped_column(Ts)
    totp_secret: Mapped[str | None] = mapped_column(String(255))
    totp_pending: Mapped[str | None] = mapped_column(String(255))
    totp_enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    totp_last_step: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=_zero())


class UserSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    expires_at: Mapped[float] = mapped_column(Ts, nullable=False, index=True)
    last_seen: Mapped[float] = mapped_column(Ts, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    ua: Mapped[str | None] = mapped_column(String(300))


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    invited_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    expires_at: Mapped[float] = mapped_column(Ts, nullable=False)
    used_at: Mapped[float | None] = mapped_column(Ts)
    revoked: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    emailed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())


class PasswordReset(Base):
    __tablename__ = "password_resets"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    expires_at: Mapped[float] = mapped_column(Ts, nullable=False)
    used_at: Mapped[float | None] = mapped_column(Ts)


class LoginChallenge(Base):
    __tablename__ = "login_challenges"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at: Mapped[float] = mapped_column(Ts, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    used_at: Mapped[float | None] = mapped_column(Ts)
    __table_args__ = (Index("recovery_user", "user_id"),)


# ---- settings, audit, notifications --------------------------------------------------------------------------
class Setting(Base):
    """Key/value store; `value` is JSON."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(LongText, nullable=False)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[float] = mapped_column(Ts, nullable=False)
    user_email: Mapped[str | None] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (Index("audit_ts", "ts"),)


class Notification(Base):
    """In-panel notification feed (deploy failed, server setup failed, ...). Mirrored to email when SMTP is set up."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[float] = mapped_column(Ts, nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, server_default="info")     # info | success | warning | error
    kind: Mapped[str] = mapped_column(String(64), nullable=False, server_default=_empty())    # deploy.failed, bootstrap.failed ...
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=_empty())
    link: Mapped[str] = mapped_column(String(255), nullable=False, server_default=_empty())
    emailed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())


# ---- deploy ---------------------------------------------------------------------------------------------------
class Target(Base):
    """A server we deploy to over SSH (the local server is implicit, id 0)."""
    __tablename__ = "targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ssh")
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default="22")
    user: Mapped[str | None] = mapped_column(String(64))
    root: Mapped[str] = mapped_column(String(512), nullable=False, server_default=_empty())
    restart_cmd: Mapped[str] = mapped_column(Text, nullable=False, server_default=_empty())
    health_url: Mapped[str] = mapped_column(String(1024), nullable=False, server_default=_empty())
    key_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="generated")
    python: Mapped[str] = mapped_column(String(64), nullable=False, server_default=_empty())      # interpreter picked by server set-up
    checks: Mapped[str] = mapped_column(LongText, nullable=False, server_default=_empty())
    verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    checked_at: Mapped[float | None] = mapped_column(Ts)
    created_at: Mapped[float | None] = mapped_column(Ts)


class Deployment(Base):
    """One deployment, rollback or server set-up run (kind = deploy | rollback | bootstrap)."""
    __tablename__ = "deployments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="deploy")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sha: Mapped[str | None] = mapped_column(String(64))
    short_sha: Mapped[str | None] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(200))
    branch: Mapped[str | None] = mapped_column(String(255))
    trigger: Mapped[str | None] = mapped_column(String(32))
    triggered_by: Mapped[str | None] = mapped_column(String(254))
    queued_at: Mapped[float] = mapped_column(Ts, nullable=False)
    started_at: Mapped[float | None] = mapped_column(Ts)
    finished_at: Mapped[float | None] = mapped_column(Ts)
    release_name: Mapped[str | None] = mapped_column(String(128))
    prev_release: Mapped[str | None] = mapped_column(String(128))
    env_version: Mapped[int | None] = mapped_column(Integer)
    rolled_back: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    error: Mapped[str | None] = mapped_column(Text)
    stages: Mapped[str | None] = mapped_column(LongText)
    log: Mapped[str | None] = mapped_column(LongText)          # finished runs keep their terminal output here (no log files)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    __table_args__ = (Index("deployments_status", "status"), Index("deployments_target", "target_id", "id"),
                      Index("deployments_release", "release_name"))


class EnvVersion(Base):
    """Every save of the deployed .env is a new immutable version."""
    __tablename__ = "env_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    content: Mapped[str] = mapped_column(LongText, nullable=False)
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    key_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())


class TrackedRun(Base):
    __tablename__ = "tracked_runs"
    repo: Mapped[str] = mapped_column(String(16), primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    workflow: Mapped[str | None] = mapped_column(String(255))
    ref: Mapped[str | None] = mapped_column(String(255))
    inputs: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(String(254))
    triggered_at: Mapped[float] = mapped_column(Ts, nullable=False)


# ---- public downloads -------------------------------------------------------------------------------------------
class App(Base):
    __tablename__ = "apps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=_empty())
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)


class Release(Base):
    __tablename__ = "releases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, server_default="stable")
    notes: Mapped[str] = mapped_column(LongText, nullable=False, server_default=_empty())
    visible: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")
    error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    published_at: Mapped[float | None] = mapped_column(Ts)
    __table_args__ = (UniqueConstraint("app_id", "version", "platform", name="uq_releases_app_version_platform"),
                      Index("releases_status", "status"))


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    downloads: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    created_at: Mapped[float] = mapped_column(Ts, nullable=False)
    remote_url: Mapped[str | None] = mapped_column(Text)
    direct_url: Mapped[str | None] = mapped_column(Text)


TABLES = {m.__tablename__: m for m in (User, UserSession, Invite, PasswordReset, LoginChallenge, RecoveryCode, Setting, Audit,
                                       Notification, Target, Deployment, EnvVersion, TrackedRun, App, Release, Asset)}
