"""Review: mandatory AI prereview (advisory) and a single administrator decision.

Model: AI agents and people propose; another AI prereviews by reading the full material; one administrator
approves, corrects or rejects. There are no roles, quorums or policies beyond that.
"""
import hashlib
import hmac
import json
import os
import re
import secrets

from . import audit, documents, intake, navigate, registry
from .canon import canonical_json, hash_obj, new_id, normalize, now
from .errors import UKBError

CONTENT_KINDS = ("new", "revise", "reinforce")
ADMIN_DECISIONS = ("approve", "approve_as_trial", "request_changes", "reject", "correct", "reinforce_into")
TRANSITIONS = {
    "active": {"deprecated", "superseded", "revoked"},
    "trial": {"active", "deprecated", "revoked"},
    "deprecated": {"active", "revoked"},
    "quarantined": {"candidate", "rejected"},
    "candidate": {"rejected"},
}
_LLM_ID_RE = re.compile(r"^llm:[A-Za-z0-9_.-]{1,60}$")
_ADMIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,40}$")
PBKDF2_ROUNDS = max(1000, int(os.environ.get("UKB_PBKDF2_ROUNDS", "200000")))
PREREVIEW_SCOPE_STATUSES = ("active", "trial", "deprecated", "candidate", "quarantined")


def agent_name(actor):
    """'agent:x', 'llm:x' and 'x' all name the same AI identity."""
    return str(actor).split(":", 1)[-1]


# ---------- administrators ----------

def _hash_token(salt, token):
    return hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def add_admin(conn, admin_id, actor):
    if not _ADMIN_ID_RE.match(admin_id or ""):
        raise UKBError("ADMIN_INVALID", "admin id must be lowercase letters/digits/._-")
    if conn.execute("SELECT 1 FROM reviewer WHERE reviewer_id=?", (admin_id,)).fetchone():
        raise UKBError("ADMIN_EXISTS", "admin already exists")
    token = secrets.token_urlsafe(24)
    salt = secrets.token_hex(16)
    conn.execute("INSERT INTO reviewer(reviewer_id,kind,token_salt,token_hash,active,created_at) "
                 "VALUES (?,'admin',?,?,1,?)", (admin_id, salt, _hash_token(salt, token), now()))
    audit.append(conn, actor, "admin.add", "reviewer", admin_id, {})
    return token


def reset_token(conn, admin_id, actor):
    r = conn.execute("SELECT * FROM reviewer WHERE reviewer_id=? AND kind='admin'", (admin_id,)).fetchone()
    if r is None:
        raise UKBError("ADMIN_UNKNOWN", "no such admin")
    token = secrets.token_urlsafe(24)
    salt = secrets.token_hex(16)
    conn.execute("UPDATE reviewer SET token_salt=?, token_hash=? WHERE reviewer_id=?",
                 (salt, _hash_token(salt, token), admin_id))
    audit.append(conn, actor, "admin.reset_token", "reviewer", admin_id, {})
    return token


def set_admin_active(conn, admin_id, active, actor):
    cur = conn.execute("UPDATE reviewer SET active=? WHERE reviewer_id=? AND kind='admin'",
                       (int(bool(active)), admin_id))
    if cur.rowcount != 1:
        raise UKBError("ADMIN_UNKNOWN", "no such admin")
    audit.append(conn, actor, "admin.active", "reviewer", admin_id, {"active": bool(active)})


def authenticate(conn, admin_id, token):
    r = conn.execute("SELECT * FROM reviewer WHERE reviewer_id=?", (admin_id,)).fetchone()
    if r is None or r["kind"] != "admin" or not r["active"] or not isinstance(token, str) or not token:
        raise UKBError("AUTH_FAILED", "unknown admin, inactive admin or bad token")
    if not hmac.compare_digest(_hash_token(r["token_salt"], token), r["token_hash"]):
        raise UKBError("AUTH_FAILED", "unknown admin, inactive admin or bad token")
    return {"reviewer_id": admin_id}


def _ensure_llm_reviewer(conn, reviewer_id):
    if not _LLM_ID_RE.match(reviewer_id or ""):
        raise UKBError("REVIEWER_INVALID", "AI prereviewer id must look like llm:<name>")
    if conn.execute("SELECT 1 FROM reviewer WHERE reviewer_id=?", (reviewer_id,)).fetchone() is None:
        conn.execute("INSERT INTO reviewer(reviewer_id,kind,token_salt,token_hash,active,created_at) "
                     "VALUES (?,'llm',NULL,NULL,1,?)", (reviewer_id, now()))


# ---------- task views ----------

def _task(conn, task_id):
    t = conn.execute("SELECT * FROM review_task WHERE task_id=?", (task_id,)).fetchone()
    if t is None:
        raise UKBError("TASK_UNKNOWN", "task not found", task_id=task_id)
    return t


def _open_task(conn, task_id):
    t = _task(conn, task_id)
    if t["state"] != "open":
        raise UKBError("TASK_CLOSED", "task is not open", state=t["state"])
    return t


def _prereviews(conn, task_id, bound_hash=None):
    q = "SELECT * FROM review_decision WHERE task_id=? AND decision='prereview'"
    args = [task_id]
    if bound_hash is not None:
        q += " AND bound_hash=?"
        args.append(bound_hash)
    return [dict(r) for r in conn.execute(q + " ORDER BY decided_at", args)]


def _task_title(conn, t):
    if t["rev_id"]:
        r = conn.execute("SELECT title, type_id FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
        return r["title"], r["type_id"]
    if t["entry_id"]:
        e = conn.execute("SELECT e.type_id, r.title FROM entry e LEFT JOIN entry_revision r ON r.rev_id=e.head_rev "
                         "WHERE e.entry_id=?", (t["entry_id"],)).fetchone()
        return e["title"], e["type_id"]
    p = json.loads(t["payload_json"] or "{}")
    return "%s: %s" % (p.get("kind"), p.get("reason", "")), None


def queue(conn, state="open", kind=None, limit=200, needs=None):
    """needs='prereview' lists tasks still waiting for AI prereview; needs='admin' those ready for the admin."""
    q = "SELECT * FROM review_task WHERE state=?"
    args = [state]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY opened_at"
    out = []
    for t in conn.execute(q, args):
        pre = bool(_prereviews(conn, t["task_id"], t["bound_hash"]))
        ready = pre or not t["prereview_required"]
        if needs == "prereview" and (ready or not t["prereview_required"]):
            continue
        if needs == "admin" and not ready:
            continue
        title, etype = _task_title(conn, t)
        recs = [json.loads(p["opinion_json"]).get("recommendation")
                for p in _prereviews(conn, t["task_id"], t["bound_hash"])]
        ctx = conn.execute("SELECT context_json FROM intake WHERE intake_id=?", (t["intake_id"],)).fetchone() \
            if t["intake_id"] else None
        out.append({"task_id": t["task_id"], "kind": t["kind"], "entry_id": t["entry_id"], "type": etype,
                    "title": title, "submitter": t["submitter"], "opened_at": t["opened_at"],
                    "prereview_required": bool(t["prereview_required"]), "prereviewed": pre,
                    "prereview_recommendations": recs,
                    "origin": json.loads(ctx["context_json"]) if ctx and ctx["context_json"] else None,
                    "risk": json.loads(t["risk_hits_json"]) if t["risk_hits_json"] else [],
                    "exact_duplicate_of": t["exact_dup_of"]})
        if len(out) >= int(limit):
            break
    return out


def _render_pending(conn, t):
    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
    rev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
    r = navigate.render(conn, e, rev)
    r["is_pending"] = True
    return r


def review_view(conn, task_id):
    t = _task(conn, task_id)
    view = {"task_id": t["task_id"], "kind": t["kind"], "state": t["state"], "submitter": t["submitter"],
            "opened_at": t["opened_at"], "bound_hash": t["bound_hash"],
            "prereview_required": bool(t["prereview_required"]),
            "risk": json.loads(t["risk_hits_json"]) if t["risk_hits_json"] else [],
            "exact_duplicate_of": t["exact_dup_of"],
            "payload": json.loads(t["payload_json"]) if t["payload_json"] else None}
    if t["intake_id"]:
        ctx = conn.execute("SELECT context_json FROM intake WHERE intake_id=?", (t["intake_id"],)).fetchone()
        view["origin"] = json.loads(ctx["context_json"]) if ctx and ctx["context_json"] else None
    if t["kind"] in ("new", "revise"):
        view["pending"] = _render_pending(conn, t)
        rev = conn.execute("SELECT type_id, type_version FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
        view["checklist"] = registry.type_spec(conn, rev["type_id"], rev["type_version"])["spec"].get(
            "review_checklist", [])
        if t["kind"] == "revise":
            e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
            if e["head_rev"]:
                cur = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (e["head_rev"],)).fetchone()
                view["current"] = navigate.render(conn, e, cur)
        view["landing_scope"] = _landing(conn, t["entry_id"])
    elif t["entry_id"]:
        e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
        rev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (e["head_rev"] or t["rev_id"],)).fetchone()
        if rev is not None:
            view["current"] = navigate.render(conn, e, rev)
    view["prereviews"] = [{"reviewer": p["reviewer_id"], "at": p["decided_at"],
                           "current": p["bound_hash"] == t["bound_hash"],
                           "opinion": json.loads(p["opinion_json"])} for p in _prereviews(conn, task_id)]
    view["decisions"] = [{"reviewer": d["reviewer_id"], "decision": d["decision"], "rationale": d["rationale"],
                          "at": d["decided_at"]}
                         for d in conn.execute("SELECT * FROM review_decision WHERE task_id=? AND "
                                               "decision<>'prereview' ORDER BY decided_at", (task_id,))]
    return view


def _landing(conn, entry_id):
    out = []
    for t in navigate.entry_tags(conn, entry_id):
        ids = registry.tag_descendants(conn, t["tag_id"])
        n = conn.execute("SELECT count(DISTINCT et.entry_id), coalesce(sum(r.char_count),0) FROM entry_tag et "
                         "JOIN entry e ON e.entry_id=et.entry_id JOIN entry_revision r ON r.rev_id=e.head_rev "
                         "WHERE et.state='confirmed' AND e.status IN ('active','trial') AND et.tag_id IN (%s)"
                         % ",".join("?" * len(ids)), ids).fetchone()
        out.append({"facet": t["facet_id"], "tag": t["slug"], "state": t["state"], "tag_status": t["tag_status"],
                    "existing_entries": n[0], "existing_chars": n[1]})
    return out


# ---------- AI prereview ----------

OPINION_LISTS = ("duplicate_of", "reinforces", "contradicts", "refines", "supersedes")


def _taxonomy(conn, entry_id):
    """Tag trees of every facet the entry uses, plus required facets, so tags can be judged against the
    descriptions of their siblings rather than in isolation."""
    e = conn.execute("SELECT type_id FROM entry WHERE entry_id=?", (entry_id,)).fetchone()
    facets = {t["facet_id"] for t in navigate.entry_tags(conn, entry_id)}
    facets.update(registry.type_spec(conn, e["type_id"])["spec"].get("required_facets", []))
    out = []
    for fid in sorted(facets):
        f = registry.facet(conn, fid)
        tags = []
        for t in conn.execute("SELECT * FROM tag WHERE facet_id=? AND status IN ('active','candidate') ORDER BY slug",
                              (fid,)):
            parent = conn.execute("SELECT slug FROM tag WHERE tag_id=?", (t["parent_tag_id"],)).fetchone() \
                if t["parent_tag_id"] else None
            tags.append({"tag": t["slug"], "label": t["label"], "description": t["description"],
                         "parent": parent[0] if parent else None, "status": t["status"],
                         "aliases": registry.tag_aliases(conn, t["tag_id"])})
        out.append({"facet": fid, "label": f["label"], "description": f["description"], "multi": bool(f["multi"]),
                    "tags": tags})
    return out


def _prereview_parts(conn, blobs, t):
    """Deterministic list of (kind, label, content) parts the prereviewer must read."""
    parts = []
    if t["kind"] in ("new", "revise"):
        pending = _render_pending(conn, t)
        parts.append(("pending", pending["ref"], pending))
        ctx = conn.execute("SELECT context_json FROM intake WHERE intake_id=?", (t["intake_id"],)).fetchone() \
            if t["intake_id"] else None
        if ctx and ctx["context_json"]:
            parts.append(("origin", t["intake_id"], json.loads(ctx["context_json"])))
        e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
        rev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
        if e["head_rev"] and e["head_rev"] != rev["rev_id"]:
            cur = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (e["head_rev"],)).fetchone()
            parts.append(("current", "%s@%d" % (e["entry_id"], cur["rev_no"]), navigate.render(conn, e, cur)))
        if rev["manifest_hash"]:
            for f in intake.load_rev_files(conn, rev["rev_id"]):
                text = documents.readable_text(blobs, f)
                parts.append(("original", "%s:%s" % (pending["ref"], f["path"]),
                              {"path": f["path"], "role": f["role_id"], "readable": text is not None,
                               "text": text}))
        parts.append(("taxonomy", e["entry_id"], _taxonomy(conn, e["entry_id"])))
        scope_ids = set()
        for x in navigate.entry_tags(conn, e["entry_id"]):
            scope_ids.update(registry.tag_descendants(conn, x["tag_id"]))
            parent = conn.execute("SELECT parent_tag_id FROM tag WHERE tag_id=?", (x["tag_id"],)).fetchone()[0]
            if parent:
                scope_ids.add(parent)
        if scope_ids:
            rows = conn.execute(
                "SELECT DISTINCT e.* FROM entry e JOIN entry_tag et ON et.entry_id=e.entry_id WHERE et.tag_id IN (%s)"
                " AND et.state IN ('confirmed','suggested') AND e.entry_id<>? AND e.status IN (%s) "
                "ORDER BY e.type_id, e.entry_id" % (",".join("?" * len(scope_ids)),
                                                    ",".join("?" * len(PREREVIEW_SCOPE_STATUSES))),
                list(scope_ids) + [e["entry_id"]] + list(PREREVIEW_SCOPE_STATUSES)).fetchall()
            for o in rows:
                orev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (o["head_rev"],)).fetchone() \
                    if o["head_rev"] else conn.execute("SELECT * FROM entry_revision WHERE entry_id=? ORDER BY rev_no "
                                                       "DESC LIMIT 1", (o["entry_id"],)).fetchone()
                parts.append(("scope", "%s@%d" % (o["entry_id"], orev["rev_no"]), navigate.render(conn, o, orev)))
    elif t["kind"] == "reinforce":
        e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
        rev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (e["head_rev"],)).fetchone()
        parts.append(("current", "%s@%d" % (e["entry_id"], rev["rev_no"]), navigate.render(conn, e, rev)))
        parts.append(("reinforce", t["task_id"], json.loads(t["payload_json"])))
    else:
        raise UKBError("PREREVIEW_NOT_APPLICABLE", "this task kind has no AI prereview", kind=t["kind"])
    return parts


_PARTS_CACHE = {}
_PARTS_CACHE_MAX = 8


def _cached_pages(conn, blobs, t, budget):
    """Rendering the scope once per task state; any write to the KB bumps the audit seq and invalidates."""
    seq = conn.execute("SELECT coalesce(max(seq),0) FROM audit_log").fetchone()[0]
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    key = (db_file, id(conn), t["task_id"], t["bound_hash"], seq, budget)
    hit = _PARTS_CACHE.get(key)
    if hit is None:
        parts = _prereview_parts(conn, blobs, t)
        hit = (parts, _pages(parts, budget))
        if len(_PARTS_CACHE) >= _PARTS_CACHE_MAX:
            _PARTS_CACHE.pop(next(iter(_PARTS_CACHE)))
        _PARTS_CACHE[key] = hit
    return hit


def _pages(parts, budget):
    pages, cur, size = [], [], 0
    for kind, label, content in parts:
        item = {"part": kind, "ref": label, "content": content}
        s = len(canonical_json(item))
        if kind == "original" and content.get("text") and s > budget:
            text = content["text"]
            step = max(1000, budget - 2000)
            for k in range(0, len(text), step):
                piece = dict(content, text=text[k:k + step], chunk="%d-%d/%d" % (k, min(len(text), k + step),
                                                                                len(text)))
                if cur:
                    pages.append(cur)
                    cur, size = [], 0
                pages.append([{"part": kind, "ref": label, "content": piece}])
            continue
        if cur and size + s > budget:
            pages.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += s
    if cur:
        pages.append(cur)
    return pages


def _page_token(bound_hash, i, page):
    return hash_obj([bound_hash, i, page])[:24]


def _checklist(conn, t):
    if not t["rev_id"] or t["kind"] == "reinforce":
        return []
    rev = conn.execute("SELECT type_id, type_version FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
    return registry.type_spec(conn, rev["type_id"], rev["type_version"])["spec"].get("review_checklist", [])


def prereview_bundle(conn, blobs, task_id, page=0, budget=40000):
    t = _open_task(conn, task_id)
    if t["kind"] not in CONTENT_KINDS:
        raise UKBError("PREREVIEW_NOT_APPLICABLE", "this task kind has no AI prereview", kind=t["kind"])
    parts, pages = _cached_pages(conn, blobs, t, budget)
    if not isinstance(page, int) or page < 0 or page >= len(pages):
        raise UKBError("PAGE_INVALID", "page out of range", pages=len(pages))
    out = {"task_id": task_id, "kind": t["kind"], "submitter": t["submitter"], "bound_hash": t["bound_hash"],
           "page": page, "pages": len(pages), "page_token": _page_token(t["bound_hash"], page, pages[page]),
           "items": pages[page]}
    if page == 0:
        out["instructions"] = {
            "read": "Read every page fully (pending entry, origin, originals, taxonomy and every scope entry) "
                    "before submitting. You may not prereview a proposal you submitted yourself.",
            "judge": ["Is the content correct, self-contained and supported by its sources?",
                      "Is the type right for what it says?",
                      "Do the tags follow the tag descriptions in 'taxonomy'? Would a sibling or child tag fit "
                      "better? Is a required facet missing? Is a proposed new tag really needed, or does an "
                      "existing tag (or alias) already cover it?",
                      "Does any scope entry say the same thing (duplicate / reinforces), say the opposite "
                      "(contradicts), or does this replace one (supersedes)?",
                      "For document entries: does every cited claim match the original at its locator?"],
            "submit": "Call prereview_submit with all page tokens and an opinion object.",
            "opinion_schema": {
                "duplicate_of|reinforces|contradicts|refines|supersedes": "[{entry_id, note}]",
                "type_opinion": "{ok: bool, suggest: type_id?, note}",
                "tag_opinion": "{ok: bool, drop: [facet/tag], add: [{facet, tag | new_tag, reason}], "
                               "merge_new_into: [{new: facet/slug, existing: facet/slug}], note}",
                "checklist": "[{item, ok: bool, note}] covering every checklist item",
                "summary_fidelity": "document types only: [{claim, locator, ok: bool, note}]",
                "recommendation": "approve | approve_as_trial | request_changes | reject",
                "note": "free text",
            },
            "checklist": _checklist(conn, t),
            "document": any(p[0] == "original" for p in parts),
        }
    return out


def _tag_key_ok(conn, key, where):
    if not isinstance(key, str) or "/" not in key:
        raise UKBError("OPINION_INVALID", "tag reference must be facet/tag", where=where, tag=key)
    f, s = key.split("/", 1)
    try:
        registry.resolve_tag(conn, f, s)
    except UKBError:
        raise UKBError("OPINION_INVALID", "unknown tag in opinion", where=where, tag=key)


def _check_opinion(conn, t, op, checklist, is_doc):
    if not isinstance(op, dict):
        raise UKBError("OPINION_INVALID", "opinion must be an object")
    allowed = set(OPINION_LISTS) | {"type_opinion", "tag_opinion", "checklist", "summary_fidelity", "recommendation",
                                    "note"}
    if set(op) - allowed:
        raise UKBError("OPINION_INVALID", "unknown opinion keys", keys=sorted(set(op) - allowed))
    for k in OPINION_LISTS:
        v = op.get(k, [])
        if not isinstance(v, list):
            raise UKBError("OPINION_INVALID", "%s must be a list" % k)
        for x in v:
            if not isinstance(x, dict) or not isinstance(x.get("note"), str) or not x["note"].strip():
                raise UKBError("OPINION_INVALID", "%s items need entry_id and note" % k)
            eid, _ = navigate.parse_ref(x.get("entry_id"))
            if eid == t["entry_id"]:
                raise UKBError("OPINION_INVALID", "an entry cannot relate to itself", list=k)
            if conn.execute("SELECT 1 FROM entry WHERE entry_id=?", (eid,)).fetchone() is None:
                raise UKBError("OPINION_INVALID", "unknown entry in %s" % k, entry_id=eid)
    content = t["kind"] in ("new", "revise")
    to = op.get("type_opinion")
    if content:
        if not isinstance(to, dict) or not isinstance(to.get("ok"), bool):
            raise UKBError("OPINION_INCOMPLETE", "type_opinion {ok, suggest?, note} is required")
        if not to["ok"] and to.get("suggest") not in registry.ids(conn, "reg_type", "type_id"):
            raise UKBError("OPINION_INVALID", "type_opinion.suggest must name a registered type")
    tg = op.get("tag_opinion")
    if content:
        if not isinstance(tg, dict) or not isinstance(tg.get("ok"), bool):
            raise UKBError("OPINION_INCOMPLETE", "tag_opinion {ok, drop, add, merge_new_into, note} is required")
        if set(tg) - {"ok", "drop", "add", "merge_new_into", "note"}:
            raise UKBError("OPINION_INVALID", "unknown tag_opinion keys")
        for key in tg.get("drop", []):
            _tag_key_ok(conn, key, "drop")
        if tg.get("add"):
            intake.resolve_tag_proposals(conn, tg["add"], [], existing_entry_id=t["entry_id"])
        for m in tg.get("merge_new_into", []):
            if not isinstance(m, dict):
                raise UKBError("OPINION_INVALID", "merge_new_into items must be {new, existing}")
            _tag_key_ok(conn, m.get("new"), "merge_new_into.new")
            _tag_key_ok(conn, m.get("existing"), "merge_new_into.existing")
        if not tg["ok"] and not (tg.get("drop") or tg.get("add") or tg.get("merge_new_into") or tg.get("note")):
            raise UKBError("OPINION_INVALID", "say what is wrong with the tags")
    if op.get("recommendation") not in ("approve", "approve_as_trial", "request_changes", "reject"):
        raise UKBError("OPINION_INVALID", "recommendation must be approve, approve_as_trial, request_changes or reject")
    if not isinstance(op.get("note"), str) or not op["note"].strip():
        raise UKBError("OPINION_INVALID", "note is required")
    cl = op.get("checklist", [])
    if not isinstance(cl, list) or any(not isinstance(c, dict) or not isinstance(c.get("ok"), bool) for c in cl):
        raise UKBError("OPINION_INVALID", "checklist items need item and ok")
    covered = {c.get("item") for c in cl}
    missing = [c for c in checklist if c not in covered]
    if missing:
        raise UKBError("OPINION_INCOMPLETE", "checklist items not answered", items=missing)
    if is_doc:
        sf = op.get("summary_fidelity")
        if not isinstance(sf, list) or not sf:
            raise UKBError("OPINION_INCOMPLETE", "document entries need summary_fidelity checks")
        for c in sf:
            if not isinstance(c, dict) or not isinstance(c.get("ok"), bool) or not c.get("claim"):
                raise UKBError("OPINION_INVALID", "summary_fidelity items need claim and ok")


def prereview_submit(conn, blobs, task_id, reviewer_id, page_tokens, opinion, budget=40000):
    t = _open_task(conn, task_id)
    if t["kind"] not in CONTENT_KINDS:
        raise UKBError("PREREVIEW_NOT_APPLICABLE", "this task kind has no AI prereview", kind=t["kind"])
    if agent_name(reviewer_id) == agent_name(t["submitter"]):
        raise UKBError("SELF_PREREVIEW", "the AI that submitted a proposal cannot prereview it")
    if _prereviews(conn, task_id, t["bound_hash"]) and any(
            p["reviewer_id"] == reviewer_id for p in _prereviews(conn, task_id, t["bound_hash"])):
        raise UKBError("PREREVIEW_DUPLICATE", "you already prereviewed the current content")
    parts, pages = _cached_pages(conn, blobs, t, budget)
    _ensure_llm_reviewer(conn, reviewer_id)
    expected = [_page_token(t["bound_hash"], i, p) for i, p in enumerate(pages)]
    if not isinstance(page_tokens, list) or sorted(page_tokens) != sorted(expected):
        raise UKBError("PREREVIEW_INCOMPLETE", "page tokens do not cover the whole current bundle; "
                       "re-read all pages (the bundle may have changed)", pages=len(pages))
    is_doc = any(p[0] == "original" for p in parts)
    opinion = normalize(opinion)
    _check_opinion(conn, t, opinion, _checklist(conn, t), is_doc)
    did = new_id("d")
    conn.execute("INSERT INTO review_decision(decision_id,task_id,reviewer_id,reviewer_kind,channel,decision,rationale,"
                 "opinion_json,adopted_json,tag_decisions_json,bound_hash,decided_at) "
                 "VALUES (?,?,?,'llm','mcp','prereview',?,?,NULL,NULL,?,?)",
                 (did, task_id, reviewer_id, opinion["note"], canonical_json(opinion), t["bound_hash"], now()))
    audit.append(conn, reviewer_id, "prereview", "task", task_id, {"decision_id": did,
                                                                     "recommendation": opinion["recommendation"]})
    return {"ok": True, "decision_id": did, "task_id": task_id, "counts_as_approval": False}


# ---------- admin decisions ----------

def _prereview_satisfied(conn, t):
    if not t["prereview_required"]:
        return True
    if _prereviews(conn, t["task_id"], t["bound_hash"]):
        return True
    corrected = conn.execute("SELECT 1 FROM review_decision WHERE task_id=? AND decision='correct' AND bound_hash=?",
                             (t["task_id"], t["bound_hash"])).fetchone()
    return bool(corrected and _prereviews(conn, t["task_id"]))


def _retire_orphan_candidates(conn):
    for r in conn.execute("SELECT t.tag_id FROM tag t WHERE t.status='candidate' AND NOT EXISTS (SELECT 1 FROM entry_tag"
                          " et WHERE et.tag_id=t.tag_id AND et.state IN ('suggested','confirmed'))").fetchall():
        conn.execute("UPDATE tag SET status='deprecated' WHERE tag_id=?", (r[0],))
        conn.execute("DELETE FROM tag_alias WHERE tag_id=?", (r[0],))


def _apply_tag_decisions(conn, entry_id, td, admin_id):
    """Default on approval: confirm every suggested tag. td can reject some, add others, or fold a proposed
    new tag into an existing one (then the candidate is retired)."""
    td = td or {}
    if not isinstance(td, dict) or set(td) - {"reject", "add", "merge_new_into"}:
        raise UKBError("TAG_INVALID", "tag decisions must be {reject: [facet/tag], add: [...], merge_new_into: [...]}")
    reject = set(td.get("reject", []))
    for m in td.get("merge_new_into", []):
        f, s = m["new"].split("/", 1)
        new = registry.resolve_tag(conn, f, s)
        ex = registry.resolve_tag(conn, *m["existing"].split("/", 1))
        if new["status"] != "candidate" or new["facet_id"] != ex["facet_id"]:
            raise UKBError("TAG_INVALID", "merge_new_into needs a candidate tag and an existing tag of the same facet")
        reject.add(m["new"])
        td.setdefault("add", []).append({"facet": ex["facet_id"], "tag": ex["slug"], "reason": "merged from " + m["new"]})
    if td.get("add"):
        props = intake.resolve_tag_proposals(conn, td["add"], [], existing_entry_id=entry_id)
        intake.write_tag_proposals(conn, entry_id, props, "human", admin_id)
    rows = navigate.entry_tags(conn, entry_id, ("suggested", "confirmed"))
    known = {"%s/%s" % (r["facet_id"], r["slug"]) for r in rows}
    unknown = reject - known
    if unknown:
        raise UKBError("TAG_INVALID", "cannot reject tags that are not on the entry", tags=sorted(unknown))
    for r in rows:
        key = "%s/%s" % (r["facet_id"], r["slug"])
        state = "rejected" if key in reject else "confirmed"
        conn.execute("UPDATE entry_tag SET state=?, decided_by=?, updated_at=? WHERE entry_id=? AND tag_id=?",
                     (state, admin_id, now(), entry_id, r["tag_id"]))
        if state == "confirmed" and r["tag_status"] == "candidate":
            conn.execute("UPDATE tag SET status='active' WHERE tag_id=?", (r["tag_id"],))
            audit.append(conn, admin_id, "tag.activate", "tag", str(r["tag_id"]), {"via": entry_id})
    _retire_orphan_candidates(conn)
    tspec = registry.type_spec(conn, conn.execute("SELECT type_id FROM entry WHERE entry_id=?",
                                                  (entry_id,)).fetchone()[0])
    have = {r["facet_id"] for r in navigate.entry_tags(conn, entry_id, ("confirmed",))}
    missing = [f for f in tspec["spec"].get("required_facets", []) if f not in have]
    if missing:
        raise UKBError("TAG_REQUIRED", "approval would leave required facets without confirmed tags", facets=missing)
    single = conn.execute("SELECT t.facet_id, count(*) FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id JOIN reg_facet "
                          "f ON f.facet_id=t.facet_id WHERE et.entry_id=? AND et.state='confirmed' AND f.multi=0 "
                          "GROUP BY t.facet_id HAVING count(*)>1", (entry_id,)).fetchall()
    if single:
        raise UKBError("TAG_INVALID", "single-choice facet has several tags", facets=[r[0] for r in single])


def _add_relation(conn, src, rel_id, dst, note, task_id):
    rel = conn.execute("SELECT * FROM reg_relation WHERE rel_id=? AND status='active'", (rel_id,)).fetchone()
    if rel is None:
        raise UKBError("RELATION_INVALID", "unknown relation", rel=rel_id)
    for e in (src, dst):
        if conn.execute("SELECT 1 FROM entry WHERE entry_id=?", (e,)).fetchone() is None:
            raise UKBError("RELATION_INVALID", "unknown entry", entry_id=e)
    if src == dst:
        raise UKBError("RELATION_INVALID", "an entry cannot relate to itself")
    if rel["acyclic"]:
        reach = conn.execute("WITH RECURSIVE r(id) AS (SELECT ? UNION SELECT x.dst_entry FROM relation x JOIN r ON "
                             "x.src_entry=r.id WHERE x.rel_id=?) SELECT 1 FROM r WHERE id=?",
                             (dst, rel_id, src)).fetchone()
        if reach:
            raise UKBError("RELATION_CYCLE", "relation would create a cycle", rel=rel_id)
    a, b = (min(src, dst), max(src, dst)) if rel["symmetric"] else (src, dst)
    conn.execute("INSERT OR IGNORE INTO relation(src_entry,rel_id,dst_entry,note,task_id,created_at) "
                 "VALUES (?,?,?,?,?,?)", (a, rel_id, b, note, task_id, now()))


def _apply_relations(conn, entry_id, rels, task_id):
    if rels is None:
        return
    if not isinstance(rels, list):
        raise UKBError("RELATION_INVALID", "relations must be a list")
    for r in rels:
        if not isinstance(r, dict):
            raise UKBError("RELATION_INVALID", "relation must be an object")
        other, _ = navigate.parse_ref(r.get("entry_id"))
        direction = r.get("direction", "out")
        if direction not in ("out", "in"):
            raise UKBError("RELATION_INVALID", "direction must be out or in")
        src, dst = (entry_id, other) if direction == "out" else (other, entry_id)
        _add_relation(conn, src, r.get("rel"), dst, r.get("note"), task_id)
        if r.get("rel") == "supersedes" and r.get("retire_other"):
            o = conn.execute("SELECT status FROM entry WHERE entry_id=?", (dst,)).fetchone()
            if o["status"] in ("active", "trial", "deprecated"):
                conn.execute("UPDATE entry SET status='superseded', status_note=?, updated_at=? WHERE entry_id=?",
                             ("superseded by %s" % src, now(), dst))
                audit.append(conn, "system", "status.superseded", "entry", dst, {"by": src, "task_id": task_id})


def _commit(conn, t, who, decision, args):
    kind = t["kind"]
    if kind in ("new", "revise"):
        e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
        rev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
        _apply_tag_decisions(conn, e["entry_id"], args.get("tag_decisions"), who["reviewer_id"])
        if kind == "revise" and e["status"] in ("active", "trial", "deprecated") and decision == "approve":
            new_status = e["status"]
        else:
            new_status = "trial" if decision == "approve_as_trial" else "active"
        conn.execute("UPDATE entry SET head_rev=?, status=?, status_note=NULL, updated_at=? WHERE entry_id=?",
                     (rev["rev_id"], new_status, now(), e["entry_id"]))
        conn.execute("INSERT INTO rev_approval(rev_id,task_id,approved_at) VALUES (?,?,?)",
                     (rev["rev_id"], t["task_id"], now()))
        _apply_relations(conn, e["entry_id"], args.get("relations"), t["task_id"])
        audit.append(conn, who["reviewer_id"], "commit." + kind, "entry", e["entry_id"],
                     {"rev_no": rev["rev_no"], "status": new_status, "content_hash": rev["content_hash"]})
    elif kind == "reinforce":
        p = json.loads(t["payload_json"])
        for s in p["sources"]:
            intake.upsert_source(conn, s)
            conn.execute("INSERT OR IGNORE INTO reinforcement(reinforce_id,entry_id,source_id,task_id,note,created_at) "
                         "VALUES (?,?,?,?,?,?)", (new_id("f"), t["entry_id"], s["source_id"], t["task_id"], p["note"],
                                                  now()))
        audit.append(conn, who["reviewer_id"], "commit.reinforce", "entry", t["entry_id"], {"sources": len(p["sources"])})
    elif kind == "status_change":
        p = json.loads(t["payload_json"])
        target = p["target_status"]
        if p.get("recheck"):
            target = args.get("target_status")
            if target in (None, "", "keep"):
                audit.append(conn, who["reviewer_id"], "recheck.keep", "entry", t["entry_id"], {})
                conn.execute("UPDATE review_task SET state='approved', closed_at=? WHERE task_id=?",
                             (now(), t["task_id"]))
                return
        elif args.get("target_status") not in (None, target):
            raise UKBError("DECISION_INVALID", "target status is fixed by the proposal")
        e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
        if target not in TRANSITIONS.get(e["status"], set()):
            raise UKBError("STATUS_INVALID", "transition not allowed", frm=e["status"], to=target)
        if target == "superseded":
            by = p.get("by_entry")
            if not by:
                raise UKBError("STATUS_INVALID", "superseded needs by_entry")
            _add_relation(conn, by, "supersedes", e["entry_id"], p.get("reason"), t["task_id"])
        if target == "active" and e["head_rev"] is None:
            raise UKBError("STATUS_INVALID", "entry has no approved revision")
        conn.execute("UPDATE entry SET status=?, status_note=?, updated_at=? WHERE entry_id=?",
                     (target, p.get("reason"), now(), e["entry_id"]))
        audit.append(conn, who["reviewer_id"], "commit.status", "entry", e["entry_id"], {"from": e["status"],
                                                                                          "to": target})
    elif kind == "tag_confirm":
        p = json.loads(t["payload_json"])
        e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
        if p.get("add"):
            props = intake.resolve_tag_proposals(conn, p["add"], [], existing_entry_id=e["entry_id"])
            intake.write_tag_proposals(conn, e["entry_id"], props, "human", who["reviewer_id"])
        drop = set(p.get("drop", [])) | set((args.get("tag_decisions") or {}).get("reject", []))
        _apply_tag_decisions(conn, e["entry_id"], {"reject": sorted(drop)}, who["reviewer_id"])
        audit.append(conn, who["reviewer_id"], "commit.tags", "entry", e["entry_id"], p)
    elif kind == "registry_change":
        p = json.loads(t["payload_json"])
        registry.apply_change(conn, p["kind"], p["spec"], who["reviewer_id"])
    if t["intake_id"]:
        conn.execute("UPDATE intake SET state='committed' WHERE intake_id=?", (t["intake_id"],))
    conn.execute("UPDATE review_task SET state='approved', closed_at=? WHERE task_id=?", (now(), t["task_id"]))


def _record(conn, t, who, decision, rationale, channel, adopted=None, tag_dec=None):
    did = new_id("d")
    conn.execute("INSERT INTO review_decision(decision_id,task_id,reviewer_id,reviewer_kind,channel,decision,rationale,"
                 "opinion_json,adopted_json,tag_decisions_json,bound_hash,decided_at) "
                 "VALUES (?,?,?,'admin',?,?,?,NULL,?,?,?,?)",
                 (did, t["task_id"], who["reviewer_id"], channel, decision, rationale,
                  canonical_json(adopted) if adopted is not None else None,
                  canonical_json(tag_dec) if tag_dec is not None else None, t["bound_hash"], now()))
    return did


def _correct(conn, blobs, t, who, rationale, args, pdf_extractor):
    corr = args.get("correction")
    if not isinstance(corr, dict):
        raise UKBError("DECISION_INVALID", "correct needs a correction object")
    corr = dict(corr)
    files = corr.pop("files", None)
    corr = normalize(corr)
    if files is not None:
        corr["files"] = intake._normalize_files(files)
    allowed = {"title", "fields", "conditions", "sources", "files", "tags"}
    if set(corr) - allowed:
        raise UKBError("DECISION_INVALID", "unknown correction keys", keys=sorted(set(corr) - allowed))
    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone()
    old = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone()
    tspec = registry.type_spec(conn, old["type_id"], old["type_version"])
    base = {"title": old["title"], "fields": json.loads(old["fields_json"]),
            "conditions": intake.load_rev_conditions(conn, old["rev_id"]),
            "sources": [{"source_type": s["source_type_id"], "title": s["title"], "locator": s["locator"],
                         "grade": s["grade"], "independence_key": s["independence_key"], "role": s["role"],
                         "locator_detail": s["locator_detail"], "excerpt": s["excerpt"]}
                        for s in intake.load_rev_sources(conn, old["rev_id"])]}
    for c in base["conditions"]:
        if c.get("note") is None:
            c.pop("note")
    base.update({k: v for k, v in corr.items() if k != "tags"})
    built = intake.build_revision(conn, blobs, tspec, base, old["rev_id"], registry.setting(conn, "intake_roots"),
                                  pdf_extractor)
    if built["content_hash"] == old["content_hash"] and not corr.get("tags"):
        raise UKBError("NO_CHANGE", "correction does not change the content; use approve")
    if built["content_hash"] != old["content_hash"]:
        last = conn.execute("SELECT max(rev_no) FROM entry_revision WHERE entry_id=?", (e["entry_id"],)).fetchone()[0]
        new_rev = intake.persist_revision(conn, e["entry_id"], last + 1, tspec, built, old["rev_id"],
                                          who["reviewer_id"], "admin correction: " + rationale)
        conn.execute("UPDATE review_task SET rev_id=?, bound_hash=? WHERE task_id=?",
                     (new_rev, built["content_hash"], t["task_id"]))
        audit.append(conn, who["reviewer_id"], "review.correct", "entry", e["entry_id"],
                     {"task_id": t["task_id"], "rev_no": last + 1, "content_hash": built["content_hash"]})
    if corr.get("tags"):
        props = intake.resolve_tag_proposals(conn, corr["tags"], [], existing_entry_id=e["entry_id"])
        intake.write_tag_proposals(conn, e["entry_id"], props, "human", who["reviewer_id"])
    return _task(conn, t["task_id"])


def decide(conn, blobs, task_id, who, decision, rationale, bound_hash, channel, args=None, pdf_extractor=None):
    """who = authenticate(...) result. Must run in a write transaction."""
    args = args or {}
    if channel == "mcp":
        raise UKBError("CHANNEL_DENIED", "admin decisions are not accepted over MCP")
    if decision not in ADMIN_DECISIONS:
        raise UKBError("DECISION_INVALID", "unknown decision", decision=decision)
    if not isinstance(rationale, str) or not rationale.strip():
        rationale = decision
    t = _open_task(conn, task_id)
    if bound_hash != t["bound_hash"]:
        raise UKBError("STALE_REVIEW", "the task content changed after you opened it; reload and review again")

    if decision == "correct":
        if t["kind"] not in ("new", "revise"):
            raise UKBError("DECISION_INVALID", "correct only applies to content tasks")
        t = _correct(conn, blobs, t, who, rationale, args, pdf_extractor)
        _record(conn, t, who, "correct", rationale, channel, adopted=args.get("relations"),
                tag_dec=args.get("tag_decisions"))
        decision = "approve_as_trial" if args.get("as_trial") else "approve"
    elif decision in ("approve", "approve_as_trial"):
        _record(conn, t, who, decision, rationale, channel, adopted=args.get("relations"),
                tag_dec=args.get("tag_decisions"))
    else:
        _record(conn, t, who, decision, rationale, channel, adopted=args.get("relations"))

    if decision in ("approve", "approve_as_trial"):
        if not _prereview_satisfied(conn, t):
            raise UKBError("PREREVIEW_REQUIRED", "AI prereview of the current content is required before approval")
        _commit(conn, t, who, decision, args)
        return {"ok": True, "task_id": task_id, "state": "approved", "entry_id": t["entry_id"]}

    if decision == "request_changes":
        conn.execute("UPDATE review_task SET state='changes_requested', closed_at=? WHERE task_id=?", (now(), task_id))
        if t["intake_id"]:
            conn.execute("UPDATE intake SET state='changes_requested' WHERE intake_id=?", (t["intake_id"],))
        audit.append(conn, who["reviewer_id"], "review.request_changes", "task", task_id, {"rationale": rationale})
        return {"ok": True, "task_id": task_id, "state": "changes_requested"}

    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (t["entry_id"],)).fetchone() if t["entry_id"] else None
    if decision == "reinforce_into":
        if t["kind"] != "new":
            raise UKBError("DECISION_INVALID", "reinforce_into only applies to new entries")
        target_id, _ = navigate.parse_ref(args.get("target_entry_id"))
        tgt = conn.execute("SELECT * FROM entry WHERE entry_id=?", (target_id,)).fetchone()
        if tgt is None or tgt["status"] not in ("active", "trial") or target_id == t["entry_id"]:
            raise UKBError("DECISION_INVALID", "target must be another active or trial entry")
        have = {r[0] for r in conn.execute("SELECT s.independence_key FROM entry_source es JOIN source s ON "
                                           "s.source_id=es.source_id WHERE es.rev_id=?", (tgt["head_rev"],))}
        have |= {r[0] for r in conn.execute("SELECT s.independence_key FROM reinforcement rf JOIN source s ON "
                                            "s.source_id=rf.source_id WHERE rf.entry_id=?", (target_id,))}
        added = []
        for s in intake.load_rev_sources(conn, t["rev_id"]):
            if s["independence_key"] in have or s["role"] not in ("supports", "derived_from"):
                continue
            have.add(s["independence_key"])
            conn.execute("INSERT OR IGNORE INTO reinforcement(reinforce_id,entry_id,source_id,task_id,note,"
                         "created_at) VALUES (?,?,?,?,?,?)", (new_id("f"), target_id, s["source_id"], task_id,
                                                              rationale, now()))
            added.append(s["source_id"])
        _add_relation(conn, t["entry_id"], "duplicate_of", target_id, rationale, task_id)
    if e is not None and t["kind"] == "new":
        conn.execute("UPDATE entry SET status='rejected', status_note=?, updated_at=? WHERE entry_id=?",
                     (rationale, now(), e["entry_id"]))
        conn.execute("UPDATE entry_tag SET state='rejected', decided_by=?, updated_at=? WHERE entry_id=? "
                     "AND state='suggested'", (who["reviewer_id"], now(), e["entry_id"]))
    elif e is not None and t["kind"] == "revise":
        conn.execute("UPDATE entry_tag SET state='rejected', decided_by=?, updated_at=? WHERE entry_id=? "
                     "AND state='suggested'", (who["reviewer_id"], now(), e["entry_id"]))
        if e["status"] == "quarantined" and e["head_rev"] is None:
            conn.execute("UPDATE entry SET status='rejected', updated_at=? WHERE entry_id=?", (now(), e["entry_id"]))
    _retire_orphan_candidates(conn)
    conn.execute("UPDATE review_task SET state='rejected', closed_at=? WHERE task_id=?", (now(), task_id))
    if t["intake_id"]:
        conn.execute("UPDATE intake SET state='rejected' WHERE intake_id=?", (t["intake_id"],))
    detail = {"rationale": rationale}
    if decision == "reinforce_into":
        detail["reinforcing_sources_added"] = len(added)
    audit.append(conn, who["reviewer_id"], "review." + decision, "task", task_id, detail)
    out = {"ok": True, "task_id": task_id, "state": "rejected", "decision": decision}
    if decision == "reinforce_into":
        out["reinforcing_sources_added"] = len(added)
    return out


# ---------- other proposals ----------

def propose_status(conn, entry_ref, target_status, reason, submitter, by_entry=None):
    entry_id, _ = navigate.parse_ref(entry_ref)
    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (entry_id,)).fetchone()
    if e is None:
        raise UKBError("ENTRY_UNKNOWN", "entry not found")
    if target_status not in TRANSITIONS.get(e["status"], set()):
        raise UKBError("STATUS_INVALID", "transition not allowed", frm=e["status"], to=target_status,
                       allowed=sorted(TRANSITIONS.get(e["status"], set())))
    if not isinstance(reason, str) or not reason.strip():
        raise UKBError("PAYLOAD_INVALID", "reason is required")
    if target_status == "superseded":
        if not by_entry:
            raise UKBError("PAYLOAD_INVALID", "superseded needs by_entry")
        by_entry, _ = navigate.parse_ref(by_entry)
        if conn.execute("SELECT 1 FROM entry WHERE entry_id=? AND status IN ('active','trial')", (by_entry,)).fetchone() \
                is None:
            raise UKBError("PAYLOAD_INVALID", "by_entry must be an active or trial entry")
    if intake.entry_has_open_task(conn, entry_id, ("status_change",)):
        raise UKBError("TASK_CONFLICT", "entry already has an open status change")
    payload = {"target_status": target_status, "reason": reason, "by_entry": by_entry}
    tid = intake.open_task(conn, "status_change", submitter, hash_obj(payload), entry_id=entry_id,
                           rev_id=e["head_rev"], payload=payload)
    audit.append(conn, submitter, "propose.status", "entry", entry_id, payload)
    return {"ok": True, "task_id": tid}


def propose_tags(conn, entry_ref, add, drop, reason, submitter):
    entry_id, _ = navigate.parse_ref(entry_ref)
    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (entry_id,)).fetchone()
    if e is None:
        raise UKBError("ENTRY_UNKNOWN", "entry not found")
    add = add or []
    drop = drop or []
    if not add and not drop:
        raise UKBError("PAYLOAD_INVALID", "nothing to change")
    if not isinstance(reason, str) or not reason.strip():
        raise UKBError("PAYLOAD_INVALID", "reason is required")
    if add:
        intake.resolve_tag_proposals(conn, add, [], existing_entry_id=entry_id)
    have = {"%s/%s" % (r["facet_id"], r["slug"]) for r in navigate.entry_tags(conn, entry_id)}
    bad = [d for d in drop if d not in have]
    if bad:
        raise UKBError("TAG_INVALID", "drop refers to tags not on the entry", tags=bad)
    if intake.entry_has_open_task(conn, entry_id, ("tag_confirm",)):
        raise UKBError("TASK_CONFLICT", "entry already has an open tag change")
    payload = {"add": add, "drop": sorted(drop), "reason": reason}
    tid = intake.open_task(conn, "tag_confirm", submitter, hash_obj(payload), entry_id=entry_id,
                           rev_id=e["head_rev"], payload=payload)
    audit.append(conn, submitter, "propose.tags", "entry", entry_id, payload)
    return {"ok": True, "task_id": tid}


def propose_registry(conn, kind, spec, reason, submitter):
    spec = normalize(spec)
    registry.validate_change(conn, kind, spec)
    if not isinstance(reason, str) or not reason.strip():
        raise UKBError("PAYLOAD_INVALID", "reason is required")
    payload = {"kind": kind, "spec": spec, "reason": reason}
    tid = intake.open_task(conn, "registry_change", submitter, hash_obj(payload), payload=payload)
    audit.append(conn, submitter, "propose.registry", "registry", kind, {"task_id": tid})
    return {"ok": True, "task_id": tid}


def feedback(conn, entry_ref, consumer, outcome, note, task_ref):
    entry_id, rev_no = navigate.parse_ref(entry_ref)
    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (entry_id,)).fetchone()
    if e is None:
        raise UKBError("ENTRY_UNKNOWN", "entry not found")
    if outcome not in ("helpful", "neutral", "misleading", "not_applicable", "reused_ok", "reused_failed"):
        raise UKBError("PAYLOAD_INVALID", "invalid outcome")
    if not isinstance(consumer, str) or not consumer.strip():
        raise UKBError("PAYLOAD_INVALID", "consumer is required")
    conn.execute("INSERT INTO usage_log(entry_id,rev_no,consumer,task_ref,outcome,note,ts) VALUES (?,?,?,?,?,?,?)",
                 (entry_id, rev_no, consumer, task_ref, outcome, note, now()))
    out = {"ok": True, "recheck_task": None}
    if outcome in ("misleading", "reused_failed") and e["status"] in ("active", "trial"):
        since = conn.execute("SELECT max(closed_at) FROM review_task WHERE entry_id=? AND state<>'open'",
                             (entry_id,)).fetchone()[0] or ""
        n = conn.execute("SELECT count(*) FROM usage_log WHERE entry_id=? AND outcome IN ('misleading','reused_failed')"
                         " AND ts>?", (entry_id, since)).fetchone()[0]
        if n >= registry.setting(conn, "feedback_recheck_threshold") and \
                not intake.entry_has_open_task(conn, entry_id, ("status_change",)):
            payload = {"target_status": None, "recheck": True, "by_entry": None,
                       "reason": "recheck after %d negative feedback report(s); latest: %s" % (n, note or outcome)}
            tid = intake.open_task(conn, "status_change", "system:feedback", hash_obj(payload),
                                   entry_id=entry_id, rev_id=e["head_rev"], payload=payload)
            audit.append(conn, "system:feedback", "recheck.open", "entry", entry_id, {"task_id": tid})
            out["recheck_task"] = tid
    return out
