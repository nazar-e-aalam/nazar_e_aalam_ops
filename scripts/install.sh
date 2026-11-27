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
#   --render-only DIR   only write the generated files into DIR and print them (touches nothing else)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN=""; EMAIL=""; ADMIN_EMAIL=""; ADMIN_PASSWORD=""; RUN_USER="nazar"; SUB="ops"; PANEL_PORT=8000; OPS_PORT=8100
PANEL_CMD=""; SERVICE="nazar-panel"; DEPLOY_ROOT="/srv/nazar_e_aalam_panel"; INSTALL_DIR="/opt/nazar-ops"
SSL=0; NO_NGINX=0; RENDER_ONLY=""

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
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
    -h|--help) sed -n '2,24p' "$0"; exit 0;;
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
  python3 "$SRC/app/serverconf.py" --domain "$DOMAIN" --sub "$SUB" --user "$RUN_USER" --ops-dir "$INSTALL_DIR" \
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
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || die "Python 3.10 or newer is required"

say "Installing system packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  PKGS="python3-venv python3-pip git"
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
runuser -u "$RUN_USER" -- bash -c "cd '$INSTALL_DIR' && python3 -m venv .venv && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -r requirements.txt"
ok "virtualenv + dependencies"

say "Configuration (.env)"
ENVF="$INSTALL_DIR/.env"
if [ -f "$ENVF" ]; then
  ok "kept existing .env"
else
  cp "$INSTALL_DIR/.env.example" "$ENVF"
  setv() {  # set KEY=value in .env (python, so passwords with | & / # are safe)
    python3 - "$ENVF" "$1" "$2" <<'PY'
import sys
path, key, val = sys.argv[1:4]
lines = open(path).read().split("\n")
open(path, "w").write("\n".join((key + "=" + val) if l.startswith(key + "=") else l for l in lines))
PY
  }
  setv OPS_ROOT_DOMAIN "$DOMAIN"; setv OPS_SUBDOMAIN "$SUB"; setv OPS_ADMIN_EMAIL "$ADMIN_EMAIL"
  setv OPS_ADMIN_PASSWORD "$ADMIN_PASSWORD"
  # Secure cookies only work over HTTPS, so plain-http installs must turn them off until a certificate exists.
  if [ "$SSL" = 1 ]; then setv OPS_HTTPS true; else setv OPS_HTTPS false; fi
  chmod 600 "$ENVF"; chown "$RUN_USER": "$ENVF"
  ok "wrote $ENVF"
fi
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

sleep 3
PWF="$INSTALL_DIR/data/initial_admin_password.txt"
SCHEME=http; [ "$SSL" = 1 ] && SCHEME=https
echo
echo "------------------------------------------------------------------"
echo " Ops panel : $SCHEME://$SUB.$DOMAIN"
echo " Public    : $SCHEME://$DOMAIN/applications"
echo " Login     : $ADMIN_EMAIL"
if [ -n "$ADMIN_PASSWORD" ]; then echo " Password  : (the one you passed)"
elif [ -f "$PWF" ]; then echo " Password  : $(sed -n 2p "$PWF")   <- shown once, change it in Settings > Account, then delete $PWF"
else echo " Password  : see $PWF (appears after first start)"; fi
echo
echo " Next: open the panel > Settings > GitHub, paste a token, then Settings > Deploy"
echo "       and set your panel start command (Settings > Server shows what was generated)."
[ "$SSL" = 1 ] || echo " Note: HTTPS is off. After DNS works run:  sudo certbot --nginx -d $DOMAIN -d $SUB.$DOMAIN"
echo "       then set OPS_HTTPS=true in $ENVF and:  sudo systemctl restart nazar-ops"
echo "------------------------------------------------------------------"
