"""Versioned .env storage. Every save creates a new version; each deployment
writes the version that is active at that moment into the new release."""
import hashlib
import re
import time

from ..db import db
from ..db.models import EnvVersion

MAX_BYTES = 256 * 1024
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(.*)$")


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    return text if text.endswith("\n") or not text else text + "\n"


def parse(text: str):
    """Return (variables, problems). variables: list of (key, value)."""
    out, problems, seen = [], [], set()
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            problems.append(f"line {no}: not a KEY=VALUE pair")
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key in seen:
            problems.append(f"line {no}: {key} is defined more than once")
        seen.add(key)
        out.append((key, val))
    return out, problems


def _fmt(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]*", value):
        return value
    if "'" not in value and "\n" not in value:
        return "'" + value + "'"          # single quotes: no expansion in any dotenv parser
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def apply(content: str, updates: dict) -> str:
    """Return content with KEY=value set for every key in updates (existing lines replaced, new ones appended)."""
    lines, left = content.split("\n"), dict(updates)
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) in left:
            lines[i] = f"{m.group(1)}={_fmt(left.pop(m.group(1)))}"
    while lines and lines[-1] == "":
        lines.pop()
    for k, v in left.items():
        lines.append(f"{k}={_fmt(v)}")
    return "\n".join(lines) + "\n"


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return value[:2] + "•" * min(12, len(value) - 4) + value[-2:]


def current():
    return db.get(EnvVersion, order=EnvVersion.version.desc())


def get(version: int):
    return db.get(EnvVersion, EnvVersion.version == version)


def save(content: str, user: str, note: str = ""):
    content = normalize(content)
    if len(content.encode()) > MAX_BYTES:
        raise ValueError("The .env file is larger than 256 KB.")
    if "\x00" in content:
        raise ValueError("That does not look like a text file.")
    variables, problems = parse(content)
    hard = [p for p in problems if "not a KEY" in p]
    if hard:
        raise ValueError("Invalid .env: " + "; ".join(hard[:4]))
    sha = hashlib.sha256(content.encode()).hexdigest()
    cur = current()
    if cur and cur["sha"] == sha:
        raise ValueError("No changes: identical to the current version.")
    version = (cur["version"] if cur else 0) + 1
    db.insert(EnvVersion, version=version, content=content, sha=sha, note=note[:200], created_by=user,
              created_at=time.time(), key_count=len(variables))
    return {"version": version, "keys": len(variables), "warnings": problems}


def view(row: dict, reveal: bool = False) -> dict:
    variables, problems = parse(row["content"])
    return {"version": row["version"], "sha": row["sha"][:10], "note": row["note"], "created_by": row["created_by"],
            "created_at": row["created_at"], "keys": row["key_count"], "problems": problems,
            "variables": [{"key": k, "value": v if reveal else mask(v), "empty": v == ""} for k, v in variables]}


def diff(a: dict, b: dict) -> dict:
    va, vb = dict(parse(a["content"])[0]), dict(parse(b["content"])[0])
    return {"added": sorted(set(vb) - set(va)), "removed": sorted(set(va) - set(vb)),
            "changed": sorted(k for k in va if k in vb and va[k] != vb[k])}


def versions(limit: int = 50):
    return db.all(EnvVersion, order=EnvVersion.version.desc(), limit=limit,
                  cols=[EnvVersion.version, EnvVersion.sha, EnvVersion.note, EnvVersion.created_by,
                        EnvVersion.created_at, EnvVersion.key_count])
