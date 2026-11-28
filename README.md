# Nazar Ops

One small FastAPI service that gives you:

| Where | What |
|---|---|
| `ops.<your-domain>` | **Ops panel** (login): run GitHub workflows with inputs, watch the whole workflow as a live "roots" graph, deploy the panel repo per commit, manage the admin panel's `.env`, publish app builds, users, audit log |
| `ops.<your-domain>/applications` | **Public downloads**: every app and every version, GitHub-releases style, no login |

Plain HTML/CSS/JS on the front, FastAPI + SQLAlchemy on the back with MySQL / MariaDB (installed and created for you). No build step, no Node. All logs go to the terminal (or `journalctl`), never to files.

---

## 1. Install (Ubuntu/Debian server)

Point two DNS `A` records (`example.com` and `ops.example.com`) at the server, upload and unzip this project, then run **one command**:

```bash
unzip nazar-ops.zip && cd nazar-ops
sudo scripts/install.sh --domain example.com --email you@example.com --ssl
```

The installer does everything, and is safe to re-run:

1. installs `python3-venv`, `git`, `nginx`, **MariaDB** (and `certbot` with `--ssl`), starts MariaDB and **creates the database + user** with a random password
2. creates a `nazar` system user and installs the app into `/opt/nazar-ops` with its own virtualenv
3. writes `.env` (domain, seeded admin login, `OPS_HTTPS`)
4. **generates and installs** the systemd services (`nazar-ops`, `nazar-panel`), the sudoers rule that lets the panel restart only that one service, and the nginx site (ops panel on `ops.example.com`, downloads on `ops.example.com/applications`, your API/admin on `example.com`)
5. enables and starts the services, reloads nginx, and gets the HTTPS certificate
6. prints the URLs and the generated admin password (once)

Useful options: `--admin-email`, `--admin-password`, `--panel-port 8000`, `--panel-cmd "<how your API starts>"`, `--service nazar-panel`, `--deploy-root /srv/nazar_e_aalam_panel`, `--no-nginx`. Run `scripts/install.sh --help` for all of them, or `--render-only DIR` to only write the generated files into `DIR` and print them.

**See or regenerate the configs any time** in the panel: *Settings > Server*. It shows every generated file with its path, a Copy button and the manual commands, built from the same generator the installer uses (`app/services/serverconf.py`).

After the installer:
1. Open `https://ops.example.com` and sign in. **Change the password** under Settings > Account.
2. **Settings > GitHub**: paste a token and press *Save and verify* (below).
3. **Settings > Deploy**: set your restart command (`sudo systemctl restart nazar-panel`), health URL and branch. Check that the **API / admin start command** in *Settings > Server* matches how your FastAPI app starts, and re-install `nazar-panel.service` if you change it.

Without HTTPS yet (`--ssl` omitted) the installer sets `OPS_HTTPS=false` so login works over plain http. After DNS works run `sudo certbot --nginx -d example.com -d ops.example.com`, set `OPS_HTTPS=true` in `/opt/nazar-ops/.env`, and `sudo systemctl restart nazar-ops`.

Try it locally first: **`./run.sh`** (first run installs the libraries and MySQL, creates the database and `.env`, then starts with auto-reload; the first-login password is printed in the terminal). Open `http://localhost:8100/ops/` and `http://localhost:8100/applications`.

### GitHub token

Create a **fine-grained personal access token** (or a bot user's) with access to both repositories:

| Permission | Level | Why |
|---|---|---|
| Contents | Read | fetch commits of the panel repo, read workflow files, releases |
| Actions | Read and write | list runs, run/cancel/re-run workflows, download artifacts |
| Metadata | Read | required by GitHub |

*Settings > GitHub* runs a live check (token valid, who it belongs to, expiry, each repo reachable, Actions readable, write access, API rate limit) and shows ✓ / ✗ with a reason for each.

---

## 1b. Database (MySQL / MariaDB)

Everything the panel stores (users, sessions, settings, deployments and their output, releases, audit log) lives in one MySQL / MariaDB database. There is no `DATABASE_URL`: the connection is these lines in `.env`, which `scripts/setup.sh` (local) and `scripts/install.sh` (server) fill in after installing MariaDB and creating the database and user for you:

```
OPS_DB_HOST=127.0.0.1
OPS_DB_PORT=3306
OPS_DB_NAME=nazar_ops
OPS_DB_USER=nazar_ops
OPS_DB_PASSWORD=<random, generated>
```

* The database, tables, missing columns and indexes are created automatically on start (`app/db/models.py` is the schema, `app/db/db.py` the engine). If MySQL is still starting, the panel waits up to 30 s.
* **External / managed MySQL:** put its host, user, password and name in `.env` (`OPS_DB_HOST` not `127.0.0.1`) and run `scripts/setup.sh --no-db`. Nothing is installed locally.
* Per-server "external database" settings were removed from deployments: your deployed app keeps its own `DB_*` variables in the **Environment** tab like any other variable.

## 1c. Automatic server setup and alerts

Granting sudo access (*Servers > guide > Access check > Grant access*) immediately starts **server setup**, with a live log:
access check → system packages (apt/dnf/yum: python3, venv, pip, git, curl, build tools) → Python check (>= 3.10 with venv; installs 3.12 when needed) → deploy folders → clone the panel repo → virtualenv + dependencies → **your test command** → cleanup. Nothing touches the running version. The sudo password is used once, kept in memory only and never logged; with root or passwordless sudo, use *Set up this server*.

If setup, a deployment or its tests fail, the panel adds a notification (bell icon) and emails all admins with the failing stage and the last output lines (*Settings > Notifications*: extra recipients, "also on success"). Email templates (invite, password reset, alerts) are HTML with a plain-text part and dark-mode support, in `app/templates/email/`; preview them under *Settings > Notifications*.

## 1d. Logs

Nothing is written to log files. The panel and every deployment / server-setup run print to the terminal as they happen (`[run #12] ...`):
locally in the window running `./run.sh`, on a server with `journalctl -u nazar-ops -f`. The live log view in the panel shows the same output, and finished runs keep it in the database. Set `OPS_LOG_LEVEL=DEBUG` for more detail.

---

## 2. How each part works

### Workflows and runs
* **Workflows** lists every workflow in the app repo (and the panel repo). Inputs of `workflow_dispatch` are read from the YAML and turned into a form (text, number, boolean, dropdown, required/default). Your `release.yml` with its two inputs just works. Add `production_release.yml` later and it appears automatically.
* **Run detail** draws the run as the roots graph, **keyed by your job IDs** from the YAML. `needs:` becomes the tree (a job with several `needs` hangs under the deepest one and gets dotted links to the others). Jobs expand into steps; matrix jobs expand into one node per leg. Click a node for status and its log. Running runs refresh every 3 seconds.
* **Who ran it**: runs started from the panel are recorded with the signed-in user ("Started in panel by …") next to GitHub's own actor, and every action is in the audit log.
* Cancel, re-run, re-run failed jobs, download artifacts, and **Publish** an artifact straight to the public page.

### Deployments (panel repo)
Per commit, in a private folder `…/releases/<id>-<sha>/`:

1. fetch exactly that commit (shallow, token never printed) → 2. write the current **.env** (mode 600) → 3. virtualenv (cached by `requirements.txt` hash) → 4. install → 5. **tests** → 6. optional pre-switch command (e.g. migrations) → 7. atomic switch of the `current` symlink → 8. restart → 9. **health check** → 10. prune old releases.

* A failing step **before** the switch leaves the live version untouched.
* If the restart or health check fails **after** the switch, the previous release is restored automatically.
* One deployment at a time, queued. Live log and per-stage timeline. One-click **Roll back** to any release still on disk.
* Triggers: **Deploy now** button, **auto-deploy by polling** (works with zero setup), and an optional **GitHub webhook** for instant deploys (Settings > Webhook).

Server prerequisites (the installer already did these): the deploy folder, `nazar-panel.service`, and the sudoers rule. Details are in *Settings > Server*. Not using the installer? Copy the generated files from that page and run its "Manual steps".

Then *Settings > Deploy*: set the branch, health URL, restart command (`sudo systemctl restart nazar-panel`), and test command. Notes:
* If `/srv/nazar_e_aalam_panel/current` already exists as a normal folder, move it away first; it becomes a symlink.
* Tests run inside the virtualenv, so list your test tool (e.g. `pytest`) in the repo's `requirements.txt`. "No tests collected" from pytest counts as a pass. You can switch tests off per setup.
* The service runs from `current/`, with the shared virtualenv linked at `current/.venv`.

### Environment (.env)
*Environment* stores the admin panel's `.env` as **versions**. Upload a file (or paste), see masked values, diff versions, restore an old one. Every new deployment writes the version that is active at that moment to the path you choose (default `.env`) and records which version it used. Revealing values or downloading the file is admin-only and goes in the audit log. (Stored in the panel's SQLite database under `data/`, protected by file permissions. Back it up like any secret.)

### Releases and the public page
* **Automatic GitHub sync**: every release of the app repo (all versions, not just the latest) is mirrored to your server and appears on `/applications` by itself, like GitHub's own releases page. Each `.apk/.aab/.ipa/.zip` asset is downloaded and served from your domain (so it works for private repos too), the release notes come from the release body, pre-releases become the *beta* channel, drafts are skipped. It runs every 10 minutes (changeable) and on the **Sync now** button on the Releases page. Only new releases and new files are fetched, failed downloads retry on the next sync, and a release you delete on purpose is not re-imported (use *Allow them again* to undo). Edited notes and "Hide" are never overwritten by a sync.
* You can still **Upload build**, **Import one release**, or **Publish** an artifact from a run. Each release has a version, platform (android / ios / other), channel (stable / beta), notes, and can be hidden or deleted.
* The public site shows, per app: the **latest stable** Android and iOS download (iOS says "Coming soon" until you publish one), all versions with notes, file sizes, SHA-256 and download counts, and a "show beta builds" switch.
* Stable permalinks for QR codes / README badges: `/applications/<app-slug>/latest/android` redirects to the newest stable APK.
* Add more apps under *Releases > Add another app*; each gets `/applications/<slug>`. With a single app, `/applications` opens it directly.

### Users and roles
* **Admin**: everything (settings, .env, users, delete releases)
* **Operator**: run workflows, deploy, roll back, publish releases
* **Viewer**: read-only

### Invitations, email, forgot password, two-step verification
All of this is optional and **works without email**: with no SMTP set up (the normal dev situation) *Invite by email* simply shows a one-time link to copy and send yourself, and "Forgot password?" is hidden.

* **Email (Settings > Email)**: SMTP host, port, STARTTLS / SSL / none, login, From address. The password is stored on the server and never shown again. *Send test* checks it. Or set `OPS_SMTP_*` in `.env` (see below). For local development a catch-all inbox such as Mailpit (port 1025, security "None") is handy.
* **Invite (Settings > Users > Invite by email)**: pick an email and a role. The person gets a link (valid 7 days, works once) that opens the panel on a *Welcome* screen where they enter **first name, last name, password and confirm password**, and are signed in. Pending invites can be resent (a new link; the old one dies) or revoked. Without SMTP you get the link on screen instead.
* **Forgot password**: link on the sign-in page (only when email is set up). The reply is identical whether or not the address has an account; the emailed link works once for 60 minutes, signs out every session and still asks for the 2FA code afterwards. Admins can also send or copy a reset link per user.
* **Two-step verification (Settings > Account)**: authenticator app (any TOTP app) via QR code or key, plus 10 one-time recovery codes (stored hashed). Sign-in then asks for the code; the same code cannot be used twice. **Settings > Users > Require two-step verification** forces everyone to set it up before they can use the panel (enforced on the server, not just in the page). Admins can reset a user's 2FA.
* Links in emails always use the address from `OPS_URL` / your domain, never the request's Host header, so a forged Host header cannot redirect people to another site.
* **Locked out?** On the server: `cd /opt/nazar-ops && sudo -u nazar .venv/bin/python -m app.manage reset-password you@example.com` (also `disable-2fa`, `create-admin`, `list-users`).

Passwords are scrypt-hashed, sessions are HttpOnly cookies (SameSite=Lax, Secure over HTTPS), state-changing requests need a CSRF header, logins are rate-limited, "sign out other sessions" is in Settings > Account.

---

## 3. Configuration reference (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `OPS_ROOT_DOMAIN` | `localhost` | your API/admin domain; public site on the Ops subdomain |
| `OPS_SUBDOMAIN` | `ops` | panel host is `<sub>.<root>` |
| `OPS_HTTPS` | `false` locally, `true` on a domain | marks cookies Secure and sends HSTS; `false` only locally |
| `OPS_ADMIN_EMAIL` / `OPS_ADMIN_PASSWORD` / `OPS_ADMIN_NAME` | | seeded admin (only when the DB is empty) |
| `OPS_GITHUB_TOKEN` | | fallback token (a token saved in the panel wins) |
| `OPS_GITHUB_ORG` / `OPS_APP_REPO` / `OPS_PANEL_REPO` | `nazar-e-aalam` / `nazar_e_aalam_app` / `nazar_e_aalam_panel` | |
| `OPS_DB_HOST` / `OPS_DB_PORT` / `OPS_DB_NAME` / `OPS_DB_USER` / `OPS_DB_PASSWORD` | `127.0.0.1` / `3306` / `nazar_ops` / `nazar_ops` / generated | MySQL connection (see 1b); optional `OPS_DB_SOCKET` |
| `OPS_DATA_DIR` | `./data` | uploaded builds and staging (no logs, no database file) |
| `OPS_LOG_LEVEL` | `INFO` | terminal log level |
| `OPS_MAX_UPLOAD_MB` | `800` | per file limit (also raise nginx `client_max_body_size`) |
| `OPS_SESSION_HOURS` | `168` | login lifetime |
| `OPS_DEV` | auto (`true` on localhost) | also serve the panel at `/ops` on any host, for local testing |
| `OPS_PUBLIC_URL` / `OPS_URL` | derived from domain (`http://localhost:8100/ops` in dev) | public / panel base URLs used in the UI **and in emailed links** |
| `OPS_SMTP_HOST` / `OPS_SMTP_PORT` | empty / `587` | optional email server (empty host = email off); can also be set in the panel |
| `OPS_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` |
| `OPS_SMTP_USER` / `OPS_SMTP_PASSWORD` | | SMTP login (leave empty for none) |
| `OPS_SMTP_FROM` / `OPS_SMTP_FROM_NAME` | | sender address and name |
| `OPS_ENV_FILE` | `./.env` | alternative path of this config file |

All deploy settings (branch, commands, health URL, retention, polling) are edited in the panel, not here.

**Back up** the MySQL database (`mysqldump nazar_ops`) and `data/` (uploaded builds). Everything else can be re-created from this folder and GitHub.

---

## 4. Project layout

```
```
app/
  main.py            two sites by Host header (ops.<domain> vs root); DEV also serves /ops
  manage.py          recovery CLI: reset-password, disable-2fa, create-admin, list-users
  core/              foundations
    config.py          env vars (incl. gh-proxy settings)
    logs.py            terminal logging + banner (no files)
    security.py        password hashing, sessions, login throttle, admin seed
    envstore.py        versioned .env
    totp.py            TOTP (RFC 6238) + recovery codes
    qr.py              tiny QR code generator (SVG) for the 2FA set-up
    mailer.py          SMTP sender + email templates
  services/          business logic
    github.py          GitHub client + gh-proxy links/downloads, ETag cache, token check, dispatch
    library.py         apps, releases (stored as version + gh-proxy URL), uploads, automatic release sync
    deploy.py          deployment engine, queue, polling, webhook (git fetch via gh-proxy), server status
    workflows.py       YAML parsing, inputs validation, run -> graph tree
    serverconf.py      generates systemd units, sudoers rule and nginx site (installer + Settings > Server)
  routes/            HTTP endpoints
    ops.py             /api/* for the panel, sign-in with the 2FA step
    auth.py            invitations, forgot / reset password, 2FA set-up, email + security settings
    public.py          /applications pages, downloads (linked releases redirect to gh-proxy), public API
web/
  shared/          theme.css (light + dark tokens), theme.js, favicon
  ops/             index.html, app.js (router, overview, workflows, runs), auth.js (sign-in, invite, reset, 2FA screens), pages.js
                   (deploy, env, releases, settings), roots.js + roots.css (the graph)
  public/          the downloads site
run.sh              start locally (auto-setup on first run)
scripts/           install.sh (full server setup), setup.sh (venv + libs + MySQL + .env), dev.sh, _lib.sh
```

---

## 5. Remaking, editing, continuing

* **Change the look**: all colours/spacing are CSS variables at the top of `web/shared/theme.css` (dark = `:root`, light = `[data-theme="light"]`). The graph is styled in `web/ops/roots.css`.
* **Add a panel page**: register `OPS.pages.<name> = function(page, segments, ctx){…}` in `web/ops/pages.js` and add it to `NAV` in `app.js`; add its endpoints in `app/routes/ops.py`.
* **Add another workflow** (e.g. `production_release`): nothing to do, it shows up in Workflows. To auto-publish its artifacts to the public page when it succeeds, that is a small addition in `routes_ops.run_detail` / `library.import_artifact`.
* **Add iOS**: publish an `.ipa` (or a run artifact containing one) with platform `ios`; the public card switches from "Coming soon" automatically.
* **Ideas not included on purpose** (kept production-lean): email/Slack notifications, 2FA, S3 storage for builds, multi-server deploys.

When asking Claude to continue, say which file(s) you want changed and paste the zip back (or this layout). The code is intentionally modular so a change touches one backend module and one page function.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| "Not found (or the token cannot see this repository)" | token lacks access to that repo, re-check in Settings > GitHub |
| "Cannot connect to MySQL" on start | run `scripts/setup.sh` again (installs / starts MariaDB, resets the DB user's password to the one in `.env`) |
| Run button says token is read-only | add **Actions: write** (fine-grained) or the `workflow` scope (classic) |
| Deploy fails at *Restart* with a sudo password prompt | sudoers rule missing or wrong service name |
| Deploy fails at *Tests* with "No module named pytest" | add `pytest` to the panel repo's `requirements.txt` |
| *Switch* fails "is a real directory" | `mv /srv/nazar_e_aalam_panel/current /srv/nazar_e_aalam_panel/current.old` |
| Upload stops at ~1 MB or errors 413 | raise `client_max_body_size` in nginx |
| Webhook shows 401 in GitHub | secret mismatch: rotate in Settings > Webhook and paste the new one |
| A GitHub release does not show on /applications | it needs a `.apk/.aab/.ipa/.zip` asset and a version-like tag (`v1.4.0`); see the problems list on the Releases page; press *Sync now* |
| Sync says "Download failed" | the asset is retried every sync; check disk space and `OPS_MAX_UPLOAD_MB` |
| Locked out or lost the phone | `python -m app.manage reset-password EMAIL` / `disable-2fa EMAIL` on the server (see Users and roles) |
| Invite or reset email never arrives | Settings > Email > *Send test*; check spam; the invite dialog always shows the link as a fallback |


## gh-proxy (no direct GitHub downloads)

Git clones for deployments and release-asset downloads are fetched through `https://gh-proxy.org`
(`OPS_GITHUB_PROXY`). Your GitHub token is never sent to the proxy. If the proxy fails (for example a
private repository), the panel retries directly from GitHub with the token; set
`OPS_GITHUB_PROXY_FALLBACK=false` to disable that. Set `OPS_GITHUB_PROXY=` (empty) to go direct only.
Workflow artifacts, job logs and the GitHub API itself still use the API, because proxies cannot
authenticate those.

## Releases are links, not copies

Imported / synced GitHub releases store only the **version, notes, size, hash (when GitHub provides one) and the
gh-proxy URL** of each file. Nothing is downloaded to this server. The public button still goes through
`/applications/download/...` so downloads are counted, then redirects to the gh-proxy URL. Each file in the
panel has a **Copy URL** button for the direct link. If gh-proxy is down, the redirect automatically uses the plain GitHub URL instead (checked live, cached 60s). Files are never downloaded; this is the fallback for
releases. Manual uploads are still stored on the server.

## Auto sync

Every few minutes (and 5 seconds after start) the panel reads all releases of the app repo and keeps the library identical:
new releases and new files are added as links, changed sizes/hashes/URLs are refreshed, and releases deleted, drafted or
emptied on GitHub are removed. **Latest** is the highest stable version per platform (1.10.0 beats 1.9.0), pre-releases
become **Beta**. Stable links: `/applications/<slug>/latest/android` and `/latest/ios` always redirect to the newest build.

## Full screen workflow watch

On a run page click **Full screen** (or press `F`). The graph fills the whole screen with a slim top bar (status, title, branch@sha, duration) and a slide-over details/log panel (`D` toggles it, clicking a node opens it). `Esc` or **Exit** leaves; navigating away exits automatically. It keeps live-updating while the run is in progress.

## First-run setup guide

The first time an admin signs in, the panel shows a step-by-step guide and stays locked (no auto deploy, no release sync)
until it is finished: **1 Domain** (main domain, `ops` sub-domain, server IP, HTTPS) → **2 DNS records** (exact A records
to add, with copy buttons and a live *Check DNS* button) → **3 Server & HTTPS** (one-command installer or manual nginx/certbot
files built for your domain) → **4 GitHub** (optional token/org/repos) → **5 Save & finish** (writes `.env`, restarts under systemd).
"Keep the current address working" leaves `OPS_DEV=true` so you cannot lock yourself out while DNS settles.
Existing installs that are upgraded are treated as already set up. Reopen the guide any time from the command palette.

## Shortcuts

`Ctrl/⌘+K` or `/` command palette · `g` then `o w r d e s b` jump to a page · `F` full screen on a run · `D` details in full screen · `?` help.
Clicking your name at the bottom of the sidebar opens Account settings.

## Servers: deploy to other machines over SSH

**Servers** (sidebar) lists where deployments can go: *This server* and any number of remote servers. A step-by-step guide per server:

1. **Connection**: address, SSH user (a normal user such as `deploy`), port, optional deploy folder / restart command / health URL for that server.
2. **SSH key**: the panel creates an ed25519 key (private half stays in `data/ssh`, mode 600) and shows one command that adds the public key to the server's `authorized_keys`. You can also paste your own key (no passphrase).
3. **Access check**: logs in and tests Python + venv, `tar`, write access to the deploy folder, free disk and **sudo for the restart command**. If sudo asks for a password, press **Grant access** and type the password once: the panel installs a sudoers rule for exactly that command (`/etc/sudoers.d/zz-nazar-ops-<user>`) and does not store the password. A manual command is shown too. The first login pins the server's host key; later connections use strict checking (*Forget host key* after a reinstall).
4. **Server setup**: runs automatically after sudo is granted (see 1c): packages, Python check, deploy folder, test build.

How a remote deploy works: the commit is fetched on the ops server (via gh-proxy), the `.env` is written, the release is streamed over SSH (`tar`), then virtualenv, install, tests, atomic `current` switch, restart (with sudo) and the health check run **on the remote server**. Failure restores the previous release there. Rollback and release lists are per server.

**Nothing deploys until it is ready.** Deployments (manual, webhook, auto) are blocked while the server has not passed the access check, no `.env` was added, or a required key is empty. Required keys: *Settings → Deploy → Required environment keys* plus every key in your repo's `.env.example` that has no value. The Deployments page lists exactly what to fix. Turn the rule off with *Block deployments until the environment is complete*.

Auto deploy and webhooks go to the server chosen on the Servers page ("Use for auto deploy"). Needs `openssh-client` on the ops server (`sudo apt install openssh-client`).
