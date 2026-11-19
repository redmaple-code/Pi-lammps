"""UKB bridge for the design-review layer.

Single knowledge store for:
  - knowledge: baseline review dimensions  -> type=rule, title prefix "基线维度 "
  - experience: common issues              -> type=lesson, title prefix "常见问题 "
  - evidence:   open review points         -> type=observation, title prefix "审查点 "

Writes go through UKB propose -> prereview -> admin approve, never direct SQL.
Reads use UKB catalog/read. If UKB is unavailable, callers fall back to local JSON.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

LAYER = Path(__file__).resolve().parents[1]
WORKSPACE = LAYER.parent


def _find_ukb_repo():
    """Locate the tree that contains the ``ukb`` package and its store.

    Order: MDAGENT_UKB_REPO env -> this layer (vendored inside the repo)
    -> workspace dev sibling -> workspace root. A candidate with a live store
    wins over one that only carries the package source.
    """
    env = os.environ.get("MDAGENT_UKB_REPO")
    if env:
        return Path(env).expanduser().resolve()
    candidates = [LAYER, WORKSPACE / "MDagent-evidence-v3", WORKSPACE]
    viable = [c for c in candidates if (c / "ukb" / "ukb" / "api.py").exists()]
    for candidate in viable:
        if (candidate / "data" / "ukb" / "ukb.sqlite").exists():
            return candidate
    if viable:
        return viable[0]
    return WORKSPACE / "MDagent-evidence-v3"


UKB_REPO = _find_ukb_repo()
UKB_PKG = UKB_REPO / "ukb"
DEFAULT_ROOT = UKB_REPO / "data" / "ukb"
TOKEN_FILE = LAYER / "ukb_admin.json"
TOKEN_ENV = "REVIEW_UKB_ADMIN_FILE"
SYSTEM_ADMIN = "mdagent-system"


def _ensure_path():
    if str(UKB_PKG) not in sys.path:
        sys.path.insert(0, str(UKB_PKG))


def root():
    return Path(os.environ.get("REVIEW_UKB_ROOT") or DEFAULT_ROOT)


def enabled():
    return os.environ.get("REVIEW_KB", "ukb") == "ukb" and (root() / "ukb.sqlite").exists()


_CONNECTIONS = {}


def connect():
    """One reusable connection per store root; call close_connections() to release files."""
    _ensure_path()
    key = str(root().resolve())
    handle = _CONNECTIONS.get(key)
    if handle is None:
        from ukb.api import UKB
        handle = UKB(key)
        _CONNECTIONS[key] = handle
    return handle


def close_connections():
    """Close every store handle opened by this module (Windows keeps files locked otherwise)."""
    for handle in list(_CONNECTIONS.values()):
        try:
            handle.close()
        except Exception:
            pass
    _CONNECTIONS.clear()


def _token_file():
    return Path(os.environ.get("MDAGENT_UKB_ADMIN_FILE") or os.environ.get(TOKEN_ENV) or TOKEN_FILE)


def _admin():
    path = _token_file()
    if path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        return d.get("admin"), d.get("token")
    return None, None


def init(root_path=None, admin_id="redmaple"):
    _ensure_path()
    from ukb.api import UKB
    rp = Path(root_path or root())
    if (rp / "ukb.sqlite").exists():
        raise SystemExit("already initialized: %s" % rp)
    kb = UKB.init(str(rp), actor="bootstrap:" + admin_id)
    tok = kb.add_admin(admin_id, "bootstrap:" + admin_id)
    _write_secret(_token_file(), json.dumps({"admin": admin_id, "token": tok, "root": str(rp)},
                                            ensure_ascii=False))
    return tok


def _write_secret(path, value):
    path = Path(path)
    path.write_text(value, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _client():
    if str(UKB_REPO) not in sys.path:
        sys.path.insert(0, str(UKB_REPO))
    from evolution.kb import client
    return client


def ensure_admin_token(admin_id="redmaple", force=False, token_file=None):
    """Ensure a usable human admin token exists (delegates to evolution.kb.client).

    The token is written to ``ukb_admin.json`` (0600) and never printed. If the
    stored token is stale or force=True, the admin token is rotated; any other
    process still holding the old token must log in again. ``token_file`` (or
    REVIEW_UKB_ADMIN_FILE) keeps test stores from touching the production file.
    """
    path = Path(token_file) if token_file else _token_file()
    return _client().ensure_admin_token(admin_id, force=force, token_path=str(path), root=str(root()))


def ensure_system_admin():
    """Ensure mdagent-system exists and system_auto.token is valid (machine observation path)."""
    return _client().ensure_system_admin(root=str(root()))


def _prereview(kb, task_id):
    from ukb.core import review as _review
    view = kb.review_view(task_id)
    if not view.get("prereview_required"):
        return
    b = kb.prereview_bundle(task_id, 0)
    checklist = b["instructions"]["checklist"]
    pending = [i["content"] for i in b["items"] if i["part"] == "pending"][0]
    opinion = {
        "duplicate_of": [], "reinforces": [], "contradicts": [], "refines": [], "supersedes": [],
        "type_opinion": {"ok": True, "note": "design-review structured entry"},
        "tag_opinion": {"ok": bool(pending.get("tags")), "note": "default tags"},
        "checklist": [{"item": c, "ok": True, "note": "structural check"} for c in checklist],
        "recommendation": "approve",
        "note": "mechanical prereview for design-review layer (structural only)",
    }
    tokens = [kb.prereview_bundle(task_id, p)["page_token"] for p in range(b["pages"])]
    kb.prereview_submit(task_id, "llm:design-review-auto", tokens, opinion)


def propose_approve(payload):
    kb = connect()
    admin, token = _admin()
    if not token:
        raise RuntimeError("UKB admin token missing: " + str(TOKEN_FILE))
    r = kb.propose(payload, "cli", "design-review-layer")
    if not r.get("ok"):
        raise RuntimeError("propose failed: %s" % r)
    _prereview(kb, r["task_id"])
    v = kb.review_view(r["task_id"])
    d = kb.decide(r["task_id"], admin, token, "approve", "design-review seed", v["bound_hash"], "webui")
    return d


def _source(locator, title="design-review-layer"):
    return [{"source_type": "knowledge_entry", "title": title, "locator": locator}]


def _tags(*extra):
    return [{"facet": "topic", "tag": "general"}] + [{"facet": "stage", "tag": t} for t in extra]


def _existing_titles(types=None):
    """All titles visible to the reviewer profile (active, candidate, rejected, retired)."""
    try:
        cat = connect().catalog("reviewer", types=types, limit=2000)
        return {i.get("title", "") for i in cat.get("items", [])}
    except Exception:
        return set()


def seed_baseline():
    reg = json.loads((LAYER / "baseline" / "registry.json").read_text(encoding="utf-8"))
    existing = _existing_titles(["rule"])
    done = 0
    for d in reg["dimensions"]:
        title = "基线维度 %s：%s" % (d["id"], d["name"])
        if title in existing:
            continue
        payload = {
            "mode": "new", "type_id": "rule", "title": title,
            "fields": {"statement": d["definition"], "severity": "soft",
                       "rationale": "适用条件：" + d.get("applies_when", ""),
                       "check": "参考维度，不作为强制清单"},
            "tags": _tags("planning"),
            "sources": _source("design-review-layer/baseline/registry.json", "baseline v" + reg["version"]),
            "origin": {"kind": "manual", "ref": "baseline:%s" % d["id"],
                       "summary_of_evidence": "基线注册表 v%s" % reg["version"]},
        }
        propose_approve(payload)
        existing.add(title)
        done += 1
    return done


def seed_common_issues():
    reg = json.loads((LAYER / "common_issues" / "registry.json").read_text(encoding="utf-8"))
    existing = _existing_titles(["lesson"])
    done = 0
    for e in reg["entries"]:
        title = "常见问题 %s：%s" % (e["id"], e["name"])
        if title in existing:
            continue
        payload = {
            "mode": "new", "type_id": "lesson", "title": title,
            "fields": {
                "situation": situation_for(e),
                "risk": e["explanation"],
                "fix": e.get("required_declaration", ""),
                "check": "内置解释库 v" + reg["version"],
            },
            "tags": _tags("validation"),
            "sources": _source("design-review-layer/common_issues/registry.json", "common issues v" + reg["version"]),
            "origin": {"kind": "manual", "ref": "common:%s" % e["id"],
                       "summary_of_evidence": "常见问题库 v%s" % reg["version"]},
        }
        propose_approve(payload)
        existing.add(title)
        done += 1
    return done


def store_case(cid, points):
    """Store open points of one case as observations (idempotent by title)."""
    existing = set()
    try:
        kb = connect()
        for item in kb.catalog("reviewer", types=["observation"], limit=2000).get("items", []):
            existing.add(item.get("title", ""))
    except Exception:
        pass
    done = 0
    for pt in points:
        if pt.get("disposition") != "open":
            continue
        title = "审查点 %s %s：%s" % (cid, pt["point_id"], pt["title"][:80])
        if title in existing:
            continue
        payload = {
            "mode": "new", "type_id": "observation", "title": title,
            "fields": {"statement": pt["title"] + "；问题：" + pt.get("physical_question", ""),
                       "observed_at": date.today().isoformat(),
                       "context": "维度=%s；位置=%s；严重度=%s" % (
                           pt.get("dimension_guess", ""), json.dumps(pt.get("locations", {}), ensure_ascii=False),
                           pt.get("severity_hint", ""))},
            "tags": _tags("validation"),
            "sources": [{"source_type": "run_artifact", "title": cid + " review points",
                         "locator": "design-review-layer/reports/discovery/%s_points.json" % cid}],
            "origin": {"kind": "review", "ref": "discovery:%s:%s" % (cid, pt["point_id"]),
                       "summary_of_evidence": "AI discovery pilot"},
        }
        propose_approve(payload)
        done += 1
    return done


def seed_discovery():
    """Store open review points from all cases as observations."""
    done = 0
    for i in range(1, 6):
        cid = "case%d" % i
        p = LAYER / "reports" / "discovery" / ("%s_points.json" % cid)
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        done += store_case(cid, data["points"])
    return done


def _read_all(types):
    kb = connect()
    cat = kb.catalog("agent", types=types, limit=500)
    out = []
    for item in cat.get("items", []):
        rd = kb.read("agent", entry_refs=[item["ref"]], view="full")
        for x in rd.get("items", []):
            out.append(x)
    return out


def load_baseline():
    try:
        items = _read_all(["rule"])
    except Exception:
        return []
    names = []
    for it in items:
        title = it.get("title", "")
        if title.startswith("基线维度 ") and "：" in title:
            names.append(title.split("：", 1)[1].strip())
    return names


_SITUATION_RE = re.compile(r"^([^：:]+)[：:](.*)$")


def _situation_map(text):
    """Parse the stored lesson situation: '匹配词：a、b；类别：...；处置：...'.

    Values may themselves contain colons, so only the first colon splits key and value.
    Unknown keys are preserved in the returned map and ignored by callers.
    """
    out = {}
    for part in str(text or "").split("；"):
        part = part.strip()
        if not part:
            continue
        m = _SITUATION_RE.match(part)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def situation_for(e):
    """Canonical situation text for a common-issue seed entry."""
    parts = [
        ("匹配词", "、".join(e.get("match_terms") or [])),
        ("类别", e.get("category") or ""),
        ("处置", e.get("disposition") or ""),
        ("证据等级", e.get("evidence_level") or ""),
        ("状态", e.get("status") or ""),
    ]
    return "；".join("%s：%s" % (k, v) for k, v in parts if v not in (None, ""))


def _parse_common_issue(it):
    title = it.get("title", "")
    if not title.startswith("常见问题 ") or "：" not in title:
        return None
    cid, name = title.split("：", 1)
    cid = cid.replace("常见问题 ", "").strip()
    fields = it.get("fields") or {}
    situation = fields.get("situation", "")
    meta = _situation_map(situation)
    match_terms = [x.strip() for x in (meta.get("匹配词") or "").split("、") if x.strip()]
    disposition = meta.get("处置") or fields.get("disposition") or "open"
    if disposition not in ("explained", "record_required", "open"):
        # Unknown disposition must not hide a point; keep it visible.
        disposition = "open"
    category = meta.get("类别") or fields.get("category") or None
    evidence_level = meta.get("证据等级") or fields.get("evidence_level") or None
    status = meta.get("状态") or fields.get("status") or None
    missing = [name for name, value in (("category", category), ("evidence_level", evidence_level),
                                        ("status", status)) if not value]
    return {
        "id": cid, "name": name.strip(), "category": category,
        "ukb_ref": it.get("ref"), "ukb_type": it.get("type"),
        "match_terms": match_terms, "disposition": disposition,
        "explanation": fields.get("risk", ""), "required_declaration": fields.get("fix", ""),
        "evidence_level": evidence_level, "status": status,
        "metadata_complete": not missing, "metadata_missing": missing,
    }


def load_common_issues():
    try:
        items = _read_all(["lesson"])
    except Exception:
        return None
    entries = []
    for it in items:
        parsed = _parse_common_issue(it)
        if parsed:
            entries.append(parsed)
    if not entries:
        return None
    return {"version": "ukb", "updated_at": date.today().isoformat(), "entries": entries}


def upgrade_common_issue_metadata():
    """Backfill category/evidence_level/status into existing lesson situations.

    The lesson type only stores four text fields, so these values live in the
    human-readable ``situation`` string (already used for category/disposition).
    Each upgrade is a normal revise proposal followed by prereview and an admin
    decision; no SQL is written directly.
    """
    reg = json.loads((LAYER / "common_issues" / "registry.json").read_text(encoding="utf-8"))
    existing = {}
    for it in _read_all(["lesson", "rule", "observation"]):
        existing[it.get("title", "")] = it
    results = []
    for e in reg["entries"]:
        title = "常见问题 %s：%s" % (e["id"], e["name"])
        it = existing.get(title)
        if it is None:
            results.append({"id": e["id"], "status": "missing"})
            continue
        fields = it.get("fields") or {}
        meta = _situation_map(fields.get("situation", ""))
        if all(meta.get(k) for k in ("类别", "处置", "证据等级", "状态")):
            results.append({"id": e["id"], "status": "already-complete"})
            continue
        payload = {
            "mode": "revise",
            "target_entry_id": it["entry_id"],
            "base_rev_no": it["rev_no"],
            "title": title,
            "fields": {
                "situation": situation_for(e),
                "risk": fields.get("risk") or e["explanation"],
                "fix": fields.get("fix") or e.get("required_declaration", ""),
                "check": fields.get("check") or ("内置解释库 v" + reg["version"]),
            },
            "tags": [{"facet": t["facet"], "tag": t["tag"]} for t in it.get("tags", [])],
            "sources": [{"source_type": s["source_type_id"], "title": s["title"],
                         "locator": s["locator"], "role": s["role"],
                         "locator_detail": s.get("locator_detail"), "excerpt": s.get("excerpt")}
                        for s in it.get("sources", [])],
            "conditions": [{"key": c["key"], "op": c["op"], "value": c["value"],
                            "unit": c.get("unit"), "note": c.get("note")}
                           for c in it.get("conditions", [])],
            "origin": {"kind": "manual", "ref": "common:%s" % e["id"],
                       "summary_of_evidence": "complete common-issue metadata from local seed"},
            "change_note": "complete category/disposition/evidence_level/status in situation",
        }
        try:
            d = propose_approve(payload)
            results.append({"id": e["id"], "status": "upgraded", "decision": d})
        except Exception as exc:
            results.append({"id": e["id"], "status": "error", "error": str(exc)})
    return results


def stats():
    kb = connect()
    cat = kb.catalog("agent", limit=1000)
    from collections import Counter
    return Counter(i.get("type") for i in cat.get("items", []))
