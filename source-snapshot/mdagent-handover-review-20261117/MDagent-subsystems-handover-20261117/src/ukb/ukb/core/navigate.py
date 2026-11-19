"""Read side with staged disclosure. No scoring, no snippets.

Level 1  overview  : facets, then tags of one facet (one level at a time) with descriptions and counts. No entries.
Level 2  catalog   : for a chosen scope, one line per entry (ref, type, title, tags). No content. Issues a
                     short-lived catalog token that lists exactly those refs.
Level 3  read      : full content, only for refs that a catalog token in this session listed, a few at a time.
Agents cannot jump from a scope to full text; they must look at titles first and pick.
"""
import json
import os
import re

from . import audit, documents, registry
from .canon import canonical_json, hash_obj, now
from .errors import UKBError
from .intake import load_rev_conditions, load_rev_files, load_rev_sources

_REF_RE = re.compile(r"^(e[0-9a-f]+)(?:@(\d+))?$")


# ---------- visibility ----------

def visible(entry_row, prof):
    st = entry_row["status"]
    if st not in registry.ENTRY_STATUSES:
        raise UKBError("INTERNAL", "unknown entry status", status=st)
    if st not in prof["visible_statuses"]:
        return False
    vt = prof.get("visible_types")
    if vt is not None and entry_row["type_id"] not in vt:
        return False
    return entry_row["head_rev"] is not None or st in ("candidate", "quarantined")


def _readable_rev(conn, entry_row):
    if entry_row["head_rev"]:
        return conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (entry_row["head_rev"],)).fetchone()
    return conn.execute("SELECT * FROM entry_revision WHERE entry_id=? ORDER BY rev_no DESC LIMIT 1",
                        (entry_row["entry_id"],)).fetchone()


def parse_ref(ref):
    m = _REF_RE.match(str(ref).strip()) if ref is not None else None
    if not m:
        raise UKBError("ENTRY_REF_INVALID", "entry reference must look like <entry_id> or <entry_id>@<rev_no>",
                       ref=ref)
    return m.group(1), (int(m.group(2)) if m.group(2) else None)


def get_entry_rev(conn, prof, ref):
    entry_id, rev_no = parse_ref(ref)
    e = conn.execute("SELECT * FROM entry WHERE entry_id=?", (entry_id,)).fetchone()
    if e is None or not visible(e, prof):
        raise UKBError("ENTRY_NOT_VISIBLE", "entry not found or not visible to this profile", entry_id=entry_id)
    if rev_no is None:
        rev = _readable_rev(conn, e)
    else:
        rev = conn.execute("SELECT * FROM entry_revision WHERE entry_id=? AND rev_no=?", (entry_id, rev_no)).fetchone()
        if rev is None:
            raise UKBError("REVISION_UNKNOWN", "revision not found", entry_id=entry_id, rev_no=rev_no)
        if "candidate" not in prof["visible_statuses"] and conn.execute(
                "SELECT 1 FROM rev_approval WHERE rev_id=?", (rev["rev_id"],)).fetchone() is None:
            raise UKBError("REVISION_UNKNOWN", "revision was never approved", entry_id=entry_id, rev_no=rev_no)
    return e, rev


# ---------- scope ----------

def resolve_scope(conn, scope):
    """scope = {facet_id: [tag refs]}; same facet OR, across facets AND; parent tags include descendants."""
    if scope is None:
        return {}
    if not isinstance(scope, dict):
        raise UKBError("SCOPE_INVALID", "scope must be an object {facet: [tags]}")
    out = {}
    for facet_id, refs in scope.items():
        if isinstance(refs, (str, int)):
            refs = [refs]
        if not isinstance(refs, list) or not refs:
            raise UKBError("SCOPE_INVALID", "each facet needs a non-empty tag list", facet=facet_id)
        ids = set()
        for r in refs:
            row = registry.resolve_tag(conn, facet_id, r)
            if row["status"] not in ("active", "candidate"):
                raise UKBError("TAG_RETIRED", "tag is no longer in use", facet=facet_id, tag=row["slug"])
            ids.update(registry.tag_descendants(conn, row["tag_id"]))
        out[facet_id] = ids
    return out


def _tag_states(prof):
    return ("confirmed", "suggested") if prof.get("include_suggested_tags") else ("confirmed",)


def scope_entries(conn, prof, resolved, types=None):
    states = _tag_states(prof)
    rows = conn.execute("SELECT * FROM entry ORDER BY type_id, entry_id").fetchall()
    cand = [e for e in rows if visible(e, prof) and (types is None or e["type_id"] in types)]
    if not resolved:
        return cand
    tags_by_entry = {}
    q = "SELECT entry_id, tag_id FROM entry_tag WHERE state IN (%s)" % ",".join("?" * len(states))
    for r in conn.execute(q, states):
        tags_by_entry.setdefault(r[0], set()).add(r[1])
    out = []
    for e in cand:
        have = tags_by_entry.get(e["entry_id"], set())
        if all(have & ids for ids in resolved.values()):
            out.append(e)
    return out


def _sorted_with_revs(conn, entries):
    items = []
    for e in entries:
        rev = _readable_rev(conn, e)
        items.append((e, rev))
    items.sort(key=lambda x: (x[0]["type_id"], x[1]["title"].casefold(), x[0]["entry_id"]))
    return items


# ---------- rendering ----------

def entry_tags(conn, entry_id, states=("confirmed", "suggested")):
    q = ("SELECT t.tag_id, t.facet_id, t.slug, t.label, t.status AS tag_status, et.state, et.origin, et.reason "
         "FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id WHERE et.entry_id=? AND et.state IN (%s) "
         "ORDER BY t.facet_id, t.slug" % ",".join("?" * len(states)))
    return [dict(r) for r in conn.execute(q, (entry_id,) + tuple(states))]


def _relations(conn, entry_id):
    out = []
    for r in conn.execute("SELECT r.*, g.label, g.inverse_label FROM relation r JOIN reg_relation g ON g.rel_id=r.rel_id "
                          "WHERE r.src_entry=? OR r.dst_entry=? ORDER BY r.rel_id, r.src_entry, r.dst_entry",
                          (entry_id, entry_id)):
        outgoing = r["src_entry"] == entry_id
        other = r["dst_entry"] if outgoing else r["src_entry"]
        st = conn.execute("SELECT status FROM entry WHERE entry_id=?", (other,)).fetchone()
        out.append({"relation": r["rel_id"], "label": r["label"] if outgoing else r["inverse_label"],
                    "direction": "out" if outgoing else "in", "entry_id": other,
                    "other_status": st["status"] if st else None, "note": r["note"]})
    return out


def render(conn, entry_row, rev, include_tags=True):
    tspec = registry.type_spec(conn, rev["type_id"], rev["type_version"])
    spec = tspec["spec"]
    fields = json.loads(rev["fields_json"])
    ordered = {k: fields[k] for k in spec["render"] if k in fields}
    head_no = None
    if entry_row["head_rev"]:
        head_no = conn.execute("SELECT rev_no FROM entry_revision WHERE rev_id=?", (entry_row["head_rev"],)).fetchone()[0]
    out = {
        "ref": "%s@%d" % (entry_row["entry_id"], rev["rev_no"]),
        "entry_id": entry_row["entry_id"], "rev_no": rev["rev_no"], "type": rev["type_id"],
        "type_label": tspec["label"], "status": entry_row["status"], "status_note": entry_row["status_note"],
        "is_head": rev["rev_id"] == entry_row["head_rev"], "head_rev_no": head_no,
        "title": rev["title"], "fields": ordered, "conditions": load_rev_conditions(conn, rev["rev_id"]),
        "sources": [{k: s[k] for k in ("source_type_id", "title", "locator", "grade", "role", "locator_detail",
                                       "excerpt")} for s in load_rev_sources(conn, rev["rev_id"])],
        "reinforced_by": [dict(r) for r in conn.execute(
            "SELECT s.source_type_id, s.title, s.locator, rf.note FROM reinforcement rf JOIN source s "
            "ON s.source_id=rf.source_id WHERE rf.entry_id=? ORDER BY rf.created_at", (entry_row["entry_id"],))],
        "relations": _relations(conn, entry_row["entry_id"]),
        "content_hash": rev["content_hash"],
    }
    if include_tags:
        out["tags"] = [{"facet": t["facet_id"], "tag": t["slug"], "label": t["label"], "state": t["state"]}
                       for t in entry_tags(conn, entry_row["entry_id"])]
    if spec.get("tier") == "document":
        files = load_rev_files(conn, rev["rev_id"])
        out["document"] = {
            "files": [{"path": f["path"], "role": f["role_id"], "format": f["format_id"],
                       "description": f["description"], "size": f["size"], "readable_chars": f["char_count"],
                       "lines": f["line_count"], "pages": f["page_count"], "reusable": f["reusable"],
                       "license_note": f["license_note"],
                       "sections": [{"section_id": s["section_id"], "title": s["title"],
                                     "chars": s["end_char"] - s["start_char"]} for s in f["sections"]]}
                      for f in files],
            "original_chars": sum(f["char_count"] or 0 for f in files),
            "hint": "summary only; read originals with read_original, reuse files with checkout",
        }
    return out


def _render_size(r):
    return len(canonical_json(r))


# ---------- public read API ----------

def must_read(conn, prof):
    spec = prof.get("must_read") or {}
    entries = []
    for ref in spec.get("entry_ids", []):
        try:
            e, rev = get_entry_rev(conn, prof, ref)
            entries.append((e, rev))
        except UKBError:
            continue
    if spec.get("scope"):
        resolved = resolve_scope(conn, spec["scope"])
        for e, rev in _sorted_with_revs(conn, scope_entries(conn, prof, resolved)):
            if all(x[0]["entry_id"] != e["entry_id"] for x in entries):
                entries.append((e, rev))
    items = [render(conn, e, rev) for e, rev in entries]
    return {"profile": prof["profile_id"], "count": len(items), "items": items,
            "chars": sum(_render_size(i) for i in items)}


def overview(conn, prof, facet=None, within=None, parent=None):
    resolved = resolve_scope(conn, within)
    base = scope_entries(conn, prof, resolved)
    base_ids = {e["entry_id"] for e in base}
    chars = {}
    for e, rev in _sorted_with_revs(conn, base):
        chars[e["entry_id"]] = rev["char_count"]
    states = _tag_states(prof)
    links = {}
    q = "SELECT entry_id, tag_id FROM entry_tag WHERE state IN (%s)" % ",".join("?" * len(states))
    for r in conn.execute(q, states):
        if r[0] in base_ids:
            links.setdefault(r[1], set()).add(r[0])
    if facet is None:
        out = []
        for f in conn.execute("SELECT * FROM reg_facet WHERE status='active' ORDER BY ord, facet_id"):
            tag_ids = [r[0] for r in conn.execute("SELECT tag_id FROM tag WHERE facet_id=?", (f["facet_id"],))]
            ents = set().union(*[links.get(t, set()) for t in tag_ids]) if tag_ids else set()
            out.append({"facet": f["facet_id"], "label": f["label"], "description": f["description"],
                        "governing": bool(f["governing"]), "multi": bool(f["multi"]),
                        "entries": len(ents), "chars": sum(chars[x] for x in ents)})
        untagged = [x for x in base_ids if not any(x in s for s in links.values())]
        return {"within": within or {}, "entries_in_scope": len(base_ids),
                "chars_in_scope": sum(chars.values()), "untagged_in_scope": len(untagged), "facets": out}
    f = registry.facet(conn, facet)
    tags = []
    if parent is None:
        level_q = ("SELECT * FROM tag WHERE facet_id=? AND status IN ('active','candidate') AND parent_tag_id IS NULL "
                   "ORDER BY slug", (facet,))
    else:
        prow = registry.resolve_tag(conn, facet, parent)
        level_q = ("SELECT * FROM tag WHERE facet_id=? AND status IN ('active','candidate') AND parent_tag_id=? "
                   "ORDER BY slug", (facet, prow["tag_id"]))
    for t in conn.execute(*level_q):
        desc = registry.tag_descendants(conn, t["tag_id"])
        ents = set().union(*[links.get(x, set()) for x in desc])
        parent = None
        if t["parent_tag_id"]:
            parent = conn.execute("SELECT slug FROM tag WHERE tag_id=?", (t["parent_tag_id"],)).fetchone()[0]
        kids = conn.execute("SELECT count(*) FROM tag WHERE parent_tag_id=? AND status IN ('active','candidate')",
                            (t["tag_id"],)).fetchone()[0]
        tags.append({"tag": t["slug"], "label": t["label"], "description": t["description"],
                     "aliases": registry.tag_aliases(conn, t["tag_id"]), "entries": len(ents), "children": kids})
    return {"facet": facet, "label": f["label"], "within": within or {}, "parent": parent, "tags": tags,
            "next": "pick tags and call ukb_catalog; expand a tag with children via ukb_overview(facet, parent=tag)"}


def catalog(conn, prof, scope=None, types=None, limit=None):
    """Level 2: titles only. A scope is required for agents (no listing of the whole KB)."""
    resolved = resolve_scope(conn, scope)
    cap = int(limit or prof.get("catalog_limit", 30))
    all_rows = _sorted_with_revs(conn, scope_entries(conn, prof, resolved, types))
    rows = []
    for e, rev in all_rows[:cap]:
        row = {"ref": "%s@%d" % (e["entry_id"], rev["rev_no"]), "type": e["type_id"], "title": rev["title"],
               "tags": ["%s/%s" % (t["facet_id"], t["slug"]) for t in entry_tags(conn, e["entry_id"],
                                                                                   _tag_states(prof))]}
        if e["status"] != "active":
            row["status"] = e["status"]
        if rev["manifest_hash"]:
            row["has_files"] = True
        rows.append(row)
    out = {"scope": scope or {}, "count": len(all_rows), "shown": len(rows), "items": rows}
    if len(all_rows) > cap:
        out["narrow"] = ("only the first %d of %d titles are shown; nothing is shown beyond this. Narrow the scope "
                         "with one of these tags:" % (cap, len(all_rows)))
        out["narrow_by"] = _split_hints(conn, prof, [e for e, _ in all_rows], scope or {})
    return out


def _split_hints(conn, prof, entries, scope):
    """Tags that split the oversized scope, with counts, so the agent can pick a narrower scope directly."""
    ids = {e["entry_id"] for e in entries}
    states = _tag_states(prof)
    q = ("SELECT t.facet_id, t.slug, count(DISTINCT et.entry_id) FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id "
         "WHERE et.state IN (%s) AND et.entry_id IN (%s) GROUP BY t.facet_id, t.slug" %
         (",".join("?" * len(states)), ",".join("?" * len(ids))))
    used = {(f, s) for f, refs in scope.items() for s in ([refs] if isinstance(refs, (str, int)) else refs)}
    hints = [{"facet": f, "tag": s, "entries": n} for f, s, n in conn.execute(q, list(states) + list(ids))
             if n < len(ids) and (f, s) not in used]
    hints.sort(key=lambda h: (-h["entries"], h["facet"], h["tag"]))
    return hints[:15]


COMPACT_DROP = ("content_hash", "status_note", "type_label", "is_head", "head_rev_no")


def compact(r):
    out = {k: v for k, v in r.items() if k not in COMPACT_DROP}
    out["tags"] = ["%s/%s" % (t["facet"], t["tag"]) for t in r.get("tags", []) if t.get("state") == "confirmed"] \
        or ["%s/%s" % (t["facet"], t["tag"]) for t in r.get("tags", [])]
    out["sources"] = [" | ".join(x for x in (s["title"], s["locator"], s.get("locator_detail")) if x)
                      for s in r.get("sources", [])]
    out["reinforced_by"] = [s["title"] for s in r.get("reinforced_by", [])]
    out["relations"] = ["%s %s (%s)" % (x["label"], x["entry_id"], x["other_status"]) for x in r.get("relations", [])
                        if x["other_status"] in ("active", "trial", "deprecated", "superseded")]
    out["conditions"] = [{k: v for k, v in c.items() if v is not None} for c in r.get("conditions", [])]
    if "document" in r:
        out["document"] = {"files": ["%s [%s] %s%s" % (f["path"], f["role"], f["description"],
                                                       " (reusable)" if f["reusable"] else "")
                                     for f in r["document"]["files"]],
                           "sections": {f["path"]: [s["section_id"] + " " + s["title"] for s in f["sections"]]
                                        for f in r["document"]["files"] if f["sections"]},
                           "original_chars": r["document"]["original_chars"]}
    return out


def read(conn, prof, scope=None, entry_refs=None, types=None, cursor=None, view="compact"):
    if view not in ("compact", "full"):
        raise UKBError("SELECTOR_INVALID", "view must be compact or full")
    budget = prof.get("read_budget_chars", 40000)
    max_refs = prof.get("read_max_entries")
    if max_refs and entry_refs is not None and len(entry_refs) > max_refs:
        raise UKBError("READ_TOO_MANY", "read at most %d entries per call; pick the ones you need from the catalog"
                       % max_refs, limit=max_refs)
    if entry_refs is not None:
        if scope is not None:
            raise UKBError("SELECTOR_INVALID", "give scope or entry_refs, not both")
        if not isinstance(entry_refs, list) or not entry_refs:
            raise UKBError("SELECTOR_INVALID", "entry_refs must be a non-empty list")
        pairs = [get_entry_rev(conn, prof, r) for r in entry_refs]
    else:
        pairs = _sorted_with_revs(conn, scope_entries(conn, prof, resolve_scope(conn, scope), types))
    order = [("%s@%d" % (e["entry_id"], rev["rev_no"])) for e, rev in pairs]
    token = hash_obj(order)[:16]
    start = 0
    if cursor:
        try:
            off, tok = str(cursor).split(":", 1)
            start = int(off)
        except ValueError:
            raise UKBError("CURSOR_INVALID", "bad cursor")
        if tok != token:
            raise UKBError("CURSOR_STALE", "the scope changed since the previous page; start again")
    items, used, i = [], 0, start
    while i < len(pairs):
        r = render(conn, *pairs[i])
        if view == "compact":
            r = compact(r)
        size = _render_size(r)
        if items and used + size > budget:
            break
        items.append(r)
        used += size
        i += 1
    return {"total": len(pairs), "offset": start, "returned": len(items), "chars": used,
            "next_cursor": ("%d:%s" % (i, token)) if i < len(pairs) else None, "items": items}


def _chunk(text, budget, start):
    if start >= len(text):
        return "", None
    end = min(len(text), start + budget)
    if end < len(text):
        cut = text.rfind("\n", start + budget // 2, end)
        if cut > start:
            end = cut + 1
    return text[start:end], (end if end < len(text) else None)


def read_original(conn, blobs, prof, ref, path, section=None, page=None, lines=None, cursor=None):
    e, rev = get_entry_rev(conn, prof, ref)
    if not rev["manifest_hash"]:
        raise UKBError("NOT_DOCUMENT", "entry has no original files")
    path = documents.norm_path(path)
    files = {f["path"]: f for f in load_rev_files(conn, rev["rev_id"])}
    f = files.get(path)
    if f is None:
        raise UKBError("FILE_UNKNOWN", "file not in this revision", path=path, files=sorted(files))
    meta = {"ref": "%s@%d" % (e["entry_id"], rev["rev_no"]), "path": path, "role": f["role_id"],
            "format": f["format_id"], "size": f["size"], "sha256": f["sha256"], "reusable": f["reusable"]}
    blobs.read_bytes(f["sha256"])
    text = documents.readable_text(blobs, f)
    if text is None:
        meta.update(readable=False, note="no readable text for this file; use checkout to get the file")
        return meta
    label, part = documents.slice_text(text, f, f["sections"], section=section, page=page, lines=lines)
    budget = prof.get("read_budget_chars", 40000)
    token = hash_obj([f["sha256"], label])[:12]
    start = 0
    if cursor:
        try:
            off, tok = str(cursor).split(":", 1)
            start = int(off)
        except ValueError:
            raise UKBError("CURSOR_INVALID", "bad cursor")
        if tok != token:
            raise UKBError("CURSOR_STALE", "cursor does not belong to this selection")
    chunk, nxt = _chunk(part, budget, start)
    meta.update(readable=True, selection=label, selection_chars=len(part), offset=start, text=chunk,
                next_cursor=("%d:%s" % (nxt, token)) if nxt is not None else None,
                sections=[{"section_id": s["section_id"], "title": s["title"]} for s in f["sections"]])
    return meta


def _under_roots(path, roots):
    rp = os.path.realpath(path)
    for r in roots:
        r = os.path.realpath(r)
        if rp == r or rp.startswith(r.rstrip(os.sep) + os.sep):
            return rp
    return None


def checkout(conn, blobs, prof, ref, dest, actor, paths=None, allow_non_reusable=False, overwrite=False,
             task_ref=None):
    """Read-heavy; the log row is written by the caller-provided transaction."""
    e, rev = get_entry_rev(conn, prof, ref)
    if not rev["manifest_hash"]:
        raise UKBError("NOT_DOCUMENT", "entry has no original files")
    roots = registry.setting(conn, "checkout_roots")
    if not roots:
        raise UKBError("CHECKOUT_DISABLED", "no checkout_roots configured")
    if not isinstance(dest, str) or not dest.strip():
        raise UKBError("CHECKOUT_DENIED", "dest is required")
    real_dest = _under_roots(dest, roots)
    if real_dest is None:
        raise UKBError("CHECKOUT_DENIED", "dest is outside the allowed workspace roots", dest=dest)
    files = load_rev_files(conn, rev["rev_id"])
    if paths in (None, "reusable"):
        chosen = [f for f in files if f["reusable"]]
        if not chosen:
            raise UKBError("CHECKOUT_EMPTY", "this entry has no reusable files")
    else:
        if not isinstance(paths, list) or not paths:
            raise UKBError("SELECTOR_INVALID", "paths must be a list or 'reusable'")
        by = {f["path"]: f for f in files}
        chosen = []
        for p in paths:
            p = documents.norm_path(p)
            if p not in by:
                raise UKBError("FILE_UNKNOWN", "file not in this revision", path=p)
            chosen.append(by[p])
    blocked = [f["path"] for f in chosen if not f["reusable"]]
    if blocked and not allow_non_reusable:
        raise UKBError("CHECKOUT_NOT_REUSABLE", "files are marked not reusable", paths=blocked)
    plan = []
    for f in chosen:
        target = os.path.realpath(os.path.join(real_dest, *f["path"].split("/")))
        if not target.startswith(real_dest.rstrip(os.sep) + os.sep):
            raise UKBError("CHECKOUT_DENIED", "path escapes dest", path=f["path"])
        if os.path.exists(target) and not overwrite:
            raise UKBError("CHECKOUT_EXISTS", "target file exists; pass overwrite to replace it (a backup is kept)",
                           path=target)
        plan.append((f, target, blobs.read_bytes(f["sha256"])))
    written = []
    stamp = now().replace(":", "").replace("-", "").replace(".", "")
    for f, target, data in plan:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        backup = None
        if os.path.exists(target):
            backup = target + ".ukb-bak-" + stamp
            with open(target, "rb") as src, open(backup, "xb") as bk:
                bk.write(src.read())
            tmp = target + ".ukb-tmp-" + stamp
            with open(tmp, "xb") as out:
                out.write(data)
            os.replace(tmp, target)
        else:
            with open(target, "xb") as out:
                out.write(data)
        with open(target, "rb") as chk:
            if documents.raw_sha(chk.read()) != f["sha256"]:
                raise UKBError("CHECKOUT_VERIFY_FAILED", "written file hash mismatch", path=target)
        written.append({"path": f["path"], "target": target, "sha256": f["sha256"], "role": f["role_id"],
                        "reusable": f["reusable"], "license_note": f["license_note"], "backup": backup})
    rendered = render(conn, e, rev, include_tags=False)
    prov = {"ukb_ref": rendered["ref"], "entry_id": e["entry_id"], "rev_no": rev["rev_no"], "title": rev["title"],
            "type": rev["type_id"], "content_hash": rev["content_hash"], "checked_out_at": now(), "actor": actor,
            "task_ref": task_ref, "summary": rendered["fields"], "conditions": rendered["conditions"],
            "files": [{k: w[k] for k in ("path", "sha256", "role", "reusable", "license_note")} for w in written]}
    pname = os.path.join(real_dest, "UKB_PROVENANCE-%s-%d.json" % (e["entry_id"], rev["rev_no"]))
    if os.path.exists(pname):
        pname = pname[:-5] + "-" + stamp + ".json"
    with open(pname, "x", encoding="utf-8") as pf:
        json.dump(prov, pf, ensure_ascii=False, indent=2)
    conn.execute("INSERT INTO checkout_log(rev_id,paths_json,dest,actor,task_ref,ts) VALUES (?,?,?,?,?,?)",
                 (rev["rev_id"], canonical_json([w["path"] for w in written]), real_dest, actor, task_ref, now()))
    audit.append(conn, actor, "checkout", "entry", e["entry_id"], {"rev_no": rev["rev_no"], "files": len(written),
                                                                     "dest": real_dest})
    return {"ok": True, "ref": rendered["ref"], "dest": real_dest, "files": written, "provenance": pname}

