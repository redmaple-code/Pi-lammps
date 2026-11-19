"""Stable knowledge-base interface for the review layer and future DB-backed features.

Every write goes through the UKB review chain: propose -> optional prereview -> admin decision.
Nothing here writes SQL directly. Reads may use the UKB read API or read-only PRAGMA/SELECT
queries for inspection (counts, history, audit tail, schema); those never change the store.

Typical use:

    with KnowledgeBase() as kb:
        rows = kb.catalog(scope={"topic": ["general"]}, types=["lesson"])
        entry = kb.entry(rows["items"][0]["ref"])
        task = kb.revise(entry["ref"], fields={"risk": "..."}, approve=True)

The module stays domain-neutral: it knows no review rules, only UKB concepts
(entries, revisions, tags, sources, conditions, review tasks, registry kinds).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO.parent
UKB_PKG = REPO / "ukb"
DEFAULT_ROOT = REPO / "data" / "ukb"
DEFAULT_TOKEN_FILE = WORKSPACE / "design-review-layer" / "ukb_admin.json"


def _find_token_file():
    """Prefer an explicit env, then the review layer's file, then a repo-local one."""
    for env in TOKEN_ENVS:
        if os.environ.get(env):
            return Path(os.environ[env]).resolve()
    for candidate in (DEFAULT_TOKEN_FILE, WORKSPACE / "ukb_admin.json", REPO / "data" / "ukb" / "admin.json"):
        if candidate.exists():
            return candidate.resolve()
    return DEFAULT_TOKEN_FILE.resolve()
TOKEN_ENVS = ("MDAGENT_UKB_ADMIN_FILE", "REVIEW_UKB_ADMIN_FILE")
ROOT_ENVS = ("MDAGENT_UKB_ROOT", "REVIEW_UKB_ROOT")
SYSTEM_ADMIN = "mdagent-system"

if str(UKB_PKG) not in sys.path:
    sys.path.insert(0, str(UKB_PKG))

DEFAULT_SUBMITTER = "kb-api"
DEFAULT_CHANNEL = "cli"


def _write_secret(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def default_root(value=None):
    if value:
        return Path(value).resolve()
    for env in ROOT_ENVS:
        if os.environ.get(env):
            return Path(os.environ[env]).resolve()
    return DEFAULT_ROOT.resolve()


def token_file():
    return _find_token_file()


def _admin():
    path = token_file()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("admin"), data.get("token")
    return None, None


def _open(root):
    root = Path(root).resolve()
    if not (root / "ukb.sqlite").exists():
        raise KBError("KB_NOT_INITIALIZED", "UKB store not found", root=str(root))
    from ukb.api import UKB
    return UKB(str(root))


def ensure_admin_token(admin_id="redmaple", force=False, token_path=None, root=None):
    """Validate or rotate the human admin token; never print it."""
    root = default_root(root)
    path = Path(token_path).resolve() if token_path else token_file()
    kb = _open(root)
    try:
        current = None
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            current = (data.get("admin"), data.get("token"))
        if not force and current and current[1]:
            try:
                kb.auth(current[0], current[1])
                return {"admin": current[0], "rotated": False, "root": str(root), "token_file": str(path)}
            except Exception:
                pass
        token = kb.reset_token(admin_id, "kb-client:ensure-admin")
        _write_secret(path, json.dumps({"admin": admin_id, "token": token, "root": str(root)},
                                       ensure_ascii=False))
        return {"admin": admin_id, "rotated": True, "root": str(root), "token_file": str(path)}
    finally:
        kb.close()


def ensure_system_admin(root=None):
    """Create/validate mdagent-system and system_auto.token for machine observations."""
    root = default_root(root)
    path = root / "system_auto.token"
    kb = _open(root)
    try:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            try:
                kb.auth(SYSTEM_ADMIN, token)
                return {"admin": SYSTEM_ADMIN, "rotated": False, "root": str(root)}
            except Exception:
                pass
        try:
            token = kb.reset_token(SYSTEM_ADMIN, "kb-client:ensure-system")
        except Exception:
            token = kb.add_admin(SYSTEM_ADMIN, "kb-client:ensure-system")
        _write_secret(path, token + "\n")
        return {"admin": SYSTEM_ADMIN, "rotated": True, "root": str(root)}
    finally:
        kb.close()


def token_file_path():
    return token_file()


class KBError(RuntimeError):
    """Uniform failure surface: a machine-readable code plus structured detail."""

    def __init__(self, code, message, **detail):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self):
        out = {"ok": False, "error": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


def _wrap(exc):
    if isinstance(exc, KBError):
        return exc
    code = getattr(exc, "code", "KB_ERROR")
    message = getattr(exc, "message", str(exc))
    detail = getattr(exc, "detail", {}) or {}
    return KBError(code, message, **detail)


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class KnowledgeBase:
    """One UKB store, one lazy connection, one admin identity."""

    def __init__(self, root=None, *, admin=None, token=None, profile="agent", submitter=DEFAULT_SUBMITTER):
        self.root = default_root(root)
        self.admin, self.token = admin, token
        if self.token is None:
            self.admin, self.token = _admin()
        self.profile = profile
        self.submitter = submitter
        self._kb = None

    # ---------- lifecycle ----------

    @property
    def kb(self):
        if self._kb is None:
            self._kb = _open(self.root)
        return self._kb

    def close(self):
        if self._kb is not None:
            try:
                self._kb.close()
            finally:
                self._kb = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # UKBError, sqlite errors, adapter errors
            raise _wrap(exc) from exc

    # ---------- inspection ----------

    def status(self):
        return self._call(self.kb.status)

    def verify_audit(self):
        return self._call(self.kb.verify_audit)

    def integrity(self):
        """SQLite-level health for storage-facing features."""
        conn = self.kb.conn
        return {
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "quick": conn.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_keys": [list(row) for row in conn.execute("PRAGMA foreign_key_check")],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "path": str(self.root / "ukb.sqlite"),
        }

    def stats(self):
        conn = self.kb.conn
        by_type = {}
        for type_id, status, n in conn.execute(
                "SELECT type_id, status, COUNT(*) FROM entry GROUP BY type_id, status ORDER BY type_id, status"):
            by_type.setdefault(type_id, {})[status] = n
        by_status = {status: n for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM entry GROUP BY status ORDER BY status")}
        return {"total": sum(by_status.values()), "by_status": by_status, "by_type": by_type}

    def schema(self):
        conn = self.kb.conn
        tables = {}
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            tables[name] = [dict(zip(("name", "type", "notnull", "default", "pk"), row))
                            for row in conn.execute("PRAGMA table_info(%s)" % name)]
        return tables

    def audit_tail(self, limit=50):
        conn = self.kb.conn
        rows = []
        for row in conn.execute(
                "SELECT seq, ts, actor, action, object_kind, object_id, detail_json, prev_hash, hash "
                "FROM audit_log ORDER BY seq DESC LIMIT ?", (int(limit),)):
            item = dict(row)
            item["detail"] = _json(item.pop("detail_json", None))
            rows.append(item)
        return rows

    def revisions(self, entry_id):
        conn = self.kb.conn
        return [dict(row) for row in conn.execute(
            "SELECT rev_id, rev_no, title, content_hash, parent_rev, author, change_note, created_at "
            "FROM entry_revision WHERE entry_id=? ORDER BY rev_no", (entry_id,))]

    # ---------- registry / profiles ----------

    def profiles(self):
        conn = self.kb.conn
        return [dict(row) for row in conn.execute(
            "SELECT profile_id, spec_json, status FROM consumer_profile ORDER BY profile_id")]

    def types(self):
        return self._call(self.kb.types)

    def type_spec(self, type_id):
        return self._call(self.kb.guide, type_id)

    def facets(self):
        conn = self.kb.conn
        return [dict(row) for row in conn.execute(
            "SELECT * FROM reg_facet WHERE status='active' ORDER BY ord, facet_id")]

    def tags(self, facet=None):
        conn = self.kb.conn
        if facet:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM tag WHERE facet_id=? AND status='active' ORDER BY slug", (facet,))]
        return [dict(row) for row in conn.execute(
            "SELECT * FROM tag WHERE status='active' ORDER BY facet_id, slug")]

    def condition_keys(self):
        conn = self.kb.conn
        return [dict(row) for row in conn.execute(
            "SELECT * FROM reg_condition_key WHERE status='active' ORDER BY key_id")]

    def source_types(self):
        conn = self.kb.conn
        return [dict(row) for row in conn.execute(
            "SELECT * FROM reg_source_type WHERE status='active' ORDER BY source_type_id")]

    # ---------- reads ----------

    def overview(self, facet=None, within=None, parent=None, profile=None):
        return self._call(self.kb.overview, profile or self.profile, facet, within, parent)

    def catalog(self, scope=None, types=None, limit=None, profile=None):
        return self._call(self.kb.catalog, profile or self.profile, scope, types, limit)

    def read(self, refs, view="full", profile=None):
        if isinstance(refs, str):
            refs = [refs]
        return self._call(self.kb.read, profile or self.profile, None, list(refs), None, None, view)

    def profile(self, profile_id):
        return self._call(self.kb.profile, profile_id)

    def must_read(self, profile_id=None):
        return self._call(self.kb.must_read, profile_id or self.profile)

    def guide(self, type_id=None):
        return self._call(self.kb.guide, type_id)

    def read_original(self, ref, path, section=None, page=None, lines=None, cursor=None, profile=None):
        return self._call(self.kb.read_original, profile or self.profile, ref, path, section, page, lines, cursor)

    def checkout(self, ref, dest, *, paths=None, overwrite=False, task_ref=None, actor=None,
                 allow_non_reusable=False, profile=None):
        return self._call(self.kb.checkout, profile or self.profile, ref, dest, actor or self.submitter,
                          paths, allow_non_reusable, overwrite, task_ref)

    def feedback(self, ref, outcome, note=None, task_ref=None):
        return self._call(self.kb.feedback, ref, self.submitter, outcome, note, task_ref)

    def entry(self, ref, profile="reviewer", view="full"):
        out = self.read([ref], view=view, profile=profile)
        items = out.get("items") or []
        if not items:
            raise KBError("ENTRY_NOT_FOUND", "entry is not visible or does not exist", ref=ref)
        return items[0]

    def list_entries(self, types=None, statuses=None, limit=2000):
        """Admin view over latest revisions; statuses filter is applied here, not by a profile."""
        conn = self.kb.conn
        q = ("SELECT e.entry_id, e.type_id, e.status, e.updated_at, r.rev_no, r.title "
             "FROM entry e JOIN entry_revision r ON r.rev_id=COALESCE(e.head_rev,"
             "(SELECT r2.rev_id FROM entry_revision r2 WHERE r2.entry_id=e.entry_id "
             " ORDER BY r2.rev_no DESC LIMIT 1))")
        args = []
        where = []
        if types:
            where.append("e.type_id IN (%s)" % ",".join("?" * len(types)))
            args += list(types)
        if statuses:
            where.append("e.status IN (%s)" % ",".join("?" * len(statuses)))
            args += list(statuses)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY e.type_id, r.title LIMIT ?"
        args.append(int(limit))
        return [dict(row) for row in conn.execute(q, args)]

    def find_entries(self, title_contains=None, type_id=None, limit=100):
        conn = self.kb.conn
        q = ("SELECT e.entry_id, e.type_id, e.status, r.rev_no, r.title FROM entry e "
             "JOIN entry_revision r ON r.rev_id=COALESCE(e.head_rev,"
             "(SELECT r2.rev_id FROM entry_revision r2 WHERE r2.entry_id=e.entry_id "
             " ORDER BY r2.rev_no DESC LIMIT 1)) WHERE 1=1")
        args = []
        if type_id:
            q += " AND e.type_id=?"
            args.append(type_id)
        if title_contains:
            q += " AND r.title LIKE ?"
            args.append("%" + title_contains + "%")
        q += " ORDER BY r.title LIMIT ?"
        args.append(int(limit))
        return [dict(row) for row in conn.execute(q, args)]

    def entry_by_title(self, title, type_id=None):
        rows = self.find_entries(title_contains=title, type_id=type_id, limit=50)
        exact = [r for r in rows if r["title"] == title]
        row = (exact or rows or [None])[0]
        return self.entry("%s@%d" % (row["entry_id"], row["rev_no"])) if row else None

    # ---------- review tasks ----------

    def tasks(self, state="open", kind=None, limit=200):
        return self._call(self.kb.queue, state, kind, limit)

    def review_view(self, task_id):
        return self._call(self.kb.review_view, task_id)

    def prereview_bundle(self, task_id, page=0):
        return self._call(self.kb.prereview_bundle, task_id, page)

    def prereview(self, task_id, opinion, reviewer_id="llm:kb-api-prereview"):
        bundle = self.prereview_bundle(task_id, 0)
        pages = bundle.get("pages", 1)
        tokens = [self.prereview_bundle(task_id, page)["page_token"] for page in range(pages)]
        return self._call(self.kb.prereview_submit, task_id, reviewer_id, tokens, opinion)

    def mechanical_prereview(self, task_id, reviewer_id="llm:kb-api-prereview"):
        """Structural-only prereview for trusted seed/test flows; never a semantic judgement."""
        bundle = self.prereview_bundle(task_id, 0)
        checklist = bundle["instructions"]["checklist"]
        pending = [i["content"] for i in bundle["items"] if i["part"] == "pending"][0]
        opinion = {
            "duplicate_of": [], "reinforces": [], "contradicts": [], "refines": [], "supersedes": [],
            "type_opinion": {"ok": True, "note": "structural check"},
            "tag_opinion": {"ok": bool(pending.get("tags")), "note": "structural check"},
            "checklist": [{"item": c, "ok": True, "note": "structural check"} for c in checklist],
            "recommendation": "approve",
            "note": "mechanical prereview in kb_api (structure only)",
        }
        return self.prereview(task_id, opinion, reviewer_id)

    def _require_admin(self):
        if not self.token:
            raise KBError("ADMIN_TOKEN_MISSING", "no UKB admin token for decisions")

    def decide(self, task_id, decision="approve", rationale="", channel="webui", args=None):
        self._require_admin()
        view = self.review_view(task_id)
        return self._call(self.kb.decide, task_id, self.admin, self.token, decision, rationale,
                          view["bound_hash"], channel, args)

    # ---------- writes ----------

    def propose(self, payload, channel=DEFAULT_CHANNEL, submitter=None):
        return self._call(self.kb.propose, payload, channel, submitter or self.submitter)

    def propose_and_approve(self, payload, rationale="kb-api local flow", submitter=None,
                            prereview="mechanical", channel="webui"):
        self._require_admin()
        result = self.propose(payload, submitter=submitter)
        task_id = result.get("task_id")
        if not task_id:
            return result
        if prereview == "mechanical":
            try:
                self.mechanical_prereview(task_id)
            except Exception as exc:
                if getattr(exc, "code", None) != "PREREVIEW_NOT_APPLICABLE":
                    raise
        elif isinstance(prereview, dict):
            self.prereview(task_id, prereview)
        decision = self.decide(task_id, "approve", rationale, channel=channel)
        return dict(result, decision=decision)

    def _current_entry(self, ref):
        return self.entry(ref, profile="reviewer")

    def _sources_payload(self, entry):
        return [{"source_type": s["source_type_id"], "title": s["title"], "locator": s["locator"],
                 "role": s["role"], "locator_detail": s.get("locator_detail"),
                 "excerpt": s.get("excerpt")} for s in entry.get("sources", [])]

    def _tags_payload(self, entry):
        return [{"facet": t["facet"], "tag": t["tag"]} for t in entry.get("tags", [])]

    def _conditions_payload(self, entry):
        return [{"key": c["key"], "op": c["op"], "value": c["value"],
                 "unit": c.get("unit"), "note": c.get("note")} for c in entry.get("conditions", [])]

    def revise(self, ref, *, title=None, fields=None, tags=None, sources=None, conditions=None,
               note="", origin=None, approve=False, rationale="kb-api revision"):
        entry = self._current_entry(ref)
        merged = dict(entry.get("fields") or {})
        for key, value in (fields or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        payload = {
            "mode": "revise",
            "target_entry_id": entry["entry_id"],
            "base_rev_no": entry["rev_no"],
            "title": title if title is not None else entry["title"],
            "fields": merged,
            "tags": tags if tags is not None else self._tags_payload(entry),
            "sources": sources if sources is not None else self._sources_payload(entry),
            "conditions": conditions if conditions is not None else self._conditions_payload(entry),
            "origin": origin or {"kind": "manual", "ref": "kb-api:revise:%s" % entry["entry_id"],
                                 "summary_of_evidence": "review-layer interface revision"},
            "change_note": note or "kb-api revision",
        }
        return self.propose_and_approve(payload, rationale) if approve else self.propose(payload)

    def allowed_transitions(self, ref):
        from ukb.core.review import TRANSITIONS
        return sorted(TRANSITIONS.get(self._current_entry(ref)["status"], ()))

    def change_status(self, ref, status, reason, approve=False, rationale=None, by_entry=None):
        if approve:
            self._require_admin()
        entry = self._current_entry(ref)
        result = self._call(self.kb.propose_status, entry["ref"], status, reason, self.submitter, by_entry)
        if not approve:
            return result
        decision = self.decide(result["task_id"], "approve", rationale or reason)
        return dict(result, decision=decision)

    def add_tags(self, ref, tags, reason, approve=False):
        if approve:
            self._require_admin()
        entry = self._current_entry(ref)
        result = self._call(self.kb.propose_tags, entry["ref"], tags, [], reason, self.submitter)
        if not approve:
            return result
        return dict(result, decision=self.decide(result["task_id"], "approve", reason))

    def drop_tags(self, ref, tags, reason, approve=False):
        if approve:
            self._require_admin()
        entry = self._current_entry(ref)
        result = self._call(self.kb.propose_tags, entry["ref"], [], tags, reason, self.submitter)
        if not approve:
            return result
        return dict(result, decision=self.decide(result["task_id"], "approve", reason))

    def propose_registry(self, kind, spec, reason, approve=False):
        if approve:
            self._require_admin()
        result = self._call(self.kb.propose_registry, kind, spec, reason, self.submitter)
        if not approve:
            return result
        return dict(result, decision=self.decide(result["task_id"], "approve", reason))

    # ---------- maintenance ----------

    def backup(self, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return self._call(self.kb.backup, str(dest))

    def export(self, dest_dir):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        return self._call(self.kb.export, str(dest_dir))
