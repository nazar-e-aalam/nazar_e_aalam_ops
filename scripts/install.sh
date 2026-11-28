#!/usr/bin/env bash
# Nazar Ops: one-command server setup (Ubuntu / Debian).
#
#   sudo scripts/install.sh --domain example.com --email you@example.com --ssl
#
# What it does:  installs packages -> creates the service user -> installs the app + virtualenv ->
#                writes .env -> GENERATES the systemd units, sudoers rule and nginx site -> enables and starts
#                everything -> (optional) gets an HTTPS certificate.
# Safe to re-run: existing .env and database are kept; generated config files are rewritten.
#
# Options
#   --domain D          root domain (required)            --email E        email for the HTTPS certificate
#   --ssl               get a Let's Encrypt certificate   --no-nginx       skip nginx
#   --admin-email E     first admin login (default: --email)   --admin-password P  (default: random, printed once)
#   --user U            linux user (default nazar)        --subdomain S    panel subdomain (default ops)
#   --panel-port N      port of your API/admin (8000)     --ops-port N     port of this app (8100)
#   --panel-cmd "CMD"   how to start your API/admin       --service NAME   systemd name of it (nazar-panel)
#   --deploy-root DIR   where deployments live (/srv/nazar_e_aalam_panel)
#   --install-dir DIR   where this app lives (/opt/nazar-ops)
#   (MariaDB/MySQL is installed, started and the database + user are created automatically; credentials go into .env.
#    To use an external MySQL instead, put OPS_DB_HOST/USER/PASSWORD/NAME in $INSTALL_DIR/.env before running.)
#   --render-only DIR   only write the generated files into DIR and print them (touches nothing else)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN=""; EMAIL=""; ADMIN_EMAIL=""; ADMIN_PASSWORD=""; RUN_USER="nazar"; SUB="ops"; PANEL_PORT=8000; OPS_PORT=8100
PANEL_CMD=""; SERVICE="nazar-panel"; DEPLOY_ROOT="/srv/nazar_e_aalam_panel"; INSTALL_DIR="/opt/nazar-ops"
SSL=0; NO_NGINX=0; RENDER_ONLY=""; GENERATED_PW=0

. "$SRC/scripts/_lib.sh"
need() { [ $# -ge 2 ] || die "option $1 needs a value"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) need "$@"; DOMAIN="$2"; shift 2;;
    --email) need "$@"; EMAIL="$2"; shift 2;;
    --admin-email) need "$@"; ADMIN_EMAIL="$2"; shift 2;;
    --admin-password) need "$@"; ADMIN_PASSWORD="$2"; shift 2;;
    --user) need "$@"; RUN_USER="$2"; shift 2;;
    --subdomain) need "$@"; SUB="$2"; shift 2;;
    --panel-port) need "$@"; PANEL_PORT="$2"; shift 2;;
    --ops-port) need "$@"; OPS_PORT="$2"; shift 2;;
    --panel-cmd) need "$@"; PANEL_CMD="$2"; shift 2;;
    --service) need "$@"; SERVICE="$2"; shift 2;;
    --deploy-root) need "$@"; DEPLOY_ROOT="$2"; shift 2;;
    --install-dir) need "$@"; INSTALL_DIR="$2"; shift 2;;
    --render-only) need "$@"; RENDER_ONLY="$2"; shift 2;;
    --ssl) SSL=1; shift;;
    --no-nginx) NO_NGINX=1; shift;;
    -h|--help) sed -n '2,26p' "$0"; exit 0;;
    *) die "unknown option: $1 (try --help)";;
  esac
done

if [ -z "$DOMAIN" ] && [ -t 0 ]; then read -rp "Root domain (e.g. example.com): " DOMAIN; fi
[ -n "$DOMAIN" ] || die "--domain is required"
if [ -z "$RENDER_ONLY" ] && [ "$SSL" = 1 ] && [ -z "$EMAIL" ]; then
  if [ -t 0 ]; then read -rp "Email for the HTTPS certificate: " EMAIL; fi
  [ -n "$EMAIL" ] || die "--ssl needs --email"
fi
ADMIN_EMAIL="${ADMIN_EMAIL:-${EMAIL:-admin@$DOMAIN}}"

if [ -d /etc/nginx/sites-available ]; then NGINX_PATH=/etc/nginx/sites-available/nazar-ops; else NGINX_PATH=/etc/nginx/conf.d/nazar-ops.conf; fi

generate() {  # $1 = output dir
  python3 "$SRC/app/services/serverconf.py" --domain "$DOMAIN" --sub "$SUB" --user "$RUN_USER" --ops-dir "$INSTALL_DIR" \
    --ops-port "$OPS_PORT" --panel-port "$PANEL_PORT" --deploy-root "$DEPLOY_ROOT" --service "$SERVICE" \
    --panel-cmd "$PANEL_CMD" --nginx-path "$NGINX_PATH" --out "$1"
}

# ---------------------------------------------------------------- render-only
if [ -n "$RENDER_ONLY" ]; then
  generate "$RENDER_ONLY"
  while IFS=$'\t' read -r key path mode; do
    printf '\n\033[1;36m### %s   (mode %s)\033[0m\n' "$path" "$mode"; cat "$RENDER_ONLY/$key"
  done < "$RENDER_ONLY/manifest.tsv"
  exit 0
fi

# ---------------------------------------------------------------- real install
[ "$(id -u)" = 0 ] || die "run as root:  sudo $0 $*"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
if [ -z "$PY" ] && command -v apt-get >/dev/null; then
  say "No Python 3.10+ found, installing python3.12"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12 python3.12-venv >/dev/null 2>&1 && PY=python3.12
fi
[ -n "$PY" ] || die "Python 3.10 or newer is required (install python3.12 and re-run)"
ok "using $($PY --version)"

say "Installing system packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  PKGS="python3-venv python3-pip git curl"
  [ "$NO_NGINX" = 1 ] || PKGS="$PKGS nginx"
  [ "$SSL" = 1 ] && PKGS="$PKGS certbot python3-certbot-nginx"
  apt-get update -qq && apt-get install -y -qq $PKGS >/dev/null
  ok "$PKGS"
else
  echo "   (apt-get not found: make sure python3-venv, git$([ "$NO_NGINX" = 1 ] || echo ', nginx') are installed)"
fi

say "Service user '$RUN_USER'"
id "$RUN_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"
ok "user ready"

say "Installing the app into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if [ "$(readlink -f "$SRC")" != "$(readlink -f "$INSTALL_DIR")" ]; then
  (cd "$SRC" && tar --exclude=./data --exclude=./.venv --exclude=./.env --exclude='__pycache__' -cf - .) | tar -xf - -C "$INSTALL_DIR"
fi
chown -R "$RUN_USER": "$INSTALL_DIR"
runuser -u "$RUN_USER" -- bash -c "cd '$INSTALL_DIR' && $PY -m venv .venv && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -r requirements.txt" \
  || die "installing the Python dependencies failed (check the server's internet access, then re-run)"
ok "virtualenv + dependencies"

say "Configuration (.env)"
ENVF="$INSTALL_DIR/.env"
if [ -f "$ENVF" ]; then
  ok "kept existing .env"
else
  cp "$INSTALL_DIR/.env.example" "$ENVF"
  setv() { env_set "$ENVF" "$1" "$2"; }
  setv OPS_ROOT_DOMAIN "$DOMAIN"; setv OPS_SUBDOMAIN "$SUB"; setv OPS_ADMIN_EMAIL "$ADMIN_EMAIL"
  [ -n "$ADMIN_PASSWORD" ] || { ADMIN_PASSWORD="$(rand)"; GENERATED_PW=1; }     # shown once at the end (there is no password file)
  setv OPS_ADMIN_PASSWORD "$ADMIN_PASSWORD"
  # Secure cookies only work over HTTPS, so plain-http installs must turn them off until a certificate exists.
  if [ "$SSL" = 1 ]; then setv OPS_HTTPS true; else setv OPS_HTTPS false; fi
  chmod 600 "$ENVF"; chown "$RUN_USER": "$ENVF"
  ok "wrote $ENVF"
fi
say "Database"
setup_database "$ENVF" || die "database setup failed (see above)"
chmod 600 "$ENVF"; chown "$RUN_USER": "$ENVF"
mkdir -p "$DEPLOY_ROOT"; chown "$RUN_USER": "$DEPLOY_ROOT"

say "Generating systemd, sudoers and nginx configuration"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SRC="$INSTALL_DIR" generate "$TMP"
while IFS=$'\t' read -r key path mode; do
  [ "$NO_NGINX" = 1 ] && [ "$key" = nginx.conf ] && continue
  if [ "$key" = sudoers ]; then
    visudo -cf "$TMP/$key" >/dev/null || die "generated sudoers file failed validation"
  fi
  mkdir -p "$(dirname "$path")"
  install -m "$mode" -o root -g root "$TMP/$key" "$path"
  ok "$path"
done < "$TMP/manifest.tsv"
if [ "$NO_NGINX" != 1 ] && [ -d /etc/nginx/sites-enabled ]; then ln -sf "$NGINX_PATH" /etc/nginx/sites-enabled/nazar-ops; fi

say "Starting services"
systemctl daemon-reload
systemctl enable --now nazar-ops >/dev/null 2>&1
systemctl enable "$SERVICE" >/dev/null 2>&1 || true     # starts at the first deployment, once code exists
ok "nazar-ops running; $SERVICE enabled (starts after the first deployment)"
if [ "$NO_NGINX" != 1 ]; then
  nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx config test failed"; }
  systemctl reload nginx; ok "nginx reloaded"
  if [ "$SSL" = 1 ]; then
    say "HTTPS certificate"
    certbot --nginx -d "$DOMAIN" -d "$SUB.$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect \
      && ok "certificate installed" || echo "   certbot failed: check that DNS A records for $DOMAIN and $SUB.$DOMAIN point to this server, then re-run."
  fi
fi

HEALTH=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  if curl -fsS -H "Host: $SUB.$DOMAIN" "http://127.0.0.1:$OPS_PORT/healthz" >/dev/null 2>&1; then HEALTH=ok; break; fi
done
if [ -z "$HEALTH" ]; then
  echo; printf '\033[1;31mThe panel did not become healthy.\033[0m Last log lines:\n'
  journalctl -u nazar-ops -n 25 --no-pager 2>/dev/null || true
  die "fix the error above (often the database, see OPS_DB_* in $ENVF), then re-run this installer"
fi
ok "panel is healthy (database reachable)"
SCHEME=http; [ "$SSL" = 1 ] && SCHEME=https
echo
echo "------------------------------------------------------------------"
echo " Ops panel : $SCHEME://$SUB.$DOMAIN"
echo " Public    : $SCHEME://$DOMAIN/applications"
echo " Login     : $ADMIN_EMAIL"
if [ "$GENERATED_PW" = 1 ]; then echo " Password  : $ADMIN_PASSWORD   <- shown once, change it in Settings > Account"
else echo " Password  : (the one in $ENVF / the one you passed)"; fi
echo
echo " Next: open the panel > Settings > GitHub, paste a token, then Servers > This server > Grant access."
echo "       Granting sudo starts the automatic setup (packages, Python check, test build) and alerts you if it fails."
echo "       Set up email under Settings > Email to get failure alerts in your inbox."
[ "$SSL" = 1 ] || echo " Note: HTTPS is off. After DNS works run:  sudo certbot --nginx -d $DOMAIN -d $SUB.$DOMAIN"
echo "       then set OPS_HTTPS=true in $ENVF and:  sudo systemctl restart nazar-ops"
echo "------------------------------------------------------------------"
