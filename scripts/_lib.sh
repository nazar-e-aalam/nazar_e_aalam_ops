#!/usr/bin/env bash
# Shared helpers for setup.sh / dev.sh / install.sh (sourced, not run).

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'; else B=; G=; Y=; R=; D=; N=; fi
say()  { printf '%s==>%s %s\n' "$B$Y" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

SUDO=""; [ "$(id -u)" = 0 ] || { command -v sudo >/dev/null 2>&1 && SUDO="sudo"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---- .env helpers (python, so passwords with | & / # ' are safe) -------------------------------------------------
env_get() {  # env_get FILE KEY
  python3 - "$1" "$2" <<'PY'
import sys
for l in open(sys.argv[1], encoding="utf-8"):
    l = l.strip()
    if l.startswith(sys.argv[2] + "="):
        v = l.split("=", 1)[1].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
        print(v); break
PY
}
env_set() {  # env_set FILE KEY VALUE   (replaces the line, or appends it)
  python3 - "$1" "$2" "$3" <<'PY'
import sys
path, key, val = sys.argv[1:4]
fmt = val if all(c.isalnum() or c in "_@%+=:,./-" for c in val) else "'" + val.replace("'", "") + "'"
lines = open(path, encoding="utf-8").read().split("\n")
hit = False
for i, l in enumerate(lines):
    if l.startswith(key + "="):
        lines[i] = f"{key}={fmt}"; hit = True
if not hit:
    while lines and lines[-1] == "": lines.pop()
    lines.append(f"{key}={fmt}")
open(path, "w", encoding="utf-8").write("\n".join(lines).rstrip("\n") + "\n")
PY
}
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(18).replace("-","x").replace("_","y"))'; }

# ---- Python -----------------------------------------------------------------------------------------------------------
find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && { echo "$c"; return 0; }
  done
  return 1
}

pkg_install() {  # pkg_install apt-name [dnf-name] [pacman-name] [apk-name] [brew-name]
  if   have apt-get; then $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$1" >/dev/null
  elif have dnf;     then $SUDO dnf install -y -q "${2:-$1}" >/dev/null
  elif have yum;     then $SUDO yum install -y -q "${2:-$1}" >/dev/null
  elif have pacman;  then $SUDO pacman -S --noconfirm --needed "${3:-$1}" >/dev/null
  elif have apk;     then $SUDO apk add --no-cache "${4:-$1}" >/dev/null
  elif have brew;    then brew install "${5:-$1}" >/dev/null
  else return 1; fi
}

# ---- MySQL / MariaDB --------------------------------------------------------------------------------------------------
db_port_open() { (exec 3<>"/dev/tcp/${1:-127.0.0.1}/${2:-3306}") 2>/dev/null; }

mysql_installed() { have mysqld || have mariadbd || have mysqld_safe || [ -x /usr/sbin/mysqld ] || [ -x /usr/sbin/mariadbd ] || [ -x /usr/local/mysql/bin/mysqld ]; }

mysql_install() {
  mysql_installed && { ok "MySQL/MariaDB server already installed"; return 0; }
  say "Installing MariaDB server (MySQL compatible)"
  have apt-get && $SUDO apt-get update -qq >/dev/null 2>&1 || true
  pkg_install mariadb-server mariadb-server mariadb mariadb mariadb || die "could not install a MySQL/MariaDB server automatically. Install one yourself, then run this again."
  have pacman && [ ! -d /var/lib/mysql/mysql ] && $SUDO mariadb-install-db --user=mysql --basedir=/usr --datadir=/var/lib/mysql >/dev/null 2>&1 || true
  ok "installed"
}

mysql_start() {
  db_port_open && return 0
  if have brew && ! have systemctl; then brew services start mariadb >/dev/null 2>&1 || brew services start mysql >/dev/null 2>&1 || true
  elif have systemctl && [ -d /run/systemd/system ]; then
    for u in mariadb mysql mysqld; do $SUDO systemctl enable --now "$u" >/dev/null 2>&1 && break; done
  elif have service; then $SUDO service mariadb start >/dev/null 2>&1 || $SUDO service mysql start >/dev/null 2>&1 || true
  fi
  if ! db_port_open; then                         # containers / no init system: run the server ourselves
    local d; d="$(command -v mariadbd-safe || command -v mysqld_safe || true)"
    if [ -n "$d" ]; then $SUDO mkdir -p /run/mysqld && $SUDO chown mysql: /run/mysqld 2>/dev/null || true; ($SUDO nohup "$d" >/dev/null 2>&1 &); fi
  fi
  for _ in $(seq 1 40); do db_port_open && return 0; sleep 1; done
  return 1
}

mysql_admin() {  # run the SQL on stdin as the database administrator (root over the unix socket, as is standard on Debian/Ubuntu/MariaDB)
  local cli sql; cli="$(command -v mariadb || command -v mysql || true)"; [ -n "$cli" ] || return 1
  sql="$(cat)"
  if   printf 'SELECT 1;' | $SUDO "$cli" -uroot >/dev/null 2>&1; then printf '%s' "$sql" | $SUDO "$cli" -uroot
  elif [ -n "${OPS_DB_ROOT_PASSWORD:-}" ] && printf 'SELECT 1;' | "$cli" -uroot -p"$OPS_DB_ROOT_PASSWORD" -h127.0.0.1 >/dev/null 2>&1; then printf '%s' "$sql" | "$cli" -uroot -p"$OPS_DB_ROOT_PASSWORD" -h127.0.0.1
  elif printf 'SELECT 1;' | "$cli" -uroot -h127.0.0.1 >/dev/null 2>&1; then printf '%s' "$sql" | "$cli" -uroot -h127.0.0.1
  else return 1; fi
}

mysql_provision() {  # mysql_provision DB USER PASSWORD
  local db="$1" u="$2" pw="${3//\'/}"
  mysql_admin <<SQL || return 1
CREATE DATABASE IF NOT EXISTS \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$u'@'localhost' IDENTIFIED BY '$pw';
CREATE USER IF NOT EXISTS '$u'@'127.0.0.1' IDENTIFIED BY '$pw';
ALTER USER '$u'@'localhost' IDENTIFIED BY '$pw';
ALTER USER '$u'@'127.0.0.1' IDENTIFIED BY '$pw';
GRANT ALL PRIVILEGES ON \`$db\`.* TO '$u'@'localhost';
GRANT ALL PRIVILEGES ON \`$db\`.* TO '$u'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
}

# One call: install -> start -> create database + user -> write OPS_DB_* into the .env file ($1)
setup_database() {
  local envf="$1" pw
  [ -n "$(env_get "$envf" OPS_DB_HOST)" ] || env_set "$envf" OPS_DB_HOST 127.0.0.1
  local host; host="$(env_get "$envf" OPS_DB_HOST)"
  case "$host" in 127.0.0.1|localhost|"") ;; *) ok "using the external MySQL at $host (OPS_DB_HOST), nothing to install"; return 0;; esac
  mysql_install
  say "Starting MySQL"
  mysql_start || die "MySQL did not start. Check:  sudo systemctl status mariadb"
  ok "MySQL is running"
  pw="$(env_get "$envf" OPS_DB_PASSWORD)"; [ -n "$pw" ] || pw="$(rand)"
  say "Creating database and user"
  if mysql_provision "$(env_get "$envf" OPS_DB_NAME)" "$(env_get "$envf" OPS_DB_USER)" "$pw"; then
    env_set "$envf" OPS_DB_PASSWORD "$pw"
    ok "database '$(env_get "$envf" OPS_DB_NAME)' and user '$(env_get "$envf" OPS_DB_USER)' are ready"
  else
    warn "could not log in to MySQL as root automatically."
    warn "Create the database yourself, then put the password in .env (OPS_DB_PASSWORD):"
    warn "  sudo mysql -e \"CREATE DATABASE nazar_ops; CREATE USER 'nazar_ops'@'127.0.0.1' IDENTIFIED BY 'PASSWORD'; GRANT ALL ON nazar_ops.* TO 'nazar_ops'@'127.0.0.1';\""
    return 1
  fi
}
