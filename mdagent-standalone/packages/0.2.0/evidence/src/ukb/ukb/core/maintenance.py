"""lint, export, backup, integrity checks."""
import json
import os
import sqlite3
import tempfile

from . import audit, documents, registry
from .canon import now
from .intake import load_rev_files

# reviewer is left out on purpose: it holds admin token hashes.
EXPORT_TABLES = ("schema_meta", "reg_setting", "reg_type", "reg_type_version", "reg_facet", "tag",
                 "tag_alias", "reg_condition_key", "reg_relation", "reg_source_type", "consumer_profile", "risk_rule",
                 "reg_artifact_role", "reg_file_format", "reg_file_deny", "entry", "entry_revision", "rev_approval", "entry_condition",
                 "entry_tag", "blob", "source", "entry_source", "doc_file", "doc_section", "intake", "review_task",
                 "review_decision", "reinforcement", "relation", "usage_log", "checkout_log", "audit_log")


def lint(conn, blobs):
    issues = []

    def add(code, **kw):
        issues.append(dict(code=code, **kw))

    a = audit.verify(conn)
    if not a["ok"]:
        add("AUDIT_CHAIN_BROKEN", seq=a["bad_seq"])
    for e in conn.execute("SELECT * FROM entry WHERE status IN ('active','trial') ORDER BY entry_id"):
        n = conn.execute("SELECT count(*) FROM entry_tag WHERE entry_id=? AND state='confirmed'",
                         (e["entry_id"],)).fetchone()[0]
        if n == 0:
            add("UNTAGGED_ENTRY", entry_id=e["entry_id"])
        if e["head_rev"] is None:
            add("ACTIVE_WITHOUT_REVISION", entry_id=e["entry_id"])
            continue
        rev = conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (e["head_rev"],)).fetchone()
        if rev["entry_id"] != e["entry_id"]:
            add("HEAD_REV_FOREIGN", entry_id=e["entry_id"])
        if rev["manifest_hash"]:
            files = load_rev_files(conn, rev["rev_id"])
            if documents.manifest_hash(files) != rev["manifest_hash"]:
                add("MANIFEST_MISMATCH", entry_id=e["entry_id"])
            for f in files:
                for sha in (f["sha256"], f["text_sha256"]):
                    if sha:
                        try:
                            blobs.read_bytes(sha)
                        except Exception as ex:
                            add("BLOB_PROBLEM", entry_id=e["entry_id"], path=f["path"], error=getattr(ex, "code", str(ex)))
    for e in conn.execute("SELECT entry_id FROM entry WHERE status='superseded'"):
        if conn.execute("SELECT 1 FROM relation WHERE dst_entry=? AND rel_id='supersedes'", (e[0],)).fetchone() is None:
            add("SUPERSEDED_WITHOUT_SUCCESSOR", entry_id=e[0])
    for r in conn.execute("SELECT r.* FROM relation r JOIN entry a ON a.entry_id=r.src_entry JOIN entry b ON "
                          "b.entry_id=r.dst_entry WHERE r.rel_id='contradicts' AND a.status IN ('active','trial') "
                          "AND b.status IN ('active','trial')"):
        add("OPEN_CONTRADICTION", src=r["src_entry"], dst=r["dst_entry"], note=r["note"])
    for f in conn.execute("SELECT * FROM reg_facet WHERE status='active' AND scope_budget_chars IS NOT NULL"):
        for t in conn.execute("SELECT * FROM tag WHERE facet_id=? AND status='active'", (f["facet_id"],)):
            if conn.execute("SELECT 1 FROM tag WHERE parent_tag_id=? AND status='active'", (t["tag_id"],)).fetchone():
                continue
            chars = conn.execute("SELECT coalesce(sum(r.char_count),0) FROM entry_tag et JOIN entry e ON "
                                 "e.entry_id=et.entry_id JOIN entry_revision r ON r.rev_id=e.head_rev WHERE "
                                 "et.tag_id=? AND et.state='confirmed' AND e.status IN ('active','trial')",
                                 (t["tag_id"],)).fetchone()[0]
            if chars > f["scope_budget_chars"]:
                add("SCOPE_OVER_BUDGET", facet=f["facet_id"], tag=t["slug"], chars=chars,
                    budget=f["scope_budget_chars"], hint="split into child tags")
    for t in conn.execute("SELECT * FROM tag WHERE status='active'"):
        used = conn.execute("SELECT 1 FROM entry_tag WHERE tag_id=? AND state IN ('confirmed','suggested')",
                            (t["tag_id"],)).fetchone()
        child = conn.execute("SELECT 1 FROM tag WHERE parent_tag_id=?", (t["tag_id"],)).fetchone()
        if not used and not child:
            add("EMPTY_TAG", facet=t["facet_id"], tag=t["slug"])
    stale = conn.execute("SELECT count(*) FROM review_task WHERE state='open' AND opened_at < ?",
                         (_days_ago(14),)).fetchone()[0]
    if stale:
        add("STALE_TASKS", count=stale, older_than_days=14)
    return {"ok": not any(i["code"] in ("AUDIT_CHAIN_BROKEN", "MANIFEST_MISMATCH", "BLOB_PROBLEM",
                                        "HEAD_REV_FOREIGN", "ACTIVE_WITHOUT_REVISION") for i in issues),
            "issues": issues}


def _days_ago(days):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat(
        timespec="milliseconds")


def export_jsonl(conn, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    counts = {}
    for table in EXPORT_TABLES:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(%s)" % table), key=lambda r: r[5]) if r[5]] or cols
        rows = conn.execute("SELECT * FROM %s ORDER BY %s" % (table, ",".join(pk))).fetchall()
        path = os.path.join(dest_dir, table + ".jsonl")
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps({c: r[c] for c in cols}, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, path)
        counts[table] = len(rows)
    return counts


def backup(conn, dest_path):
    d = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(d, exist_ok=True)
    fd = os.open(dest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".sqlite")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        conn.backup(dst)
        dst.close()
        os.replace(tmp, dest_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        if os.path.exists(dest_path) and os.path.getsize(dest_path) == 0:
            os.unlink(dest_path)
        raise
    return {"ok": True, "path": dest_path, "at": now()}


def status(conn):
    out = {"entries": {}, "open_tasks": {}, "tags": {}}
    for r in conn.execute("SELECT status, count(*) FROM entry GROUP BY status"):
        out["entries"][r[0]] = r[1]
    for r in conn.execute("SELECT kind, count(*) FROM review_task WHERE state='open' GROUP BY kind"):
        out["open_tasks"][r[0]] = r[1]
    for r in conn.execute("SELECT status, count(*) FROM tag GROUP BY status"):
        out["tags"][r[0]] = r[1]
    out["awaiting_prereview"] = conn.execute(
        "SELECT count(*) FROM review_task t WHERE t.state='open' AND t.prereview_required=1 AND NOT EXISTS "
        "(SELECT 1 FROM review_decision d WHERE d.task_id=t.task_id AND d.decision='prereview' "
        "AND d.bound_hash=t.bound_hash)").fetchone()[0]
    out["settings"] = {k: registry.setting(conn, k) for k in registry.DEFAULT_SETTINGS}
    return out
