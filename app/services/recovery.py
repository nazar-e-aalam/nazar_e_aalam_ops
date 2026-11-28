"""Bounded deployment recovery. Never execute repository code as root."""
import os
import re
import shlex
from pathlib import Path


def prepare_local(ctx, cfg):
    from .deploy import Failure
    root = Path(cfg['deploy_root'])
    try:
        for directory in (root, root / 'releases', root / 'shared' / 'venvs'):
            directory.mkdir(parents=True, exist_ok=True)
            if not os.access(directory, os.W_OK | os.X_OK):
                raise PermissionError(str(directory))
    except OSError as exc:
        raise Failure(f'Deployment directory inaccessible: {exc}. Run install.sh --deploy-root with the configured path.') from exc
    ctx.log.write('Preflight: deployment directories writable.')


def validate_runtime(ctx, tg, rel, venv):
    from .deploy import Failure
    python = f'{venv}/bin/python'
    # Only repair packages already present in the application environment.
    probe = '''import sys, importlib.util as u
if u.find_spec('sqlalchemy') and sys.version_info >= (3,14):
    import sqlalchemy
    from sqlalchemy.util.typing import make_union_type
    try: make_union_type(int, str)
    except TypeError: print('SQLALCHEMY_UNION')
if u.find_spec('pymysql'):
    from pymysql import _auth
    if not _auth._have_cryptography: print('PYMYSQL_RSA')
'''
    command = f'{shlex.quote(python)} -c {shlex.quote(probe)}'
    rc, out = tg.capture(command, 60)
    if rc:
        raise Failure('Runtime compatibility probe failed; inspect installed dependencies.')
    packages = []
    if 'SQLALCHEMY_UNION' in out.splitlines():
        packages.append('SQLAlchemy>=2.0.44,<2.1')
    if 'PYMYSQL_RSA' in out.splitlines():
        packages.append('cryptography>=42')
    if packages:
        ctx.log.write('Recovery: installing known compatibility dependencies: ' + ', '.join(packages))
        if tg.run(ctx, [python, '-m', 'pip', 'install', '--upgrade', *packages], rel, 600) != 0:
            raise Failure('Automatic dependency recovery failed')
        rc, out = tg.capture(command, 60)
        if rc or any(x in out.splitlines() for x in ('SQLALCHEMY_UNION', 'PYMYSQL_RSA')):
            raise Failure('Runtime remains incompatible after recovery')
    if tg.run(ctx, [python, '-m', 'pip', 'check'], rel, 120) != 0:
        raise Failure('Dependency conflicts remain after recovery')


def diagnose(ctx, cfg, tg):
    # Do not copy arbitrary journal messages into the web UI: they may contain secrets.
    match = re.fullmatch(r'(?:sudo\s+)?(?:/usr/bin/)?systemctl restart ([\w.@-]+)', cfg.get('restart_cmd', ''))
    if not match:
        return
    unit = match.group(1)
    rc, out = tg.capture(f'journalctl -u {shlex.quote(unit)} -n 100 --no-pager -o cat', 20)
    hints = {'PermissionError': 'Runtime file permission failure', 'caching_sha2_password': 'MySQL RSA authentication dependency or configuration', 'typing.Union': 'SQLAlchemy/Python typing incompatibility', 'ModuleNotFoundError': 'Missing application dependency', 'Access denied for user': 'Database credentials or grants rejected', 'Address already in use': 'Port conflict'}
    for token, hint in hints.items():
        if token in out:
            ctx.log.write('Startup diagnostic: ' + hint)
    ctx.log.write(f'For full startup logs, run journalctl -u {unit} -n 100 --no-pager on the target.')
