"""The single write entry: propose(). Structural checks only; everything semantic goes to review."""
import json
import math
import re

from . import audit, documents, registry
from .canon import canonical_json, hash_obj, new_id, normalize, now
from .errors import UKBError
from .typespec import validate_fields, walk_text

MODES = ("new", "revise", "reinforce")
SOURCE_ROLES = ("supports", "contradicts", "derived_from", "example")
COND_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "range", "exists")
CHANNELS = ("mcp", "cli", "webui", "import", "api")
PREREVIEW_KINDS = ("new", "revise", "reinforce")
CONTEXT_KINDS = ("conversation", "task", "run", "document", "review", "manual")
MAX_TITLE = 200
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _raw_for_storage(payload):
    """Keep the payload as given, but replace inline file contents by their hash to avoid duplicating blobs."""
    p = json.loads(canonical_json(payload))
    for f in p.get("files") or []:
        if isinstance(f, dict):
            for k in ("content_text", "content_b64", "text_content_text", "text_content_b64"):
                if isinstance(f.get(k), str):
                    f[k] = {"omitted_sha256": hash_obj(f[k]), "chars": len(f[k])}
    return p


# ---------- component validators ----------

def _title(v):
    if not isinstance(v, str) or not v.strip():
        raise UKBError("PAYLOAD_INVALID", "title is required")
    if len(v) > MAX_TITLE:
        raise UKBError("PAYLOAD_INVALID", "title too long", max=MAX_TITLE)
    return v


def _sources(conn, items, required):
    if items is None:
        items = []
    if not isinstance(items, list):
        raise UKBError("SOURCE_INVALID", "sources must be a list")
    if required and not items:
        raise UKBError("SOURCE_REQUIRED", "at least one source is required")
    out, seen = [], set()
    for i, s in enumerate(items):
        if not isinstance(s, dict):
            raise UKBError("SOURCE_INVALID", "source must be an object", index=i)
        st = conn.execute("SELECT * FROM reg_source_type WHERE source_type_id=? AND status='active'",
                          (s.get("source_type"),)).fetchone()
        if st is None:
            raise UKBError("SOURCE_INVALID", "unknown or inactive source_type", index=i,
                           source_type=s.get("source_type"))
        for k in ("title", "locator"):
            if not isinstance(s.get(k), str) or not s[k].strip():
                raise UKBError("SOURCE_INVALID", "source needs %s" % k, index=i)
        role = s.get("role", "supports")
        if role not in SOURCE_ROLES:
            raise UKBError("SOURCE_INVALID", "invalid source role", index=i, role=role)
        grade = s.get("grade", st["default_grade"])
        if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 9:
            raise UKBError("SOURCE_INVALID", "grade must be 0..9", index=i)
        sid = "s" + hash_obj([st["source_type_id"], s["locator"]])[:20]
        key = (sid, role)
        if key in seen:
            raise UKBError("SOURCE_INVALID", "duplicate source", index=i)
        seen.add(key)
        out.append({"source_id": sid, "source_type_id": st["source_type_id"], "title": s["title"],
                    "locator": s["locator"], "grade": grade,
                    "independence_key": s.get("independence_key") or "%s:%s" % (st["source_type_id"], s["locator"]),
                    "role": role, "locator_detail": s.get("locator_detail"), "excerpt": s.get("excerpt")})
    out.sort(key=lambda x: (x["source_id"], x["role"]))
    return out


def _check_cond_value(key, op, value):
    vt = key["value_type"]
    if op == "exists":
        if value not in (None, True):
            raise UKBError("CONDITION_INVALID", "exists takes no value", key=key["key_id"])
        return
    if op == "range":
        if not isinstance(value, list) or len(value) != 2:
            raise UKBError("CONDITION_INVALID", "range needs [min, max]", key=key["key_id"])
        for v in value:
            _scalar(key, v)
        if value[0] > value[1]:
            raise UKBError("CONDITION_INVALID", "range min > max", key=key["key_id"])
        return
    if op == "in":
        if not isinstance(value, list) or not value:
            raise UKBError("CONDITION_INVALID", "in needs a non-empty list", key=key["key_id"])
        for v in value:
            _scalar(key, v)
        return
    if op in ("lt", "le", "gt", "ge") and vt not in ("number", "integer"):
        raise UKBError("CONDITION_INVALID", "ordering operators need a numeric key", key=key["key_id"])
    _scalar(key, value)


def _scalar(key, v):
    vt = key["value_type"]
    ok = {
        "number": isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
        "integer": isinstance(v, int) and not isinstance(v, bool),
        "text": isinstance(v, str) and bool(v.strip()),
        "bool": isinstance(v, bool),
        "enum": isinstance(v, str) and v in json.loads(key["enum_json"] or "[]"),
    }[vt]
    if not ok:
        raise UKBError("CONDITION_INVALID", "value does not match key type", key=key["key_id"], value_type=vt)


def _conditions(conn, items):
    if items is None:
        return []
    if not isinstance(items, list):
        raise UKBError("CONDITION_INVALID", "conditions must be a list")
    out, seen = [], set()
    for i, c in enumerate(items):
        if not isinstance(c, dict):
            raise UKBError("CONDITION_INVALID", "condition must be an object", index=i)
        key = conn.execute("SELECT * FROM reg_condition_key WHERE key_id=? AND status='active'",
                           (c.get("key"),)).fetchone()
        if key is None:
            raise UKBError("CONDITION_INVALID", "unknown or inactive condition key", key=c.get("key"))
        op = c.get("op", "eq")
        if op not in COND_OPS:
            raise UKBError("CONDITION_INVALID", "invalid operator", op=op)
        _check_cond_value(key, op, c.get("value"))
        unit = c.get("unit")
        if key["unit"] and unit is not None and unit != key["unit"]:
            raise UKBError("CONDITION_INVALID", "unit differs from the registered unit; convert first",
                           key=key["key_id"], unit=unit, registered=key["unit"])
        if (key["key_id"], op) in seen:
            raise UKBError("CONDITION_INVALID", "duplicate condition", key=key["key_id"], op=op)
        seen.add((key["key_id"], op))
        out.append({"key": key["key_id"], "op": op, "value": c.get("value"), "unit": key["unit"] or unit,
                    "note": c.get("note")})
    out.sort(key=lambda x: (x["key"], x["op"]))
    return out


def resolve_tag_proposals(conn, items, required_facets, existing_entry_id=None):
    """Returns list of {"facet", "tag_id"|None, "new_tag"|None, "reason"}."""
    if items is None:
        items = []
    if not isinstance(items, list):
        raise UKBError("TAG_INVALID", "tags must be a list")
    out, seen, per_facet = [], set(), {}
    for i, t in enumerate(items):
        if not isinstance(t, dict):
            raise UKBError("TAG_INVALID", "tag must be an object", index=i)
        f = registry.facet(conn, t.get("facet"))
        if f["status"] != "active":
            raise UKBError("TAG_INVALID", "facet is deprecated", facet=f["facet_id"])
        reason = t.get("reason")
        if "new_tag" in t:
            nt = t["new_tag"]
            if not isinstance(nt, dict):
                raise UKBError("TAG_INVALID", "new_tag must be an object", index=i)
            slug = nt.get("slug")
            if not isinstance(slug, str) or not _SLUG_RE.match(slug):
                raise UKBError("TAG_INVALID", "new_tag.slug invalid", slug=slug)
            old = conn.execute("SELECT * FROM tag WHERE facet_id=? AND slug=?", (f["facet_id"], slug)).fetchone()
            if old is not None and old["status"] in ("active", "candidate"):
                raise UKBError("TAG_EXISTS", "tag already exists; reference it instead", facet=f["facet_id"],
                               slug=slug)
            if old is not None and old["status"] == "merged":
                raise UKBError("TAG_EXISTS", "this name was merged into another tag; use that tag", facet=f["facet_id"],
                               slug=slug)
            for name in [nt.get("label") or ""] + list(nt.get("aliases") or []):
                hit = registry.name_owner(conn, f["facet_id"], name) if name.strip() else None
                if hit is not None:
                    raise UKBError("TAG_EXISTS", "an existing tag already has this name or alias; use it",
                                   facet=f["facet_id"], name=name, tag=hit["slug"])
            for k in ("label", "description"):
                if not isinstance(nt.get(k), str) or not nt[k].strip():
                    raise UKBError("TAG_INVALID", "new_tag needs %s" % k, slug=slug)
            if not isinstance(reason, str) or not reason.strip():
                raise UKBError("TAG_INVALID", "a new tag needs a reason", slug=slug)
            parent = None
            if nt.get("parent"):
                parent = registry.resolve_tag(conn, f["facet_id"], nt["parent"])["tag_id"]
            al = nt.get("aliases", [])
            if not isinstance(al, list) or not all(isinstance(a, str) and a.strip() for a in al):
                raise UKBError("TAG_INVALID", "aliases must be strings", slug=slug)
            key = (f["facet_id"], "new:" + slug)
            rec = {"facet": f["facet_id"], "tag_id": None, "reason": reason, "revive_tag_id": old["tag_id"] if old else None,
                   "new_tag": {"slug": slug, "label": nt["label"], "description": nt["description"],
                               "parent_tag_id": parent, "aliases": al}}
        else:
            row = registry.resolve_tag(conn, f["facet_id"], t.get("tag"))
            if row["status"] not in ("active", "candidate"):
                raise UKBError("TAG_DEPRECATED", "tag is deprecated", facet=f["facet_id"], tag=row["slug"])
            key = (f["facet_id"], row["tag_id"])
            rec = {"facet": f["facet_id"], "tag_id": row["tag_id"], "new_tag": None, "reason": reason}
        if key in seen:
            raise UKBError("TAG_INVALID", "duplicate tag", facet=key[0])
        seen.add(key)
        per_facet[f["facet_id"]] = per_facet.get(f["facet_id"], 0) + 1
        if not f["multi"]:
            existing = 0
            if existing_entry_id:
                existing = conn.execute(
                    "SELECT count(*) FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id WHERE et.entry_id=? "
                    "AND t.facet_id=? AND et.state IN ('confirmed','suggested')",
                    (existing_entry_id, f["facet_id"])).fetchone()[0]
            if per_facet[f["facet_id"]] + existing > 1:
                raise UKBError("TAG_INVALID", "facet allows a single tag", facet=f["facet_id"])
        out.append(rec)
    have = set(per_facet)
    if existing_entry_id:
        for r in conn.execute("SELECT DISTINCT t.facet_id FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id "
                              "WHERE et.entry_id=? AND et.state IN ('confirmed','suggested')", (existing_entry_id,)):
            have.add(r[0])
    missing = [f for f in required_facets if f not in have]
    if missing:
        raise UKBError("TAG_REQUIRED", "type requires tags on these facets", facets=missing)
    return out


def write_tag_proposals(conn, entry_id, props, origin, actor):
    for p in props:
        tag_id = p["tag_id"]
        if tag_id is None:
            nt = p["new_tag"]
            if p.get("revive_tag_id"):
                tag_id = p["revive_tag_id"]
                conn.execute("UPDATE tag SET label=?, description=?, parent_tag_id=?, status='candidate', created_by=?, "
                             "created_at=? WHERE tag_id=?", (nt["label"], nt["description"], nt["parent_tag_id"], actor,
                                                              now(), tag_id))
            else:
                cur = conn.execute(
                    "INSERT INTO tag(facet_id,slug,label,description,parent_tag_id,status,merged_into,created_by,"
                    "created_at) VALUES (?,?,?,?,?,'candidate',NULL,?,?)",
                    (p["facet"], nt["slug"], nt["label"], nt["description"], nt["parent_tag_id"], actor, now()))
                tag_id = cur.lastrowid
            from .canon import alias_norm
            for a in [nt["label"]] + list(nt["aliases"]):
                conn.execute("INSERT OR IGNORE INTO tag_alias(alias_norm,tag_id,lang) VALUES (?,?,NULL)",
                             (alias_norm(a), tag_id))
            audit.append(conn, actor, "tag.candidate", "tag", str(tag_id), {"facet": p["facet"], "slug": nt["slug"]})
        prev = conn.execute("SELECT state FROM entry_tag WHERE entry_id=? AND tag_id=?", (entry_id, tag_id)).fetchone()
        if prev is not None and prev["state"] == "confirmed":
            continue
        conn.execute("INSERT INTO entry_tag(entry_id,tag_id,state,origin,reason,decided_by,updated_at) "
                     "VALUES (?,?,'suggested',?,?,NULL,?) ON CONFLICT(entry_id,tag_id) DO UPDATE SET "
                     "state='suggested', origin=excluded.origin, reason=excluded.reason, decided_by=NULL, "
                     "updated_at=excluded.updated_at", (entry_id, tag_id, origin, p.get("reason"), now()))


def risk_hits(conn, type_id, title, texts, tag_keys):
    hits = []
    blob = "\n".join([title] + list(texts)).casefold()
    full = "\n".join([title] + list(texts))
    for r in conn.execute("SELECT * FROM risk_rule WHERE status='active' ORDER BY rule_id"):
        mk, pat = r["match_kind"], r["pattern"]
        hit = (mk == "keyword" and pat.casefold() in blob) or \
              (mk == "regex" and re.search(pat, full) is not None) or \
              (mk == "type" and pat == type_id) or \
              (mk == "tag" and pat in tag_keys)
        if hit:
            hits.append({"rule_id": r["rule_id"], "action": r["action"], "note": r["note"]})
    return hits


def content_hash(type_id, type_version, title, fields, conditions, sources, mhash):
    return hash_obj({"type_id": type_id, "type_version": type_version, "title": title, "fields": fields,
                     "conditions": conditions,
                     "sources": [[s["source_id"], s["role"], s.get("locator_detail"), s.get("excerpt")]
                                 for s in sources],
                     "manifest_hash": mhash})


def body_hash(type_id, title, fields, conditions, mhash):
    return hash_obj({"type_id": type_id, "title": title, "fields": fields, "conditions": conditions,
                     "manifest_hash": mhash})


def load_rev_files(conn, rev_id):
    files = [dict(r) for r in conn.execute("SELECT * FROM doc_file WHERE rev_id=? ORDER BY path", (rev_id,))]
    for f in files:
        f["sections"] = [dict(s) for s in conn.execute(
            "SELECT section_id,title,start_char,end_char FROM doc_section WHERE rev_id=? AND path=? ORDER BY ord",
            (rev_id, f["path"]))]
        f["reusable"] = bool(f["reusable"])
    return files


def load_rev_sources(conn, rev_id):
    return [dict(r) for r in conn.execute(
        "SELECT s.source_id, s.source_type_id, s.title, s.locator, s.grade, s.independence_key, es.role, "
        "es.locator_detail, es.excerpt FROM entry_source es JOIN source s ON s.source_id=es.source_id "
        "WHERE es.rev_id=? ORDER BY s.source_id, es.role", (rev_id,))]


def load_rev_conditions(conn, rev_id):
    return [{"key": r["key_id"], "op": r["op"], "value": json.loads(r["value_json"]), "unit": r["unit"],
             "note": r["note"]}
            for r in conn.execute("SELECT * FROM entry_condition WHERE rev_id=? ORDER BY key_id, op", (rev_id,))]


def build_revision(conn, blobs, tspec, payload, parent_rev, intake_roots, pdf_extractor):
    """Validates content and returns a dict with everything needed to persist a revision."""
    spec = tspec["spec"]
    title = _title(payload.get("title"))
    fields = validate_fields(spec, payload.get("fields"))
    conditions = _conditions(conn, payload.get("conditions"))
    sources = _sources(conn, payload.get("sources"), required=False)
    texts = list(walk_text(spec["fields"], fields))
    files, mhash = None, None
    if spec.get("tier") == "document":
        parent_files = load_rev_files(conn, parent_rev) if parent_rev else None
        files = documents.prepare_files(conn, blobs, tspec, payload.get("files"), parent_files, intake_roots,
                                        pdf_extractor)
        char_count = documents.check_summary(tspec, texts, files)
        mhash = documents.manifest_hash(files)
    else:
        if payload.get("files"):
            raise UKBError("PAYLOAD_INVALID", "files are only allowed for document-tier types")
        char_count = sum(len(t) for t in texts)
    char_count += len(title)
    ch = content_hash(tspec["type_id"], tspec["version"], title, fields, conditions, sources, mhash)
    bh = body_hash(tspec["type_id"], title, fields, conditions, mhash)
    return {"title": title, "fields": fields, "conditions": conditions, "sources": sources, "files": files,
            "manifest_hash": mhash, "content_hash": ch, "body_hash": bh, "char_count": char_count, "texts": texts}


def persist_revision(conn, entry_id, rev_no, tspec, built, parent_rev, author, change_note):
    rev_id = new_id("r")
    conn.execute(
        "INSERT INTO entry_revision(rev_id,entry_id,rev_no,type_id,type_version,title,fields_json,char_count,"
        "manifest_hash,body_hash,content_hash,parent_rev,author,change_note,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rev_id, entry_id, rev_no, tspec["type_id"], tspec["version"], built["title"],
         canonical_json(built["fields"]), built["char_count"], built["manifest_hash"], built["body_hash"],
         built["content_hash"], parent_rev, author, change_note, now()))
    for c in built["conditions"]:
        conn.execute("INSERT INTO entry_condition(rev_id,key_id,op,value_json,unit,note) VALUES (?,?,?,?,?,?)",
                     (rev_id, c["key"], c["op"], canonical_json(c["value"]), c["unit"], c["note"]))
    for s in built["sources"]:
        upsert_source(conn, s)
        conn.execute("INSERT INTO entry_source(rev_id,source_id,role,locator_detail,excerpt) VALUES (?,?,?,?,?)",
                     (rev_id, s["source_id"], s["role"], s.get("locator_detail"), s.get("excerpt")))
    for f in built["files"] or []:
        for sha, size in ((f["sha256"], f["size"]), (f["text_sha256"], None)):
            if sha:
                conn.execute("INSERT OR IGNORE INTO blob(sha256,size,created_at) VALUES (?,?,?)",
                             (sha, size if size is not None else -1, now()))
        conn.execute(
            "INSERT INTO doc_file(rev_id,path,role_id,format_id,description,sha256,size,text_sha256,char_count,"
            "line_count,page_count,reusable,license_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rev_id, f["path"], f["role_id"], f["format_id"], f["description"], f["sha256"], f["size"],
             f["text_sha256"], f["char_count"], f["line_count"], f["page_count"], int(f["reusable"]),
             f.get("license_note")))
        for i, s in enumerate(f["sections"]):
            conn.execute("INSERT INTO doc_section(rev_id,path,section_id,title,ord,start_char,end_char) "
                         "VALUES (?,?,?,?,?,?,?)", (rev_id, f["path"], s["section_id"], s["title"], i,
                                                    s["start_char"], s["end_char"]))
    if built["files"] is not None:
        if documents.manifest_hash(load_rev_files(conn, rev_id)) != built["manifest_hash"]:
            raise UKBError("INTERNAL", "manifest hash mismatch after write")
    return rev_id


def upsert_source(conn, s):
    conn.execute("INSERT OR IGNORE INTO source(source_id,source_type_id,title,locator,grade,independence_key,added_at)"
                 " VALUES (?,?,?,?,?,?,?)", (s["source_id"], s["source_type_id"], s["title"], s["locator"],
                                             s["grade"], s["independence_key"], now()))


def open_task(conn, kind, submitter, bound_hash, *, intake_id=None, entry_id=None, rev_id=None,
              payload=None, risk=None, exact_dup=None):
    task_id = new_id("t")
    conn.execute(
        "INSERT INTO review_task(task_id,kind,intake_id,entry_id,rev_id,submitter,payload_json,bound_hash,"
        "prereview_required,risk_hits_json,exact_dup_of,state,opened_at,closed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?,NULL)",
        (task_id, kind, intake_id, entry_id, rev_id, submitter,
         canonical_json(payload) if payload is not None else None, bound_hash,
         int(kind in PREREVIEW_KINDS), canonical_json(risk) if risk else None, exact_dup, now()))
    return task_id


def entry_has_open_task(conn, entry_id, kinds=None):
    q = "SELECT task_id FROM review_task WHERE entry_id=? AND state='open'"
    args = [entry_id]
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += list(kinds)
    row = conn.execute(q, args).fetchone()
    return row["task_id"] if row else None


def _origin(payload, channel):
    """Where the knowledge came from. Required for AI submissions so the admin and prereviewer can trace it."""
    o = payload.get("origin")
    if o is None:
        if channel == "mcp":
            raise UKBError("ORIGIN_REQUIRED", "AI submissions need origin {kind, ref, summary_of_evidence}",
                           kinds=list(CONTEXT_KINDS))
        return None
    if not isinstance(o, dict) or set(o) - {"kind", "ref", "summary_of_evidence", "excerpt", "task"}:
        raise UKBError("ORIGIN_INVALID", "origin must be {kind, ref, summary_of_evidence, excerpt?, task?}")
    if o.get("kind") not in CONTEXT_KINDS:
        raise UKBError("ORIGIN_INVALID", "origin.kind unknown", kinds=list(CONTEXT_KINDS))
    for k in ("ref", "summary_of_evidence"):
        if not isinstance(o.get(k), str) or not o[k].strip():
            raise UKBError("ORIGIN_INVALID", "origin.%s is required" % k)
    if len(canonical_json(o)) > 8000:
        raise UKBError("ORIGIN_INVALID", "origin is too long; cite, do not paste")
    return o


def _rate_limit(conn, channel, submitter):
    if channel != "mcp":
        return
    limit = registry.setting(conn, "agent_daily_proposal_limit")
    if not limit:
        return
    import datetime
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat(
        timespec="milliseconds")
    n = conn.execute("SELECT count(*) FROM intake WHERE submitter=? AND channel='mcp' AND state<>'invalid' "
                     "AND received_at>?", (submitter, since)).fetchone()[0]
    if n >= limit:
        raise UKBError("RATE_LIMITED", "daily proposal limit reached for this agent; batch or prioritise",
                       limit=limit)


def propose(conn, blobs, payload, channel, submitter, pdf_extractor=None):
    """Must run inside a write transaction. Returns result dict."""
    if channel not in CHANNELS:
        raise UKBError("PAYLOAD_INVALID", "unknown channel", channel=channel)
    if not isinstance(submitter, str) or not submitter.strip():
        raise UKBError("PAYLOAD_INVALID", "submitter is required")
    if not isinstance(payload, dict):
        raise UKBError("PAYLOAD_INVALID", "payload must be an object")
    raw = payload
    files_raw = payload.get("files")
    payload = normalize({k: v for k, v in payload.items() if k != "files"})
    if files_raw is not None:
        payload["files"] = _normalize_files(files_raw)
    mode = payload.get("mode", "new")
    if mode not in MODES:
        raise UKBError("PAYLOAD_INVALID", "mode must be new, revise or reinforce", mode=mode)
    allowed = {"mode", "type_id", "target_entry_id", "base_rev_no", "title", "fields", "tags", "sources",
               "conditions", "files", "change_note", "note", "origin"}
    extra = set(payload) - allowed
    if extra:
        raise UKBError("PAYLOAD_INVALID", "unknown payload keys", keys=sorted(extra))
    origin = _origin(payload, channel)
    _rate_limit(conn, channel, submitter)
    intake_id = new_id("i")
    intake_roots = registry.setting(conn, "intake_roots")
    target = None
    if mode in ("revise", "reinforce"):
        target = conn.execute("SELECT * FROM entry WHERE entry_id=?", (payload.get("target_entry_id"),)).fetchone()
        if target is None:
            raise UKBError("ENTRY_UNKNOWN", "target_entry_id not found", entry_id=payload.get("target_entry_id"))
        if payload.get("type_id") not in (None, target["type_id"]):
            raise UKBError("PAYLOAD_INVALID", "the type of an entry cannot change; propose a new entry")
    result = {"ok": True, "intake_id": intake_id, "mode": mode}

    if mode == "reinforce":
        if target["status"] not in ("active", "trial"):
            raise UKBError("STATUS_INVALID", "only active or trial entries can be reinforced", status=target["status"])
        sources = _sources(conn, payload.get("sources"), required=True)
        note = payload.get("note")
        if not isinstance(note, str) or not note.strip():
            raise UKBError("PAYLOAD_INVALID", "reinforce needs a note explaining what the new sources confirm")
        task_payload = {"sources": sources, "note": note}
        _insert_intake(conn, intake_id, channel, submitter, mode, raw, "queued", target["entry_id"], origin=origin)
        task_id = open_task(conn, "reinforce", submitter, hash_obj(task_payload), intake_id=intake_id,
                            entry_id=target["entry_id"], rev_id=target["head_rev"], payload=task_payload)
        audit.append(conn, submitter, "propose.reinforce", "entry", target["entry_id"], {"task_id": task_id})
        result.update(task_id=task_id, entry_id=target["entry_id"])
        return result

    if mode == "new":
        tspec = registry.type_spec(conn, payload.get("type_id"))
        if tspec["status"] != "active":
            raise UKBError("TYPE_DEPRECATED", "type is deprecated", type_id=tspec["type_id"])
        parent_rev, entry_id, rev_no = None, new_id("e"), 1
        tag_props = resolve_tag_proposals(conn, payload.get("tags"), tspec["spec"].get("required_facets", []))
    else:
        tspec = registry.type_spec(conn, target["type_id"])
        if tspec["status"] != "active":
            raise UKBError("TYPE_DEPRECATED", "type is deprecated", type_id=tspec["type_id"])
        if target["status"] in ("revoked", "rejected", "superseded"):
            raise UKBError("STATUS_INVALID", "this entry can no longer be revised", status=target["status"])
        busy = entry_has_open_task(conn, target["entry_id"], ("new", "revise"))
        if busy:
            raise UKBError("TASK_CONFLICT", "entry already has an open content review", task_id=busy)
        entry_id = target["entry_id"]
        head = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (target["head_rev"],)).fetchone() \
            if target["head_rev"] else None
        last = conn.execute("SELECT * FROM entry_revision WHERE entry_id=? ORDER BY rev_no DESC LIMIT 1",
                            (entry_id,)).fetchone()
        base_no = payload.get("base_rev_no")
        expect = head["rev_no"] if head else last["rev_no"]
        if base_no is not None and base_no != expect:
            raise UKBError("STALE_BASE", "entry changed since you read it", base_rev_no=base_no, current=expect)
        parent_rev = (head or last)["rev_id"]
        rev_no = last["rev_no"] + 1
        tag_props = resolve_tag_proposals(conn, payload.get("tags"), tspec["spec"].get("required_facets", []),
                                          existing_entry_id=entry_id)

    built = build_revision(conn, blobs, tspec, payload, parent_rev, intake_roots, pdf_extractor)
    if tspec["spec"].get("require_sources", True):
        n_support = len({s["independence_key"] for s in built["sources"] if s["role"] in ("supports", "derived_from")})
        if n_support < 1:
            raise UKBError("SOURCE_REQUIRED", "this type needs at least one supporting source")

    if mode == "revise":
        cmp_rev = conn.execute("SELECT content_hash FROM entry_revision WHERE rev_id=?", (parent_rev,)).fetchone()
        if cmp_rev["content_hash"] == built["content_hash"] and not tag_props:
            raise UKBError("NO_CHANGE", "revision is identical to the current one")
    dup = conn.execute("SELECT r.rev_id FROM entry_revision r JOIN entry e ON e.entry_id=r.entry_id WHERE r.body_hash=? "
                       "AND r.entry_id<>? AND e.status NOT IN ('rejected','revoked') ORDER BY r.created_at LIMIT 1",
                       (built["body_hash"], entry_id)).fetchone()

    tag_keys = set()
    for p in tag_props:
        slug = p["new_tag"]["slug"] if p["new_tag"] else \
            conn.execute("SELECT slug FROM tag WHERE tag_id=?", (p["tag_id"],)).fetchone()[0]
        tag_keys.add("%s/%s" % (p["facet"], slug))
    risk = risk_hits(conn, tspec["type_id"], built["title"], built["texts"], tag_keys)
    quarantine = any(h["action"] == "quarantine" for h in risk)

    _insert_intake(conn, intake_id, channel, submitter, mode, raw, "queued", target["entry_id"] if target else None,
                   origin=origin)
    if mode == "new":
        conn.execute("INSERT INTO entry(entry_id,type_id,status,head_rev,status_note,created_by,created_at,updated_at)"
                     " VALUES (?,?,?,NULL,?,?,?,?)",
                     (entry_id, tspec["type_id"], "quarantined" if quarantine else "candidate",
                      "risk rule hit" if quarantine else None, submitter, now(), now()))
    rev_id = persist_revision(conn, entry_id, rev_no, tspec, built, parent_rev, submitter,
                              payload.get("change_note"))
    write_tag_proposals(conn, entry_id, tag_props, "submitter", submitter)
    if mode == "revise" and quarantine and target["status"] == "candidate":
        conn.execute("UPDATE entry SET status='quarantined', status_note='risk rule hit', updated_at=? "
                     "WHERE entry_id=?", (now(), entry_id))
    conn.execute("UPDATE intake SET result_entry_id=? WHERE intake_id=?", (entry_id, intake_id))
    task_id = open_task(conn, mode, submitter, built["content_hash"], intake_id=intake_id, entry_id=entry_id,
                        rev_id=rev_id, risk=risk, exact_dup=dup["rev_id"] if dup else None)
    audit.append(conn, submitter, "propose." + mode, "entry", entry_id,
                 {"rev_id": rev_id, "task_id": task_id, "content_hash": built["content_hash"]})
    result.update(entry_id=entry_id, rev_id=rev_id, rev_no=rev_no, task_id=task_id,
                  content_hash=built["content_hash"], exact_duplicate_of=dup["rev_id"] if dup else None,
                  risk_hits=[h["rule_id"] for h in risk], quarantined=quarantine)
    return result


_FILE_VERBATIM_KEYS = ("content_text", "content_b64", "text_content_text", "text_content_b64")


def _normalize_files(files):
    """Normalise file metadata but keep file contents byte-exact (indentation and trailing newlines matter)."""
    if not isinstance(files, list):
        return files
    out = []
    for f in files:
        if not isinstance(f, dict):
            out.append(f)
            continue
        meta = normalize({k: v for k, v in f.items() if k not in _FILE_VERBATIM_KEYS})
        meta.update({k: f[k] for k in _FILE_VERBATIM_KEYS if k in f})
        out.append(meta)
    return out


def _insert_intake(conn, intake_id, channel, submitter, mode, raw, state, target, issues=None, origin=None):
    stored = _raw_for_storage(raw)
    conn.execute("INSERT INTO intake(intake_id,received_at,channel,submitter,mode,context_json,raw_json,raw_hash,state,"
                 "target_entry_id,result_entry_id,issues_json) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)",
                 (intake_id, now(), channel, submitter, str(mode)[:20],
                  canonical_json(origin) if origin else None, canonical_json(stored), hash_obj(stored), state,
                  target, canonical_json(issues) if issues else None))


def record_invalid(conn, payload, channel, submitter, err):
    intake_id = new_id("i")
    target = payload.get("target_entry_id") if isinstance(payload, dict) else None
    if target and conn.execute("SELECT 1 FROM entry WHERE entry_id=?", (target,)).fetchone() is None:
        target = None
    mode = payload.get("mode", "new") if isinstance(payload, dict) else "?"
    raw = payload if isinstance(payload, dict) else {"_raw": str(payload)[:2000]}
    _insert_intake(conn, intake_id, channel if channel in CHANNELS else "api", str(submitter or "?")[:120],
                   mode, raw, "invalid", target, issues=err.to_dict())
    audit.append(conn, str(submitter or "?")[:120], "propose.invalid", "intake", intake_id, {"error": err.code})
    return intake_id
