"""Workflow YAML parsing and conversion of a GitHub run into the node tree that
the "roots" visualisation draws. Nodes are keyed by the job ID from the YAML."""
import time

import yaml

from . import github as gh

_yaml_cache: dict = {}


def status_of(status, conclusion) -> str:
    if status in ("queued", "waiting", "requested", "pending"):
        return "queued"
    if status == "in_progress":
        return "running"
    if status == "completed":
        return {"success": "success", "neutral": "success", "failure": "failed", "timed_out": "failed",
                "startup_failure": "failed", "action_required": "failed", "cancelled": "cancelled",
                "stale": "cancelled", "skipped": "skipped"}.get(conclusion, "failed")
    return "queued"


def fmt_dur(sec) -> str:
    if sec is None:
        return ""
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def duration(start, end, running=False):
    a = gh.iso_to_epoch(start)
    if a is None:
        return None
    b = gh.iso_to_epoch(end) if end else None
    if b is None:
        b = time.time() if running else None
    return None if b is None else b - a


def parse_workflow(text: str) -> dict:
    try:
        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            raise yaml.YAMLError("workflow is not a mapping")
    except yaml.YAMLError as e:
        return {"name": None, "inputs": [], "jobs": {}, "dispatch": False, "error": str(e)[:300]}
    on = doc.get("on", doc.get(True))  # YAML 1.1 turns the key `on` into True
    dispatch, inputs = False, []
    if isinstance(on, str):
        dispatch = on == "workflow_dispatch"
    elif isinstance(on, list):
        dispatch = "workflow_dispatch" in on
    elif isinstance(on, dict) and "workflow_dispatch" in on:
        dispatch = True
        wd = on["workflow_dispatch"] or {}
        for key, spec in (wd.get("inputs") or {}).items():
            spec = spec or {}
            opts = [str(o) for o in (spec.get("options") or [])]
            inputs.append({"key": str(key), "description": str(spec.get("description") or ""),
                           "required": bool(spec.get("required", False)), "default": spec.get("default"),
                           "type": spec.get("type") or ("choice" if opts else "string"), "options": opts})
    jobs = {}
    for jid, job in (doc.get("jobs") or {}).items():
        job = job or {}
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        jobs[str(jid)] = {"name": str(job.get("name") or jid), "needs": [str(n) for n in needs],
                          "uses": job.get("uses")}
    return {"name": doc.get("name"), "inputs": inputs, "jobs": jobs, "dispatch": dispatch, "error": None}


def get_definition(repo_key: str, path: str, ref: str) -> dict:
    """Workflow file at an exact commit is immutable, so cache it forever (bounded)."""
    key = (repo_key, path, ref)
    if key in _yaml_cache:
        return _yaml_cache[key]
    try:
        wf = parse_workflow(gh.text_file(repo_key, path, ref))
    except gh.GitHubError as e:
        return {"name": None, "inputs": [], "jobs": {}, "dispatch": False, "error": e.message}
    if len(_yaml_cache) > 200:
        _yaml_cache.clear()
    if not wf["error"]:
        _yaml_cache[key] = wf
    return wf


def validate_inputs(defn: dict, given: dict) -> dict:
    """Return string inputs ready for the API, or raise ValueError with a readable message."""
    out = {}
    for spec in defn["inputs"]:
        k = spec["key"]
        raw = given.get(k)
        if raw is None or raw == "":
            raw = spec["default"] if spec["default"] is not None else None
        if spec["type"] == "boolean":
            if raw is None:
                raw = False
            val = "true" if (raw is True or str(raw).lower() == "true") else "false"
        else:
            if raw is None:
                if spec["required"]:
                    raise ValueError(f"'{k}' is required")
                continue
            val = str(raw)
            if spec["type"] == "choice" and val not in spec["options"]:
                raise ValueError(f"'{k}' must be one of: {', '.join(spec['options'])}")
            if spec["type"] == "number":
                try:
                    float(val)
                except ValueError:
                    raise ValueError(f"'{k}' must be a number")
        out[k] = val
    return out


def _job_matches(jid: str, jd: dict, api_name: str) -> bool:
    name = jd["name"]
    if "${{" in name:
        prefix = name.split("${{")[0].strip()
        if prefix and api_name.startswith(prefix):
            return True
    elif api_name == name or api_name.startswith(name + " ("):
        return True
    if jd.get("uses"):
        return api_name == jid or api_name.startswith(jid + " /")
    return api_name == jid or api_name.startswith(jid + " (")


def _step_nodes(job: dict, prefix: str) -> list:
    nodes = []
    for s in job.get("steps") or []:
        st = status_of(s.get("status"), s.get("conclusion"))
        d = duration(s.get("started_at"), s.get("completed_at"), st == "running")
        nodes.append({"key": f"{prefix}:s{s.get('number')}", "name": s.get("name") or f"step {s.get('number')}",
                      "type": "job", "status": st,
                      "meta": fmt_dur(d) if d is not None else ("waiting" if st == "queued" else st),
                      "ref": {"kind": "step", "job_id": job["id"], "number": s.get("number")}})
    return nodes


def _combine(statuses: list) -> str:
    if not statuses:
        return "queued"
    for s in ("running", "failed", "queued", "cancelled"):
        if s in statuses:
            return s
    return "skipped" if all(s == "skipped" for s in statuses) else "success"


def build_tree(run: dict, api_jobs: list, wf: dict) -> dict:
    run_state = status_of(run.get("status"), run.get("conclusion"))
    run_done = run.get("status") == "completed"
    yjobs = wf.get("jobs") or {}
    claimed, nodes = set(), {}

    for jid, jd in yjobs.items():
        matches = [j for j in api_jobs if j["id"] not in claimed and _job_matches(jid, jd, j["name"])]
        for j in matches:
            claimed.add(j["id"])
        if not matches:
            st = "skipped" if run_done else "queued"
            node = {"key": f"job:{jid}", "name": jid, "type": "workflow", "status": st,
                    "meta": "skipped" if run_done else "waiting", "children": [],
                    "ref": {"kind": "job", "job_ids": [], "display": jd["name"], "job_id": None}}
        else:
            sts = [status_of(j.get("status"), j.get("conclusion")) for j in matches]
            st = _combine(sts)
            start = min((j.get("started_at") or "9" for j in matches))
            ends = [j.get("completed_at") for j in matches]
            end = max(ends) if all(ends) else None
            d = duration(None if start == "9" else start, end, st == "running")
            meta = fmt_dur(d) if d is not None else ("waiting" if st == "queued" else st)
            node = {"key": f"job:{jid}", "name": jid, "type": "workflow", "status": st, "meta": meta,
                    "ref": {"kind": "job", "job_ids": [j["id"] for j in matches], "display": jd["name"],
                            "job_id": matches[0]["id"] if len(matches) == 1 else None}}
            if len(matches) == 1:
                node["children"] = _step_nodes(matches[0], node["key"])
            else:
                legs = []
                for j in matches:
                    lst = status_of(j.get("status"), j.get("conclusion"))
                    ld = duration(j.get("started_at"), j.get("completed_at"), lst == "running")
                    legs.append({"key": f"leg:{j['id']}", "name": j["name"].replace(jd["name"], "", 1).strip(" ()") or j["name"],
                                 "type": "job", "status": lst, "meta": fmt_dur(ld) if ld is not None else lst,
                                 "ref": {"kind": "job", "job_ids": [j["id"]], "display": j["name"], "job_id": j["id"]},
                                 "children": _step_nodes(j, f"leg:{j['id']}")})
                node["children"] = legs
        node["collapsed"] = node["status"] not in ("running", "failed")
        nodes[jid] = node

    # dependency depth: the primary parent is the deepest `needs`, so depth is consistent
    depth, visiting = {}, set()

    def dep(jid):
        if jid in depth:
            return depth[jid]
        if jid in visiting:
            return 0
        visiting.add(jid)
        needs = [n for n in yjobs[jid]["needs"] if n in yjobs]
        depth[jid] = 1 + max((dep(n) for n in needs), default=0)
        visiting.discard(jid)
        return depth[jid]

    for jid in yjobs:
        dep(jid)

    root = {"key": "run", "name": run.get("name") or wf.get("name") or "run", "type": "main", "status": run_state,
            "meta": f"#{run.get('run_number')} · {run.get('head_branch')} · {(run.get('head_sha') or '')[:7]}",
            "ref": {"kind": "run"}, "children": []}
    order = list(yjobs)
    for jid in order:
        needs = [n for n in yjobs[jid]["needs"] if n in yjobs and n != jid]
        node = nodes[jid]
        if not needs:
            root["children"].append(node)
            continue
        primary = max(needs, key=lambda n: (depth[n], -order.index(n)))
        parent = nodes[primary]
        # real jobs sit before their step children, so append in front of steps
        parent.setdefault("children", [])
        parent["children"].insert(sum(1 for c in parent["children"] if c["type"] == "workflow"), node)
        node_links = [f"job:{n}" for n in needs if n != primary]
        if node_links:
            node["links"] = node_links
    for j in api_jobs:
        if j["id"] not in claimed:
            st = status_of(j.get("status"), j.get("conclusion"))
            d = duration(j.get("started_at"), j.get("completed_at"), st == "running")
            root["children"].append({
                "key": f"extra:{j['id']}", "name": j["name"], "type": "workflow", "status": st,
                "meta": fmt_dur(d) if d is not None else st, "collapsed": st not in ("running", "failed"),
                "ref": {"kind": "job", "job_ids": [j["id"]], "display": j["name"], "job_id": j["id"]},
                "children": _step_nodes(j, f"extra:{j['id']}")})
    return root


def shape_run(r: dict, tracked: dict | None = None) -> dict:
    st = status_of(r.get("status"), r.get("conclusion"))
    d = duration(r.get("run_started_at") or r.get("created_at"), r.get("updated_at") if r.get("status") == "completed" else None,
                 st == "running")
    actor = (r.get("triggering_actor") or r.get("actor") or {}).get("login")
    return {"id": r["id"], "name": r.get("name"), "title": r.get("display_title") or r.get("name"),
            "number": r.get("run_number"), "attempt": r.get("run_attempt"), "status": st,
            "gh_status": r.get("status"), "conclusion": r.get("conclusion"), "branch": r.get("head_branch"),
            "sha": (r.get("head_sha") or "")[:7], "event": r.get("event"), "actor": actor,
            "avatar": (r.get("triggering_actor") or r.get("actor") or {}).get("avatar_url"),
            "panel_user": (tracked or {}).get("triggered_by"), "workflow_id": r.get("workflow_id"),
            "path": r.get("path"), "created_at": r.get("created_at"), "started_at": r.get("run_started_at"),
            "updated_at": r.get("updated_at"), "duration": d, "duration_text": fmt_dur(d) if d is not None else "",
            "url": r.get("html_url"), "head_sha": r.get("head_sha")}
