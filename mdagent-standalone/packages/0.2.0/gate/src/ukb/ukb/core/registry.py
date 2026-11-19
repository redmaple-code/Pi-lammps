"""Registries: read access, seed loading and approved registry changes.

All concrete values (types, facets, tags, formats, policies, ...) live in data files and in these
tables. Nothing domain-specific is hard-coded here.
"""
import fnmatch
import json
import os
import re

from . import audit
from .canon import alias_norm, canonical_json, now
from .errors import UKBError
from .typespec import validate_spec

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
STATUS = ("active", "deprecated")

REGISTRY_KINDS = ("type", "facet", "tag", "condition_key", "relation", "source_type",
                  "profile", "risk_rule", "artifact_role", "file_format", "file_deny", "setting")

DEFAULT_SETTINGS = {
    "max_file_bytes": 10 * 1024 * 1024,
    "max_entry_bytes": 50 * 1024 * 1024,
    "max_files_per_entry": 200,
    "intake_roots": [],
    "checkout_roots": [],
    "pdf_text_extraction": True,
    "feedback_recheck_threshold": 1,
    "agent_daily_proposal_limit": 50,
}


def _check_id(value, what):
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise UKBError("REGISTRY_INVALID", "invalid %s id" % what, value=value)


def _req_str(spec, key, what):
    v = spec.get(key)
    if not isinstance(v, str) or not v.strip():
        raise UKBError("REGISTRY_INVALID", "%s needs non-empty %s" % (what, key))
    return v.strip()


def _status(spec):
    s = spec.get("status", "active")
    if s not in STATUS:
        raise UKBError("REGISTRY_INVALID", "status must be active or deprecated", status=s)
    return s


# ---------- read access ----------

def setting(conn, key):
    row = conn.execute("SELECT value_json FROM reg_setting WHERE key=?", (key,)).fetchone()
    if row is not None:
        return json.loads(row["value_json"])
    if key in DEFAULT_SETTINGS:
        return DEFAULT_SETTINGS[key]
    raise UKBError("CONFIG_INVALID", "unknown setting", key=key)


def ids(conn, table, col, active_only=True):
    q = "SELECT %s FROM %s" % (col, table)
    if active_only:
        q += " WHERE status='active'"
    return {r[0] for r in conn.execute(q)}


def type_spec(conn, type_id, version=None):
    t = conn.execute("SELECT * FROM reg_type WHERE type_id=?", (type_id,)).fetchone()
    if t is None:
        raise UKBError("TYPE_UNKNOWN", "unknown type", type_id=type_id)
    v = version if version is not None else t["current_version"]
    row = conn.execute("SELECT * FROM reg_type_version WHERE type_id=? AND version=?", (type_id, v)).fetchone()
    if row is None:
        raise UKBError("TYPE_UNKNOWN", "unknown type version", type_id=type_id, version=v)
    spec = json.loads(row["schema_json"])
    return {"type_id": type_id, "version": v, "status": t["status"], "label": t["label"],
            "description": t["description"], "spec": spec}


def profile(conn, profile_id):
    row = conn.execute("SELECT * FROM consumer_profile WHERE profile_id=?", (profile_id,)).fetchone()
    if row is None or row["status"] != "active":
        raise UKBError("PROFILE_UNKNOWN", "unknown or inactive consumer profile", profile_id=profile_id)
    spec = json.loads(row["spec_json"])
    spec["profile_id"] = profile_id
    return spec


def facet(conn, facet_id):
    row = conn.execute("SELECT * FROM reg_facet WHERE facet_id=?", (facet_id,)).fetchone()
    if row is None:
        raise UKBError("FACET_UNKNOWN", "unknown facet", facet_id=facet_id)
    return row


def resolve_tag(conn, facet_id, ref):
    """ref may be tag_id (int), slug or alias. Follows merges. Returns tag row or raises TAG_UNKNOWN."""
    facet(conn, facet_id)
    row = None
    if isinstance(ref, int) and not isinstance(ref, bool):
        row = conn.execute("SELECT * FROM tag WHERE tag_id=? AND facet_id=?", (ref, facet_id)).fetchone()
    elif isinstance(ref, str) and ref.strip():
        row = conn.execute("SELECT * FROM tag WHERE facet_id=? AND slug=?", (facet_id, ref.strip())).fetchone()
        if row is None:
            hits = conn.execute(
                "SELECT DISTINCT t.* FROM tag t LEFT JOIN tag_alias a ON t.tag_id=a.tag_id WHERE t.facet_id=? "
                "AND t.status<>'deprecated' AND (a.alias_norm=? OR lower(t.label)=?)",
                (facet_id, alias_norm(ref), alias_norm(ref))).fetchall()
            if len(hits) > 1:
                raise UKBError("TAG_AMBIGUOUS", "alias matches several tags", facet=facet_id, ref=ref,
                               tags=[h["slug"] for h in hits])
            row = hits[0] if hits else None
    if row is None:
        raise UKBError("TAG_UNKNOWN", "tag not found", facet=facet_id, ref=ref)
    seen = set()
    while row["status"] == "merged":
        if row["tag_id"] in seen:
            raise UKBError("INTERNAL", "tag merge cycle", tag_id=row["tag_id"])
        seen.add(row["tag_id"])
        row = conn.execute("SELECT * FROM tag WHERE tag_id=?", (row["merged_into"],)).fetchone()
    return row


def tag_descendants(conn, tag_id):
    rows = conn.execute(
        "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT t.tag_id FROM tag t JOIN d ON t.parent_tag_id=d.id) "
        "SELECT id FROM d", (tag_id,)).fetchall()
    return [r[0] for r in rows]


def tag_aliases(conn, tag_id):
    return [r[0] for r in conn.execute("SELECT alias_norm FROM tag_alias WHERE tag_id=? ORDER BY alias_norm",
                                       (tag_id,))]


def match_file_format(conn, filename):
    """Deny list first, then allow list. Returns format row; raises FILE_TYPE_DENIED / FILE_TYPE_NOT_ALLOWED."""
    base = filename.rsplit("/", 1)[-1].lower()
    for r in conn.execute("SELECT * FROM reg_file_deny WHERE status='active' ORDER BY pattern"):
        if fnmatch.fnmatchcase(base, r["pattern"].lower()):
            raise UKBError("FILE_TYPE_DENIED", "file type is not allowed in the knowledge base",
                           path=filename, pattern=r["pattern"], reason=r["reason"])
    hits = []
    for r in conn.execute("SELECT * FROM reg_file_format WHERE status='active' ORDER BY format_id"):
        pats = json.loads(r["patterns_json"])
        if any(fnmatch.fnmatchcase(base, p.lower()) for p in pats):
            hits.append(r)
    if not hits:
        raise UKBError("FILE_TYPE_NOT_ALLOWED", "file type is not registered as allowed", path=filename)
    return hits[0]


# ---------- registry writes (bootstrap seed and approved changes) ----------

def validate_change(conn, kind, spec):
    """Validates without writing. Returns normalized spec."""
    if kind not in REGISTRY_KINDS:
        raise UKBError("REGISTRY_INVALID", "unknown registry kind", kind=kind)
    if not isinstance(spec, dict):
        raise UKBError("REGISTRY_INVALID", "spec must be an object")
    fn = globals()["_v_" + kind]
    return fn(conn, dict(spec))


def apply_change(conn, kind, spec, actor):
    spec = validate_change(conn, kind, spec)
    globals()["_a_" + kind](conn, spec, actor)
    audit.append(conn, actor, "registry." + kind, "registry", _key(kind, spec), spec)
    return spec


def _key(kind, spec):
    for k in ("type_id", "facet_id", "key_id", "rel_id", "source_type_id", "profile_id",
              "rule_id", "role_id", "format_id", "pattern", "key"):
        if k in spec:
            return "%s:%s" % (kind, spec[k])
    if kind == "tag":
        return "tag:%s/%s" % (spec["facet"], spec["slug"])
    return kind


def _v_type(conn, s):
    _check_id(s.get("type_id"), "type")
    _req_str(s, "label", "type")
    _req_str(s, "description", "type")
    s["status"] = _status(s)
    if "spec" in s:
        validate_spec(s["spec"],
                      known_facets=ids(conn, "reg_facet", "facet_id"),
                      known_roles=ids(conn, "reg_artifact_role", "role_id"),
                      known_formats=ids(conn, "reg_file_format", "format_id"))
    else:
        if conn.execute("SELECT 1 FROM reg_type WHERE type_id=?", (s["type_id"],)).fetchone() is None:
            raise UKBError("REGISTRY_INVALID", "new type needs spec")
    return s


def _a_type(conn, s, actor):
    row = conn.execute("SELECT * FROM reg_type WHERE type_id=?", (s["type_id"],)).fetchone()
    if "spec" in s:
        version = 1 if row is None else row["current_version"] + 1
        if row is not None:
            prev = type_spec(conn, s["type_id"])
            if canonical_json(prev["spec"]) == canonical_json(s["spec"]):
                version = row["current_version"]
        if row is None:
            conn.execute("INSERT INTO reg_type(type_id,label,description,current_version,status) VALUES (?,?,?,?,?)",
                         (s["type_id"], s["label"], s["description"], version, s["status"]))
        if row is None or version != row["current_version"]:
            conn.execute("INSERT INTO reg_type_version(type_id,version,schema_json,created_at) VALUES (?,?,?,?)",
                         (s["type_id"], version, canonical_json(s["spec"]), now()))
        conn.execute("UPDATE reg_type SET label=?, description=?, current_version=?, status=? WHERE type_id=?",
                     (s["label"], s["description"], version, s["status"], s["type_id"]))
    else:
        conn.execute("UPDATE reg_type SET label=?, description=?, status=? WHERE type_id=?",
                     (s["label"], s["description"], s["status"], s["type_id"]))


def _v_facet(conn, s):
    _check_id(s.get("facet_id"), "facet")
    _req_str(s, "label", "facet")
    _req_str(s, "description", "facet")
    for k in ("multi", "governing"):
        if k in s and not isinstance(s[k], bool):
            raise UKBError("REGISTRY_INVALID", "%s must be boolean" % k)
    b = s.get("scope_budget_chars")
    if b is not None and (not isinstance(b, int) or b < 1000):
        raise UKBError("REGISTRY_INVALID", "scope_budget_chars must be an integer >= 1000")
    s["status"] = _status(s)
    return s


def _a_facet(conn, s, actor):
    conn.execute(
        "INSERT INTO reg_facet(facet_id,label,description,multi,governing,scope_budget_chars,ord,status) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(facet_id) DO UPDATE SET label=excluded.label, "
        "description=excluded.description, multi=excluded.multi, governing=excluded.governing, "
        "scope_budget_chars=excluded.scope_budget_chars, ord=excluded.ord, status=excluded.status",
        (s["facet_id"], s["label"], s["description"], int(s.get("multi", True)), int(s.get("governing", False)),
         s.get("scope_budget_chars"), int(s.get("ord", 0)), s["status"]))


def _v_tag(conn, s):
    f = facet(conn, s.get("facet"))
    if f["status"] != "active":
        raise UKBError("REGISTRY_INVALID", "facet is deprecated", facet=s.get("facet"))
    _check_id(s.get("slug"), "tag slug")
    st = s.get("status", "active")
    if st not in ("candidate", "active", "deprecated", "merged"):
        raise UKBError("REGISTRY_INVALID", "invalid tag status", status=st)
    s["status"] = st
    existing = conn.execute("SELECT * FROM tag WHERE facet_id=? AND slug=?", (s["facet"], s["slug"])).fetchone()
    if existing is None:
        _req_str(s, "label", "tag")
        _req_str(s, "description", "tag")
    if st == "merged":
        if existing is None:
            raise UKBError("REGISTRY_INVALID", "only an existing tag can be merged")
        tgt = resolve_tag(conn, s["facet"], s.get("merge_into"))
        if tgt["tag_id"] == existing["tag_id"]:
            raise UKBError("REGISTRY_INVALID", "cannot merge a tag into itself")
        s["_merge_target"] = tgt["tag_id"]
    if s.get("parent"):
        p = resolve_tag(conn, s["facet"], s["parent"])
        if existing is not None and p["tag_id"] in tag_descendants(conn, existing["tag_id"]):
            raise UKBError("REGISTRY_INVALID", "parent would create a cycle")
        s["_parent_id"] = p["tag_id"]
    al = s.get("aliases", [])
    if not isinstance(al, list) or not all(isinstance(a, str) and a.strip() for a in al):
        raise UKBError("REGISTRY_INVALID", "aliases must be a list of strings")
    own = existing["tag_id"] if existing is not None else None
    for name in al + ([s["label"]] if s.get("label") else []):
        clash = name_owner(conn, s["facet"], name)
        if clash is not None and clash["tag_id"] != own and s["status"] != "merged":
            raise UKBError("REGISTRY_INVALID", "name or alias already used by another tag in this facet",
                           name=name, tag=clash["slug"])
    return s


def name_owner(conn, facet_id, name):
    """The live tag in this facet whose slug, label or alias equals name (case-insensitive), or None."""
    n = alias_norm(name)
    return conn.execute(
        "SELECT DISTINCT t.* FROM tag t LEFT JOIN tag_alias a ON a.tag_id=t.tag_id WHERE t.facet_id=? "
        "AND t.status IN ('active','candidate') AND (lower(t.slug)=? OR lower(t.label)=? OR a.alias_norm=?) LIMIT 1",
        (facet_id, n, n, n)).fetchone()


def _a_tag(conn, s, actor):
    existing = conn.execute("SELECT * FROM tag WHERE facet_id=? AND slug=?", (s["facet"], s["slug"])).fetchone()
    parent = s.get("_parent_id", existing["parent_tag_id"] if existing else None)
    if existing is None:
        cur = conn.execute(
            "INSERT INTO tag(facet_id,slug,label,description,parent_tag_id,status,merged_into,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,NULL,?,?)",
            (s["facet"], s["slug"], s["label"], s["description"], parent,
             s["status"] if s["status"] != "merged" else "active", actor, now()))
        tag_id = cur.lastrowid
    else:
        tag_id = existing["tag_id"]
        if s["status"] == "merged":
            tgt = s["_merge_target"]
            for r in conn.execute("SELECT * FROM entry_tag WHERE tag_id=?", (tag_id,)).fetchall():
                conn.execute("INSERT OR IGNORE INTO entry_tag(entry_id,tag_id,state,origin,reason,decided_by,updated_at)"
                             " VALUES (?,?,?,?,?,?,?)", (r["entry_id"], tgt, r["state"], r["origin"], r["reason"],
                                                         r["decided_by"], now()))
            conn.execute("DELETE FROM entry_tag WHERE tag_id=?", (tag_id,))
            conn.execute("UPDATE tag SET parent_tag_id=? WHERE parent_tag_id=?", (tgt, tag_id))
            conn.execute("INSERT OR IGNORE INTO tag_alias(alias_norm,tag_id,lang) VALUES (?,?,NULL)",
                         (alias_norm(existing["slug"]), tgt))
            conn.execute("INSERT OR IGNORE INTO tag_alias(alias_norm,tag_id,lang) VALUES (?,?,NULL)",
                         (alias_norm(existing["label"]), tgt))
            for a in conn.execute("SELECT alias_norm, lang FROM tag_alias WHERE tag_id=?", (tag_id,)).fetchall():
                conn.execute("INSERT OR IGNORE INTO tag_alias(alias_norm,tag_id,lang) VALUES (?,?,?)",
                             (a[0], tgt, a[1]))
            conn.execute("DELETE FROM tag_alias WHERE tag_id=?", (tag_id,))
            conn.execute("UPDATE tag SET status='merged', merged_into=? WHERE tag_id=?", (tgt, tag_id))
        else:
            conn.execute("UPDATE tag SET label=?, description=?, parent_tag_id=?, status=? WHERE tag_id=?",
                         (s.get("label", existing["label"]), s.get("description", existing["description"]),
                          parent, s["status"], tag_id))
    for a in s.get("aliases", []):
        conn.execute("INSERT OR IGNORE INTO tag_alias(alias_norm,tag_id,lang) VALUES (?,?,?)",
                     (alias_norm(a), tag_id, None))


def _v_condition_key(conn, s):
    _check_id(s.get("key_id"), "condition key")
    _req_str(s, "label", "condition key")
    if s.get("value_type") not in ("number", "integer", "text", "enum", "bool"):
        raise UKBError("REGISTRY_INVALID", "invalid value_type")
    if s["value_type"] == "enum":
        ev = s.get("enum")
        if not isinstance(ev, list) or not ev or not all(isinstance(x, str) for x in ev):
            raise UKBError("REGISTRY_INVALID", "enum key needs enum list")
    s["status"] = _status(s)
    return s


def _a_condition_key(conn, s, actor):
    conn.execute("INSERT INTO reg_condition_key(key_id,label,value_type,unit,enum_json,status) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(key_id) DO UPDATE SET label=excluded.label, value_type=excluded.value_type, "
                 "unit=excluded.unit, enum_json=excluded.enum_json, status=excluded.status",
                 (s["key_id"], s["label"], s["value_type"], s.get("unit"),
                  canonical_json(s["enum"]) if s.get("enum") else None, s["status"]))


def _v_relation(conn, s):
    _check_id(s.get("rel_id"), "relation")
    _req_str(s, "label", "relation")
    _req_str(s, "inverse_label", "relation")
    s["status"] = _status(s)
    return s


def _a_relation(conn, s, actor):
    conn.execute("INSERT INTO reg_relation(rel_id,label,inverse_label,symmetric,acyclic,status) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(rel_id) DO UPDATE SET label=excluded.label, inverse_label=excluded.inverse_label, "
                 "symmetric=excluded.symmetric, acyclic=excluded.acyclic, status=excluded.status",
                 (s["rel_id"], s["label"], s["inverse_label"], int(bool(s.get("symmetric"))),
                  int(bool(s.get("acyclic"))), s["status"]))


def _v_source_type(conn, s):
    _check_id(s.get("source_type_id"), "source type")
    _req_str(s, "label", "source type")
    g = s.get("default_grade", 0)
    if not isinstance(g, int) or not 0 <= g <= 9:
        raise UKBError("REGISTRY_INVALID", "default_grade must be 0..9")
    s["status"] = _status(s)
    return s


def _a_source_type(conn, s, actor):
    conn.execute("INSERT INTO reg_source_type(source_type_id,label,default_grade,status) VALUES (?,?,?,?) "
                 "ON CONFLICT(source_type_id) DO UPDATE SET label=excluded.label, "
                 "default_grade=excluded.default_grade, status=excluded.status",
                 (s["source_type_id"], s["label"], s.get("default_grade", 0), s["status"]))


ENTRY_STATUSES = ("candidate", "trial", "active", "deprecated", "superseded", "revoked", "rejected", "quarantined")


def _v_profile(conn, s):
    _check_id(s.get("profile_id"), "profile")
    spec = s.get("spec")
    if not isinstance(spec, dict):
        raise UKBError("REGISTRY_INVALID", "profile needs spec")
    allowed = {"label", "visible_statuses", "visible_types", "include_suggested_tags", "read_budget_chars",
               "must_read", "catalog_limit", "read_max_entries"}
    if set(spec) - allowed:
        raise UKBError("REGISTRY_INVALID", "unknown profile keys", keys=sorted(set(spec) - allowed))
    vs = spec.get("visible_statuses")
    if not isinstance(vs, list) or not vs or any(x not in ENTRY_STATUSES for x in vs):
        raise UKBError("REGISTRY_INVALID", "visible_statuses must list known entry statuses")
    vt = spec.get("visible_types")
    if vt is not None and (not isinstance(vt, list) or any(t not in ids(conn, "reg_type", "type_id", False)
                                                           for t in vt)):
        raise UKBError("REGISTRY_INVALID", "visible_types refers to unknown types")
    b = spec.get("read_budget_chars", 40000)
    if not isinstance(b, int) or b < 2000:
        raise UKBError("REGISTRY_INVALID", "read_budget_chars must be an integer >= 2000")
    for k, lo in (("catalog_limit", 5), ("read_max_entries", 1)):
        v = spec.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < lo):
            raise UKBError("REGISTRY_INVALID", "%s must be an integer >= %d" % (k, lo))
    mr = spec.get("must_read", {})
    if not isinstance(mr, dict) or set(mr) - {"entry_ids", "scope"}:
        raise UKBError("REGISTRY_INVALID", "must_read must be {entry_ids?, scope?}")
    s["status"] = _status(s)
    return s


def _a_profile(conn, s, actor):
    conn.execute("INSERT INTO consumer_profile(profile_id,spec_json,status) VALUES (?,?,?) "
                 "ON CONFLICT(profile_id) DO UPDATE SET spec_json=excluded.spec_json, status=excluded.status",
                 (s["profile_id"], canonical_json(s["spec"]), s["status"]))


def _v_risk_rule(conn, s):
    _check_id(s.get("rule_id"), "risk rule")
    if s.get("match_kind") not in ("keyword", "regex", "tag", "type"):
        raise UKBError("REGISTRY_INVALID", "invalid match_kind")
    _req_str(s, "pattern", "risk rule")
    if s["match_kind"] == "regex":
        try:
            re.compile(s["pattern"])
        except re.error as ex:
            raise UKBError("REGISTRY_INVALID", "bad regex", error=str(ex))
    if s["match_kind"] == "tag" and not re.match(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$", s["pattern"]):
        raise UKBError("REGISTRY_INVALID", "tag pattern must be facet/slug")
    if s.get("action") not in ("flag", "quarantine"):
        raise UKBError("REGISTRY_INVALID", "action must be flag or quarantine")
    s["status"] = _status(s)
    return s


def _a_risk_rule(conn, s, actor):
    conn.execute("INSERT INTO risk_rule(rule_id,match_kind,pattern,action,note,status) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(rule_id) DO UPDATE SET match_kind=excluded.match_kind, pattern=excluded.pattern, "
                 "action=excluded.action, note=excluded.note, status=excluded.status",
                 (s["rule_id"], s["match_kind"], s["pattern"], s["action"], s.get("note"), s["status"]))


def _v_artifact_role(conn, s):
    _check_id(s.get("role_id"), "artifact role")
    _req_str(s, "label", "artifact role")
    _req_str(s, "description", "artifact role")
    fm = s.get("formats")
    if fm is not None and (not isinstance(fm, list) or any(f not in ids(conn, "reg_file_format", "format_id")
                                                           for f in fm)):
        raise UKBError("REGISTRY_INVALID", "formats refers to unknown file formats")
    s["status"] = _status(s)
    return s


def _a_artifact_role(conn, s, actor):
    conn.execute("INSERT INTO reg_artifact_role(role_id,label,description,default_reusable,formats_json,status) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(role_id) DO UPDATE SET label=excluded.label, "
                 "description=excluded.description, default_reusable=excluded.default_reusable, "
                 "formats_json=excluded.formats_json, status=excluded.status",
                 (s["role_id"], s["label"], s["description"], int(bool(s.get("default_reusable"))),
                  canonical_json(s["formats"]) if s.get("formats") is not None else None, s["status"]))


def _v_file_format(conn, s):
    _check_id(s.get("format_id"), "file format")
    _req_str(s, "label", "file format")
    pats = s.get("patterns")
    if not isinstance(pats, list) or not pats or not all(isinstance(p, str) and p.strip() and "/" not in p
                                                          for p in pats):
        raise UKBError("REGISTRY_INVALID", "patterns must be a non-empty list of filename globs")
    if s.get("kind") not in ("text", "pdf", "binary"):
        raise UKBError("REGISTRY_INVALID", "kind must be text, pdf or binary")
    mh = s.get("magic_hex")
    if s["kind"] == "binary" and not mh:
        raise UKBError("REGISTRY_INVALID", "binary formats need magic_hex")
    if mh is not None:
        try:
            bytes.fromhex(mh)
        except ValueError:
            raise UKBError("REGISTRY_INVALID", "magic_hex is not hex")
    mb = s.get("max_bytes")
    if mb is not None and (not isinstance(mb, int) or mb < 1):
        raise UKBError("REGISTRY_INVALID", "max_bytes must be a positive integer")
    s["status"] = _status(s)
    return s


def _a_file_format(conn, s, actor):
    conn.execute("INSERT INTO reg_file_format(format_id,label,patterns_json,kind,magic_hex,max_bytes,status) "
                 "VALUES (?,?,?,?,?,?,?) ON CONFLICT(format_id) DO UPDATE SET label=excluded.label, "
                 "patterns_json=excluded.patterns_json, kind=excluded.kind, magic_hex=excluded.magic_hex, "
                 "max_bytes=excluded.max_bytes, status=excluded.status",
                 (s["format_id"], s["label"], canonical_json(s["patterns"]), s["kind"], s.get("magic_hex"),
                  s.get("max_bytes"), s["status"]))


def _v_file_deny(conn, s):
    p = _req_str(s, "pattern", "file deny")
    if "/" in p:
        raise UKBError("REGISTRY_INVALID", "deny pattern is a filename glob")
    _req_str(s, "reason", "file deny")
    s["status"] = _status(s)
    return s


def _a_file_deny(conn, s, actor):
    conn.execute("INSERT INTO reg_file_deny(pattern,reason,status) VALUES (?,?,?) ON CONFLICT(pattern) DO UPDATE "
                 "SET reason=excluded.reason, status=excluded.status", (s["pattern"], s["reason"], s["status"]))


def _v_setting(conn, s):
    k = s.get("key")
    if k not in DEFAULT_SETTINGS:
        raise UKBError("REGISTRY_INVALID", "unknown setting", key=k)
    v = s.get("value")
    d = DEFAULT_SETTINGS[k]
    if isinstance(d, bool):
        ok = isinstance(v, bool)
    elif isinstance(d, int):
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
    else:
        ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
    if not ok:
        raise UKBError("REGISTRY_INVALID", "setting value has wrong type", key=k)
    if k in ("intake_roots", "checkout_roots"):
        v = [os.path.realpath(x) for x in v]
        for x in v:
            if not os.path.isabs(x):
                raise UKBError("REGISTRY_INVALID", "roots must be absolute paths", value=x)
        s["value"] = v
    return s


def _a_setting(conn, s, actor):
    conn.execute("INSERT INTO reg_setting(key,value_json) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value_json=excluded.value_json", (s["key"], canonical_json(s["value"])))


# ---------- seed loading ----------

SEED_ORDER = ("setting", "file_format", "file_deny", "artifact_role", "facet", "tag", "condition_key",
              "relation", "source_type", "type", "profile", "risk_rule")


def load_pack(conn, pack_dir, actor):
    """A pack is a directory of <kind>.json files (each a JSON list of specs) plus types/*.json."""
    if not os.path.isdir(pack_dir):
        raise UKBError("CONFIG_INVALID", "pack directory not found", path=pack_dir)
    count = 0
    for kind in SEED_ORDER:
        items = []
        if kind == "type":
            tdir = os.path.join(pack_dir, "types")
            if os.path.isdir(tdir):
                for name in sorted(os.listdir(tdir)):
                    if name.endswith(".json"):
                        with open(os.path.join(tdir, name), encoding="utf-8") as f:
                            items.append(json.load(f))
        else:
            p = os.path.join(pack_dir, kind + ".json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    items = json.load(f)
                if not isinstance(items, list):
                    raise UKBError("CONFIG_INVALID", "pack file must hold a list", path=p)
        for it in items:
            apply_change(conn, kind, it, actor)
            count += 1
    return count
