"""Generates the server configuration (systemd units, sudoers rule, nginx site) from a few values.
Pure standard library and no side effects, so it is used by BOTH the installer (scripts/install.sh) and the
panel (Settings > Server), and they can never disagree.

    python3 app/serverconf.py --domain example.com --out /tmp/conf      # writes files + manifest.tsv
"""
import argparse
import json
import re
import sys
from pathlib import Path

_PATH = re.compile(r"^/[A-Za-z0-9_./-]*$")
_NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,60}$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")

OPS_UNIT = """[Unit]
Description=Nazar Ops panel (releases, deployments, public downloads)
After=network.target mysql.service mariadb.service
Wants=mariadb.service

[Service]
User={{USER}}
Group={{USER}}
WorkingDirectory={{OPS_DIR}}
Environment=PYTHONUNBUFFERED=1
ExecStart={{OPS_DIR}}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port {{OPS_PORT}} --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
# Deployments run as child processes: keep them alive if only this panel restarts.
KillMode=process
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

PANEL_UNIT = """[Unit]
Description=Nazar e Aalam API + admin panel (deployed by Nazar Ops)
After=network.target

[Service]
User={{USER}}
Group={{USER}}
# `current` is a symlink that Nazar Ops switches atomically to each new release.
WorkingDirectory={{DEPLOY_ROOT}}/current
ExecStart={{PANEL_CMD}}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

SUDOERS = """# Lets the ops panel restart ONLY the panel service, without a password.
{{USER}} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {{SERVICE}}, /bin/systemctl restart {{SERVICE}}, /usr/bin/systemctl is-active {{SERVICE}}, /bin/systemctl is-active {{SERVICE}}
"""

NGINX = """# Nazar Ops: ops panel on {{OPS_HOST}}, public downloads on {{DOMAIN}}/applications, your API/admin on {{DOMAIN}}
# HTTPS: run  sudo certbot --nginx -d {{DOMAIN}} -d {{OPS_HOST}}  (certbot edits this file for you).

server {
    listen 80;
    listen [::]:80;
    server_name {{OPS_HOST}};

    client_max_body_size {{MAX_UPLOAD}}m;      # app build uploads (APK / IPA)
    proxy_read_timeout 300s;
    proxy_request_buffering off;               # stream big uploads straight to the app

    location / {
        proxy_pass http://127.0.0.1:{{OPS_PORT}};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name {{DOMAIN}};

    client_max_body_size 50m;

    # public downloads (served by Nazar Ops)
    location /applications {
        proxy_pass http://127.0.0.1:{{OPS_PORT}};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;                   # big downloads do not fill nginx temp files
    }
    location = /favicon.svg {
        proxy_pass http://127.0.0.1:{{OPS_PORT}};
        proxy_set_header Host $host;
    }

    # everything else: your nazar_e_aalam_panel (API + admin panel)
    location / {
        proxy_pass http://127.0.0.1:{{PANEL_PORT}};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
"""


def _fill(tpl: str, vals: dict) -> str:
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


def render(domain: str, sub: str = "ops", user: str = "nazar", ops_dir: str = "/opt/nazar-ops", ops_port: int = 8100,
           panel_port: int = 8000, deploy_root: str = "/srv/nazar_e_aalam_panel", service: str = "nazar-panel",
           panel_cmd: str = "", max_upload_mb: int = 800, nginx_path: str = "/etc/nginx/sites-available/nazar-ops") -> dict:
    """Returns {"files": [{key,name,path,mode,description,content}], "commands": [str]}. Raises ValueError on bad input."""
    domain, sub = (domain or "").strip().lower(), (sub or "").strip().lower()
    if not _HOST.match(domain) or "." not in domain and domain != "localhost":
        raise ValueError("Enter a domain like example.com")
    if not re.match(r"^[a-z0-9-]{1,40}$", sub):
        raise ValueError("Subdomain may contain letters, digits and dashes only")
    if not _USER.match(user):
        raise ValueError("Invalid Linux user name")
    if not _NAME.match(service):
        raise ValueError("Invalid service name")
    for label, p in (("Install folder", ops_dir), ("Deploy root", deploy_root), ("nginx path", nginx_path)):
        if not _PATH.match(p) or ".." in p:
            raise ValueError(f"{label} must be an absolute path without spaces")
    for label, n in (("Ops port", ops_port), ("Panel port", panel_port)):
        if not isinstance(n, int) or not 1 <= n <= 65535:
            raise ValueError(f"{label} must be between 1 and 65535")
    if ops_port == panel_port:
        raise ValueError("The ops port and the panel port must differ")
    ops_dir, deploy_root = ops_dir.rstrip("/"), deploy_root.rstrip("/")
    panel_cmd = (panel_cmd or "").strip() or (
        f"{deploy_root}/current/.venv/bin/uvicorn main:app --host 127.0.0.1 --port {panel_port} --proxy-headers")
    if "\n" in panel_cmd or "\r" in panel_cmd or len(panel_cmd) > 400:
        raise ValueError("The panel start command must be a single line")
    v = dict(DOMAIN=domain, OPS_HOST=f"{sub}.{domain}", USER=user, OPS_DIR=ops_dir, OPS_PORT=ops_port, PANEL_PORT=panel_port,
             DEPLOY_ROOT=deploy_root, SERVICE=service, PANEL_CMD=panel_cmd, MAX_UPLOAD=max(10, int(max_upload_mb) + 100))
    files = [
        dict(key="nazar-ops.service", name="nazar-ops.service", path="/etc/systemd/system/nazar-ops.service", mode="644",
             description="The ops panel itself (this app)", content=_fill(OPS_UNIT, v)),
        dict(key="panel.service", name=f"{service}.service", path=f"/etc/systemd/system/{service}.service", mode="644",
             description="Your API + admin panel, deployed into the `current` release folder", content=_fill(PANEL_UNIT, v)),
        dict(key="sudoers", name="nazar-ops (sudoers)", path="/etc/sudoers.d/nazar-ops", mode="440",
             description="Allows the panel to restart only that one service", content=_fill(SUDOERS, v)),
        dict(key="nginx.conf", name="nginx site", path=nginx_path, mode="644",
             description="Routes ops.<domain>, <domain>/applications and <domain>", content=_fill(NGINX, v)),
    ]
    cmds = [f"sudo useradd --system --create-home --shell /usr/sbin/nologin {user}   # once",
            f"sudo mkdir -p {deploy_root} && sudo chown {user}: {deploy_root}",
            "sudo systemctl daemon-reload",
            f"sudo systemctl enable --now nazar-ops && sudo systemctl enable {service}",
            "sudo nginx -t && sudo systemctl reload nginx",
            f"sudo certbot --nginx -d {domain} -d {v['OPS_HOST']}   # HTTPS"]
    return {"files": files, "commands": cmds, "ops_host": v["OPS_HOST"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--sub", default="ops")
    ap.add_argument("--user", default="nazar")
    ap.add_argument("--ops-dir", default="/opt/nazar-ops")
    ap.add_argument("--ops-port", type=int, default=8100)
    ap.add_argument("--panel-port", type=int, default=8000)
    ap.add_argument("--deploy-root", default="/srv/nazar_e_aalam_panel")
    ap.add_argument("--service", default="nazar-panel")
    ap.add_argument("--panel-cmd", default="")
    ap.add_argument("--max-upload-mb", type=int, default=800)
    ap.add_argument("--nginx-path", default="/etc/nginx/sites-available/nazar-ops")
    ap.add_argument("--out", required=True, help="directory to write the files and manifest.tsv into")
    a = ap.parse_args()
    try:
        r = render(a.domain, a.sub, a.user, a.ops_dir, a.ops_port, a.panel_port, a.deploy_root, a.service, a.panel_cmd,
                   a.max_upload_mb, a.nginx_path)
    except ValueError as e:
        print("error: " + str(e), file=sys.stderr)
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "manifest.tsv", "w") as m:
        for f in r["files"]:
            (out / f["key"]).write_text(f["content"])
            m.write(f"{f['key']}\t{f['path']}\t{f['mode']}\n")
    json.dump({"ops_host": r["ops_host"]}, open(out / "info.json", "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
