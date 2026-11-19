"""UKB compatibility layer for MDagent.

Every legacy knowledge entry point keeps its old signature and return shape; when ``MDAGENT_KB=ukb`` the data
comes from the UKB store under ``<data_root>/ukb`` instead of ``memory/*`` and ``mcp/review_experience/*``.

Nothing here deletes or rewrites the legacy store: legacy files stay on disk, untouched, for comparison and
rollback. Reads map UKB entries back to the exact dicts the consumers expect; the original record is preserved in
the entry field ``raw_json`` at import time, so no field is lost.

Write path (owner decision A): machine observations (type ``observation``, origin.kind ``run``) are landed
automatically by a dedicated system administrator after a mechanical prereview; everything else stays candidate
until an AI prereview and a human approval.
"""
import contextlib
import hashlib
import sqlite3
import threading
from types import SimpleNamespace
import json
import os
import sys
import time
from pathlib import Path

MODE_ENV = "MDAGENT_KB"
ROOT_ENV = "MDAGENT_UKB_ROOT"
AUTO_OBS_ENV = "MDAGENT_KB_AUTO_OBS"
SYSTEM_ADMIN = "mdagent-system"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STATUS = {"active": "active", "trial": "canary", "deprecated": "deprecated", "revoked": "revoked",
           "superseded": "superseded", "candidate": "candidate"}
_LEGACY_TYPES = ("legacy_anchor", "legacy_methodology", "legacy_dimension", "legacy_surface", "legacy_entry",
                 "legacy_atom", "claim", "observation", "case", "lesson", "rule", "parameter", "checklist",
                 "procedure", "definition", "reference", "correction", "failure_pattern", "faq", "document",
                 "template", "evidence_claim")
_READ_STATUSES = ("active", "trial", "deprecated", "revoked", "superseded")
_OPEN_STATUSES = ("active", "trial")
_CACHE = {}


# ---------- switch and roots ----------

def _mode_file():
    return os.path.join(resolve_data_root(), "kb_mode.json")


def _repo_config():
    path = Path(REPO) / "mcp" / "kb_config.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict) or set(config) - {"mode", "ukb_root"}:
        raise RuntimeError("invalid repository knowledge configuration")
    return config


def enabled():
    """Explicit switch: MDAGENT_KB, else <data_root>/kb_mode.json, else legacy.

    Any value other than 'ukb'/'legacy' is refused (fail-closed).
    """
    mode = (os.environ.get(MODE_ENV) or "").strip().lower()
    if not mode:
        path = _mode_file()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    config = json.load(f)
                if not isinstance(config, dict) or config.get("mode") not in ("ukb", "legacy"):
                    raise ValueError("missing or invalid mode")
                mode = config["mode"]
            except (OSError, ValueError) as exc:
                raise RuntimeError("invalid knowledge mode file") from exc
        # Per-process data roots are used by tests and isolated tools; they must not inherit the
        # repository production-mode switch. The serving wrapper sets MDAGENT_KB explicitly.
        if not mode and not os.environ.get("MDSIM_DATA_ROOT"):
            mode = str(_repo_config().get("mode") or "").strip().lower()
    if mode in ("", "legacy"):
        return False
    if mode != "ukb":
        raise RuntimeError("%s must be 'ukb' or 'legacy', got %r" % (MODE_ENV, mode))
    return True


def _default_data_root():
    """The deployment data root from repository config, ignoring per-process env overrides."""
    cfg = {}
    try:
        with open(os.path.join(REPO, "mcp", "md_config.json"), encoding="utf-8") as stream:
            cfg = json.load(stream)
    except (OSError, ValueError):
        cfg = {}
    raw = cfg.get("data_root")
    if not isinstance(raw, str) or not raw.strip():
        return os.path.abspath(os.path.join(os.path.dirname(REPO), "mdsim-data"))
    raw = os.path.expanduser(raw.strip())
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.dirname(REPO), raw)
    return os.path.abspath(raw)


def resolve_data_root(value=None):
    if value:
        return os.path.abspath(value)
    if os.environ.get("MDSIM_DATA_ROOT"):
        return os.path.abspath(os.environ["MDSIM_DATA_ROOT"])
    return _default_data_root()


# Backwards-compatible name used by the tooling.
def data_root(value=None):
    return resolve_data_root(value)


def ukb_root(data_root=None):
    """Store location.

    - an explicit data root is isolated under <root>/ukb, except when it is the deployment root,
      which maps to the repository-configured store;
    - without an explicit root: per-process override, then repository configuration.
    """
    env = (os.environ.get(ROOT_ENV) or "").strip()
    if env:
        return os.path.abspath(env)
    configured = _repo_config().get("ukb_root")
    if data_root is not None:
        explicit = os.path.abspath(data_root)
        if configured and os.path.normcase(explicit) == os.path.normcase(_default_data_root()):
            return str((Path(REPO) / configured).resolve())
        return os.path.join(explicit, "ukb")
    if configured:
        return str((Path(REPO) / configured).resolve())
    return os.path.join(resolve_data_root(), "ukb")


def _ukb_module():
    vendor = os.path.join(REPO, "ukb")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import ukb.api as api
    return api


def kb(data_root=None):
    root = ukb_root(data_root)
    key = os.path.abspath(root)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    api = _ukb_module()
    if not os.path.exists(os.path.join(key, "ukb.sqlite")):
        raise RuntimeError("UKB not initialised at %s; run tools/kb/bootstrap.py first" % key)
    _CACHE[key] = api.UKB(key)
    return _CACHE[key]


def reset_cache():
    for handle in _CACHE.values():
        try:
            handle.close()
        except Exception:
            pass
    _CACHE.clear()


# ---------- row helpers ----------

def _rows(data_root=None, types=_LEGACY_TYPES, statuses=_READ_STATUSES, with_head=True, *, handle=None):
    handle = handle or kb(data_root)
    q = ("SELECT e.entry_id, e.type_id, e.status, e.head_rev, r.fields_json, r.title, r.created_at, "
         "       r.content_hash "
         "FROM entry e JOIN entry_revision r ON r.rev_id=e.head_rev "
         "WHERE e.type_id IN (%s) AND e.status IN (%s) ORDER BY e.type_id, e.entry_id"
         % (",".join("?" * len(types)), ",".join("?" * len(statuses))))
    return [dict(r) for r in handle.conn.execute(q, tuple(types) + tuple(statuses))]


def _conditions(handle, rev_id):
    out = {}
    for r in handle.conn.execute("SELECT key_id, op, value_json FROM entry_condition WHERE rev_id=?", (rev_id,)):
        try:
            value = json.loads(r["value_json"])
        except ValueError:
            value = r["value_json"]
        if r["op"] == "eq":
            out[r["key_id"]] = value
        else:
            out[r["key_id"]] = {"op": r["op"], "value": value}
    return out


def _pack_entry(row, data_root=None, *, handle=None):
    """The original legacy record, with the legacy id and status restored."""
    handle = handle or kb(data_root)
    fields = json.loads(row["fields_json"])
    raw = fields.get("raw_json")
    entry = json.loads(raw) if isinstance(raw, str) and raw.strip() else dict(fields)
    entry.pop("raw_json", None)
    cond = _conditions(handle, row.get("head_rev") or "")
    legacy_id = cond.get("legacy_id") or entry.get("id") or row["entry_id"]
    entry["id"] = legacy_id
    entry["status"] = _STATUS.get(row["status"], row["status"])
    if cond.get("legacy_kind"):
        entry.setdefault("kind", cond["legacy_kind"]) if not entry.get("kind") else None
        entry["_legacy_kind"] = cond["legacy_kind"]
    if cond.get("legacy_pack"):
        entry["_pack"] = cond["legacy_pack"]
    if cond.get("legacy_consumer"):
        entry["_consumer"] = cond["legacy_consumer"]
    actual_conditions = {key: value for key, value in cond.items() if not key.startswith("legacy_")}
    entry["conditions"] = dict(entry.get("conditions") or {}, **actual_conditions)
    entry["_ukb_entry_id"] = row["entry_id"]
    entry["_status"] = entry["status"]
    return entry


# ---------- read APIs used by stage_gate / assessment / deep_review / surface ----------

def exp_status_index(data_root=None):
    idx = {}
    for row in _rows(data_root):
        entry = _pack_entry(row, data_root)
        kind = entry.get("kind") or ""
        if entry.get("_legacy_kind") == "pack-catalog":
            continue
        idx[str(entry["id"])] = {
            "status": entry["status"],
            "kind": kind,
            "severity": entry.get("severity_hint") or ("hard" if entry.get("exemptible") is False else "soft"),
            "pack": entry.get("_pack", row["type_id"]),
            "entry": entry,
        }
    return idx


def load_methodology_entries(data_root=None):
    out = []
    for info in exp_status_index(data_root).values():
        entry = info.get("entry") or {}
        if info.get("kind") != "methodology" and entry.get("_legacy_kind") != "methodology":
            continue
        if info.get("status") == "revoked":
            continue
        if not isinstance(entry.get("constraints"), dict):
            continue
        out.append(dict(entry, _status=info.get("status")))
    return out


def load_anchors(data_root=None):
    out = {}
    for row in _rows(data_root, types=("legacy_anchor",)):
        entry = _pack_entry(row, data_root)
        if (entry.get("kind") or "physics-anchor") == "physics-anchor":
            out[str(entry["id"])] = entry
    return out


def load_dimensions(data_root=None):
    out = {}
    for row in _rows(data_root, types=("legacy_dimension",), statuses=("active",)):
        entry = _pack_entry(row, data_root)
        fam = str(entry.get("family") or "*")
        out.setdefault(fam, []).append(entry)
    return out


def load_items(data_root=None):
    out = {}
    for row in _rows(data_root, types=("legacy_surface",)):
        entry = _pack_entry(row, data_root)
        out[str(entry["id"])] = entry
    return out


# ---------- atoms ----------

def _atom_from_entry(entry, row=None):
    t = entry.get("t")
    if t is None:
        created = entry.get("_created_at") or (row or {}).get("created_at")
        t = time.time()
        if created:
            try:
                import datetime
                t = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return {"id": str(entry.get("id")), "t": t, "kind": entry.get("kind") or "observation",
            "fact": str(entry.get("fact") or entry.get("statement") or "")[:400],
            "refs": list(entry.get("refs") or []), "axes": entry.get("axes") or {}}


def load_atoms(data_root=None):
    out = []
    for row in _rows(data_root, types=("legacy_atom",), statuses=_OPEN_STATUSES):
        entry = _pack_entry(row, data_root)
        entry.setdefault("_created_at", row["created_at"])
        out.append(_atom_from_entry(entry, row))
    handle = kb(data_root)
    q = ("SELECT e.entry_id, e.status, r.fields_json, r.created_at, r.rev_id FROM entry e JOIN entry_revision r "
         "ON r.rev_id=e.head_rev WHERE e.type_id='observation' AND e.status IN ('active','trial') "
         "ORDER BY r.created_at")
    for row in handle.conn.execute(q):
        fields = json.loads(row["fields_json"])
        cond = _conditions(handle, row["rev_id"])
        sources = [s["locator"] for s in _sources(handle, row["rev_id"])]
        out.append({"id": row["entry_id"], "t": _ts(row["created_at"]), "kind": cond.get("legacy_kind") or "observation",
                    "fact": str(fields.get("statement", ""))[:400], "refs": sources, "axes": {}})
    out.sort(key=lambda a: a["t"])
    return out


def _sources(handle, rev_id):
    return [dict(r) for r in handle.conn.execute(
        "SELECT s.source_type_id, s.title, s.locator, es.role FROM entry_source es JOIN source s "
        "ON s.source_id=es.source_id WHERE es.rev_id=? ORDER BY s.source_id", (rev_id,))]


def _ts(iso):
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def current_value(kind):
    matching = [a for a in load_atoms() if a.get("kind") == kind]
    return max(matching, key=lambda x: x.get("t", 0)) if matching else None


def find_scenario_semantic(query, top_k=5, data_root=None):
    keywords = set(str(query).lower().split())
    scored = []
    for a in load_atoms(data_root):
        fact = str(a.get("fact", "")).lower()
        k = str(a.get("kind", "")).lower()
        overlap = sum(1 for kw in keywords if kw in fact or kw in k)
        if overlap > 0:
            scored.append((overlap, a))
    scored.sort(key=lambda x: (-x[0], -x[1].get("t", 0)))
    return [a for _, a in scored[:top_k]]


def find_scenario_weighted(signature, data_root=None):
    matching = [a for a in load_atoms(data_root) if a.get("kind") == signature]
    if not matching:
        return None
    weighted = [(_weight(a), a) for a in matching]
    weighted.sort(key=lambda x: -x[0])
    best = weighted[0]
    return {"signature": signature, "count": len(matching), "best": best[1], "weight": best[0],
            "atom_ids": [a.get("id") for _, a in weighted[:5]]}


def _weight(atom):
    age_days = (time.time() - atom.get("t", 0)) / 86400
    return 0.5 ** (age_days / 30)


def rebuild(data_root=None):
    """scenarios/SOP are legacy derived views; UKB keeps no such projection."""
    return []


# ---------- write path ----------

def _system_admin():
    root = ukb_root(None)
    path = os.path.join(root, "system_auto.token")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def _mechanical_prereview(handle, task_id, submitter):
    """Deterministic prereview for machine observations: structural checks only, recorded as an llm opinion."""
    from ukb.core import navigate, registry
    try:
        bundle = handle.prereview_bundle(task_id, 0)
    except Exception:
        return False
    checklist = bundle["instructions"]["checklist"]
    pending = [i["content"] for i in bundle["items"] if i["part"] == "pending"][0]
    tags_ok = bool(pending.get("tags"))
    opinion = {"duplicate_of": [], "reinforces": [], "contradicts": [], "refines": [], "supersedes": [],
               "type_opinion": {"ok": True, "note": "machine observation, type observation"},
               "tag_opinion": {"ok": tags_ok, "note": "default topic tag" if tags_ok else "no tag"},
               "checklist": [{"item": c, "ok": True, "note": "structural check"} for c in checklist],
               "recommendation": "approve" if tags_ok else "request_changes",
               "note": "mechanical prereview for machine observation (no semantic judgement)"}
    tokens = []
    for page in range(bundle["pages"]):
        part = handle.prereview_bundle(task_id, page)
        tokens.append(part["page_token"])
    handle.prereview_submit(task_id, "llm:mdagent-auto", tokens, opinion)
    return True


def _land(task_id, rationale, data_root=None):
    token = _system_admin()
    if not token:
        return False
    handle = kb(data_root)
    view = handle.review_view(task_id)
    handle.decide(task_id, SYSTEM_ADMIN, token, "approve", rationale, view["bound_hash"], "cli")
    return True


def record_atom(kind, fact, refs=None, data_root=None):
    """Legacy signature. Machine observations are landed by the system administrator (option A)."""
    fact = str(fact)
    if len(fact) < 10:
        return None
    if len(fact) > 2000:
        fact = fact[:2000]
    if kind in ("friction-tool-error", "friction-interrupt") and "turn" in fact and "session" in fact:
        return None
    if isinstance(refs, str):
        refs = [refs]
    refs = [str(r) for r in list(refs or [])[:8]]
    sources = [{"source_type": "legacy_ref", "title": r.split(":")[0], "locator": r} for r in refs] or \
              [{"source_type": "legacy_ref", "title": "mdagent:observe", "locator": "mdagent:observe/%s" % kind}]
    payload = {"mode": "new", "type_id": "observation", "title": ("%s: %s" % (kind, fact))[:120],
               "fields": {"statement": fact, "context": kind},
               "tags": [{"facet": "topic", "tag": "general"}],
               "conditions": [{"key": "legacy_kind", "value": str(kind)}],
               "sources": sources,
               "origin": {"kind": "run", "ref": "mdagent:observe", "summary_of_evidence": "observe signal: " + kind}}
    handle = kb(data_root)
    try:
        result = handle.propose(payload, "mcp", "agent:mdagent-observe")
    except Exception:
        return None
    entry_id = result.get("entry_id")
    atom = {"id": entry_id, "t": time.time(), "kind": kind, "fact": fact[:400], "refs": refs, "axes": {}}
    if (os.environ.get(AUTO_OBS_ENV, "1") != "0" and result.get("task_id")):
        try:
            if _mechanical_prereview(handle, result["task_id"], "agent:mdagent-observe"):
                if not _land(result["task_id"], "machine observation auto-landed (type=observation, origin=run)",
                             data_root):
                    sys.stderr.write("[kb] warning: observation %s stayed candidate (no system token)\n"
                                     % entry_id)
        except Exception as exc:
            sys.stderr.write("[kb] warning: observation %s stayed candidate: %s\n" % (entry_id, exc))
    return atom


def retract_atom(atom_id, reason="", data_root=None):
    return _status_change(atom_id, "revoked", reason or "retracted", data_root)


def supersede_atom(old_id, new_id, reason="", data_root=None):
    handle = kb(data_root)
    try:
        result = handle.propose_status(old_id, "superseded", reason or "superseded", "agent:mdagent-observe",
                                       by_entry=new_id)
    except Exception:
        return _status_change(old_id, "revoked", reason or "superseded", data_root)
    return _land(result["task_id"], "machine supersede", data_root)


def _status_change(entry_id, status, reason, data_root=None):
    handle = kb(data_root)
    try:
        result = handle.propose_status(entry_id, status, reason, "agent:mdagent-observe")
    except Exception:
        return False
    return _land(result["task_id"], "machine " + status, data_root)


# ---------- evidence_v3 retrieval ----------

_READ_CONTEXT = threading.local()


@contextlib.contextmanager
def _knowledge_read(data_root):
    path = (Path(ukb_root(data_root)) / "ukb.sqlite").resolve()
    contexts = getattr(_READ_CONTEXT, "connections", {})
    if str(path) in contexts:
        yield contexts[str(path)]
        return
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    handle = SimpleNamespace(conn=conn)
    _READ_CONTEXT.connections = contexts
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM entry").fetchone()
        if conn.execute("PRAGMA journal_mode").fetchone()[0].upper() == "WAL":
            raise RuntimeError("UKB task gates require rollback-journal mode; WAL cannot serialize approval commits")
        contexts[str(path)] = handle
        yield handle
    finally:
        contexts.pop(str(path), None)
        conn.close()


def _evidence_source_health(record):
    """Check current source bytes without changing evidence or executing recipes.

    Keep each declared raw source in the health fingerprint so raw-only changes
    invalidate dependent snapshots, even when the derived artifact is unchanged.
    """
    import stat
    try:
        from ..evidence_v3.contracts import file_hash, read_json
    except ImportError:
        from evidence_v3.contracts import file_hash, read_json

    def source_health(locator, expected):
        result = {"status": "error", "source": locator, "expected": expected, "actual": None}
        if (not isinstance(locator, str) or not locator.strip() or
                not isinstance(expected, str) or len(expected) != 64 or
                any(char not in "0123456789abcdef" for char in expected)):
            result["reason"] = "invalid-source-binding"
            return result
        try:
            path = Path(locator)
            if not path.is_absolute():
                result["reason"] = "source-not-absolute"
                return result
            # lstat also catches Windows junctions on Python 3.11, which has no
            # Path.is_junction(). Reject all reparse points, including ancestors.
            for component in (path, *path.parents):
                info = component.lstat()
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, "st_file_attributes", 0) &
                        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                    result["reason"] = "unsafe-source-path"
                    return result
                if component == path and not stat.S_ISREG(info.st_mode):
                    result["reason"] = "source-not-regular"
                    return result
            result["actual"] = file_hash(path)
            result["status"] = "ok" if result["actual"] == expected else "error"
            if result["status"] == "error":
                result["reason"] = "source-hash-mismatch"
        except Exception:
            result["reason"] = "source-unavailable"
        return result

    verification = record.get("verification")
    if not isinstance(verification, dict):
        return {"status": "error", "reason": "missing-verification"}
    measurement = record.get("source_kind") == "measurement"
    result = source_health(verification.get("source_locator"),
                           record.get("output_hash") if measurement else record.get("source_sha256"))
    required = False
    if measurement and result["status"] == "ok":
        try:
            document = read_json(verification["source_locator"])
            required = isinstance(document, dict) and document.get("schema_version") == 1
            if required and (not isinstance(document.get("raw_sources"), list) or
                             not document["raw_sources"] or
                             verification.get("raw_sources") != document["raw_sources"]):
                result.update(status="error", reason="measurement-raw-sources-mismatch")
        except Exception:
            result.update(status="error", reason="invalid-measurement-source")
    if "raw_sources" in verification or required:
        declared = verification.get("raw_sources")
        result["raw_sources"] = []
        if not isinstance(declared, list) or (required and not declared):
            result.update(status="error", reason="invalid-raw-sources")
        else:
            for source in declared:
                if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
                    health = {"status": "error", "reason": "invalid-raw-source"}
                else:
                    health = source_health(source["path"], source["sha256"])
                result["raw_sources"].append(health)
                if health["status"] != "ok":
                    result.update(status="error", reason="raw-source-unhealthy")
    return result


def _review_metadata(handle, row):
    conditions = [dict(r) for r in handle.conn.execute(
        "SELECT * FROM entry_condition WHERE rev_id=? ORDER BY key_id,op", (row["head_rev"],))]
    sources = [dict(r) for r in handle.conn.execute(
        "SELECT s.*, es.role, es.locator_detail, es.excerpt FROM entry_source es JOIN source s "
        "ON s.source_id=es.source_id WHERE es.rev_id=? ORDER BY s.source_id,es.role", (row["head_rev"],))]
    reinforced = [dict(r) for r in handle.conn.execute(
        "SELECT rf.*, s.source_type_id,s.locator,s.title,s.grade,s.independence_key FROM reinforcement rf "
        "JOIN source s ON s.source_id=rf.source_id WHERE rf.entry_id=? ORDER BY rf.reinforce_id", (row["entry_id"],))]
    tags = [dict(r) for r in handle.conn.execute(
        "SELECT et.*, t.facet_id,t.slug,t.status FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id "
        "WHERE et.entry_id=? AND et.state='confirmed' ORDER BY et.tag_id", (row["entry_id"],))]
    approvals = [dict(r) for r in handle.conn.execute(
        "SELECT a.*,d.decision_id,d.reviewer_id,d.decision,d.rationale,d.bound_hash FROM rev_approval a "
        "JOIN review_decision d ON d.task_id=a.task_id AND d.reviewer_kind='admin' "
        "AND d.decision IN ('approve','approve_as_trial','correct') WHERE a.rev_id=? ORDER BY d.decision_id",
        (row["head_rev"],))]
    evidence_health = None
    if row["type_id"] == "evidence_claim":
        fields = json.loads(row["fields_json"])
        raw = json.loads(fields["raw_json"])
        verification = raw.get("verification")
        if isinstance(verification, dict) or raw.get("source_kind") is not None:
            evidence_health = _evidence_source_health(raw)
    return {"conditions": conditions, "sources": sources, "reinforcements": reinforced,
            "tags": tags, "approvals": approvals, "evidence_health": evidence_health}


def load_knowledge(data_root=None, knowledge_dirs=None):
    with _knowledge_read(data_root) as handle:
        return _load_knowledge(handle, data_root)


def _load_knowledge(handle, data_root):
    entries, health = [], []
    for row in _rows(data_root, handle=handle):
        entry = _pack_entry(row, data_root, handle=handle)
        lifecycle = entry.get("status") or "candidate"
        if entry.get("_consumer") not in (None, "", "evidence", "evidence_v3", "design_review"):
            continue
        if lifecycle in ("revoked", "superseded"):
            continue
        metadata = _review_metadata(handle, row)
        approved = handle.conn.execute(
            "SELECT d.decision_id, d.reviewer_id, d.rationale FROM rev_approval a "
            "JOIN review_decision d ON d.task_id=a.task_id AND d.reviewer_kind='admin' "
            "AND d.decision IN ('approve','approve_as_trial','correct') "
            "WHERE a.rev_id=? ORDER BY d.decided_at DESC LIMIT 1", (row["head_rev"],)).fetchone()
        if lifecycle in ("active", "canary", "deprecated") and approved is None:
            raise RuntimeError("UKB approved entry has no authenticated review decision")
        if (metadata.get("evidence_health") or {}).get("status") == "error":
            health.append({"channel": "ukb", "source": "ukb:" + row["entry_id"],
                           "status": "error", "reason": "evidence-source-changed",
                           "expected": metadata["evidence_health"].get("expected"),
                           "actual": metadata["evidence_health"].get("actual")})
            continue
        sources = metadata["sources"] + metadata["reinforcements"]
        refs = sorted({s["locator"] for s in sources if s.get("locator")})
        refs = refs or entry.get("source_refs") or entry.get("refs") or []
        content = entry.get("question_or_claim") or entry.get("statement") or entry.get("question")
        content = content or (entry.get("content") or {}).get("question_or_claim") or entry.get("quantity") or entry.get("id")
        approval = ({"actor_kind": "human", "status": "approved", "actor_id": approved["reviewer_id"],
                     "event_id": approved["decision_id"], "reason": approved["rationale"],
                     "content_hash": row["content_hash"]} if approved is not None else None)
        entries.append({"id": str(entry["id"]), "channel": "ukb", "source_locator": "ukb:" + row["entry_id"],
                        "pack": entry.get("_pack") or row["type_id"],
                        "version": handle.conn.execute("SELECT rev_no FROM entry_revision WHERE rev_id=?",
                                                       (row["head_rev"],)).fetchone()[0],
                        "lifecycle": lifecycle, "source_refs": refs or ["ukb:" + row["entry_id"]],
                        "provenance": {"source": "ukb-reviewed"}, "conditions": entry.get("conditions") or {},
                        "approval": approval, "candidate_only": approval is None,
                        "limitations": [] if approval else ["ukb-candidate"],
                        "content": content, "record": entry, "ukb_metadata": metadata})
    health.append({"channel": "ukb", "source": ukb_root(data_root), "status": "ok", "count": len(entries)})
    generation = hashlib.sha256(json.dumps(
        entries,
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"entries": entries, "manifest": [], "health": health, "generation": generation}


def _corpus_records(data_root, health):
    """External corpus stays a read-only reference channel (not internal knowledge, not reviewed).

    It is not imported into UKB: it is unverified third-party material and must not look like reviewed knowledge.
    """
    try:
        from ..evidence_v3 import retrieval as legacy
    except ImportError:
        from evidence_v3 import retrieval as legacy
    root = Path(data_root).resolve()
    code_root = Path(__file__).resolve().parents[2]
    out = []
    for directory in sorted({str(root / "external_corpus"), str(code_root / "external_corpus")}):
        folder = Path(directory)
        try:
            if not folder.exists():
                health.append({"channel": "local_corpus", "source": str(folder), "status": "unavailable",
                               "reason": "missing-directory"})
                continue
            files = sorted(folder.glob("*.jsonl"))
            for path in files:
                for position, row in legacy._read_rows(path, "local_corpus", [], health, True):
                    locator = str(path) + "#" + str(position)
                    if not isinstance(row.get("id"), str) or not row["id"].strip():
                        continue
                    if row.get("status") in ("revoked", "superseded"):
                        continue
                    out.append({"id": "corpus:" + row["id"], "channel": "local_corpus",
                                "source_locator": locator,
                                "source_refs": [row.get("url") or row["id"]],
                                "conditions": row.get("conditions", {}) if isinstance(
                                    row.get("conditions", {}), dict) else {},
                                "candidate_only": True, "lifecycle": "candidate",
                                "verification": "unverified", "record": row})
        except (OSError, ValueError):
            continue
    return out


def search(intent, data_root, knowledge_dirs=None, limit=20):
    try:
        from ..evidence_v3 import planning
    except ImportError:
        from evidence_v3 import planning
    try:
        from ..evidence_v3.contracts import canonical_hash
    except ImportError:
        from evidence_v3.contracts import canonical_hash
    intent = planning.validate_intent(intent)
    knowledge = load_knowledge(data_root)
    records = list(knowledge["entries"])
    health = list(knowledge["health"])
    records.extend(_corpus_records(data_root, health))
    queries = _queries(intent)
    ranked, exclusions = [], []
    for entry in records:
        hay = json.dumps(entry["record"], ensure_ascii=False, sort_keys=True).casefold()
        hay_tokens = _tokens(hay)
        matches = []
        for query in queries:
            hits = sorted(t for t in _tokens(query["text"]) if t in hay_tokens)
            substring = len(query["text"].strip()) > 1 and query["text"].casefold() in hay
            if hits or substring:
                matches.append({"query_id": query["id"], "tokens": hits, "substring": substring})
        if not matches:
            exclusions.append({"id": entry["id"], "source": entry["source_locator"], "reason": "no-lexical-match"})
            continue
        candidate = dict(entry)
        candidate["matches"] = matches
        candidate["score"] = sum(len(m["tokens"]) + int(m["substring"]) for m in matches)
        candidate["applicability"] = _condition_match(entry.get("conditions") or {}, intent["conditions"])
        candidate["source_group"] = canonical_hash(sorted(set(entry["source_refs"])))
        ranked.append(candidate)
    ranked.sort(key=lambda c: (-c["score"], c["id"], c["source_locator"]))
    unique, seen = [], set()
    for c in ranked:
        key = canonical_hash([c["source_group"], json.dumps(c["record"], sort_keys=True, ensure_ascii=False)])
        if key in seen:
            exclusions.append({"id": c["id"], "source": c["source_locator"], "reason": "duplicate-source"})
            continue
        seen.add(key)
        unique.append(c)
    unique = unique[:limit]
    if not isinstance(intent, dict):
        intent = {}
    limitations = ["top-k-is-not-plan-scope"] if len(ranked) > limit else []
    if any(h["status"] in ("error", "unavailable") for h in health):
        limitations.append("some-sources-not-available-or-healthy")
    bundle = {"schema_version": 3, "intent_hash": canonical_hash(intent), "queries": queries,
              "counterexample_queries": [q["id"] for q in queries if q.get("purpose") == "counterexample"],
              "health": health, "manifest": knowledge["manifest"],
              "knowledge_generation": knowledge["generation"], "candidates": unique,
              "exclusions": exclusions,
              "truncation": {"limit": limit, "total_matches": len(ranked),
                               "omitted_ids": [c["id"] for c in ranked[limit:]]},
              "limitations": limitations,
              "status": "error" if any(h["status"] == "error" for h in health)
              else ("ok" if unique else "empty")}
    bundle["bundle_hash"] = canonical_hash(bundle)
    return bundle


def _tokens(text):
    """Same tokenizer as the legacy recall: latin words, whole CJK runs, plus CJK 2-grams."""
    import re
    lowered = str(text).casefold()
    tokens = set(re.findall(r"[a-z0-9_]+", lowered))
    for segment in re.findall(r"[㐀-鿿]+", lowered):
        tokens.add(segment)
        tokens.update(segment[i:i + 2] for i in range(len(segment) - 1))
    return tokens


def _queries(intent):
    texts = []
    for raw in intent.get("queries") or []:
        texts.append(raw.get("text") if isinstance(raw, dict) else raw)
    for goal in intent.get("goals") or []:
        if isinstance(goal, dict):
            texts.append(goal.get("text"))
    texts.append(intent.get("question"))
    texts.extend(intent.get("expanded_queries") or [])
    out = []
    for text in texts:
        if isinstance(text, str) and text.strip() and text not in [q["text"] for q in out]:
            out.append({"id": "q%d" % len(out), "text": text})
    return out or [{"id": "q0", "text": ""}]


def _condition_match(expected, actual):
    """Exact structural comparison only; semantic applicability stays unknown (same as legacy)."""
    try:
        from ..evidence_v3.contracts import canonical_hash
    except ImportError:
        from evidence_v3.contracts import canonical_hash
    missing, different = [], []
    for key, value in (expected or {}).items():
        if key not in (actual or {}):
            missing.append(key)
        elif canonical_hash(value) != canonical_hash(actual[key]):
            different.append(key)
    return {"status": "unknown", "reason": "semantic-review-required",
            "exact_conditions": bool(expected) and not missing and not different,
            "missing": missing, "different": different}


# ---------- evidence_v3 evidence submissions and knowledge publication ----------

def submit_evidence_record(record, task_id, data_root=None):
    """Submit a verified reusable evidence claim as a UKB candidate.

    The immutable task artifact remains authoritative for the run; UKB stores only a
    reviewable projection. No reviewer identity is inferred from the task record.
    """
    try:
        from ..evidence_v3.contracts import V3Error, canonical_hash
    except ImportError:
        from evidence_v3.contracts import V3Error, canonical_hash
    try:
        from ..evidence_v3.store import TaskStore, exclusive_lock
    except ImportError:
        from evidence_v3.store import TaskStore, exclusive_lock
    store = TaskStore(resolve_data_root(data_root))
    with store.transaction(task_id):
        state = store.load(task_id)
        if not isinstance(record, dict) or record.get("task_id") != task_id:
            raise V3Error("EVIDENCE_TASK_MISMATCH")
        matches = [e for e in state["evidence"] if e.get("id") == record.get("id")]
        if len(matches) != 1 or canonical_hash(matches[0]) != canonical_hash(record):
            raise V3Error("EVIDENCE_RECORD_MISMATCH")
        store.verify_artifact(task_id, "evidence", matches[0])
        _verify_evidence_source(state, record, store)
        # Serialize retries for this projection without retaining another knowledge copy.
        with exclusive_lock(Path(ukb_root(data_root)) / ".evidence-intake.lock"):
            handle = kb(data_root)
            rows = handle.conn.execute(
                "SELECT t.task_id, t.entry_id, t.state, r.fields_json FROM review_task t "
                "JOIN entry_revision r ON r.rev_id=t.rev_id WHERE t.submitter='agent:evidence-v3' "
                "AND t.kind='new' ORDER BY t.opened_at").fetchall()
            for row in rows:
                fields = json.loads(row["fields_json"])
                if fields.get("raw_json") == json.dumps(record, ensure_ascii=False):
                    return {"status": "pending_review" if row["state"] == "open" else row["state"],
                            "task_id": row["task_id"], "entry_id": row["entry_id"], "record_id": record["id"]}
            return _submit_evidence_candidate(record, task_id, data_root)


def _verify_evidence_source(state, record, store):
    try:
        from ..evidence_v3 import policy
    except ImportError:
        from evidence_v3 import policy
    try:
        from ..evidence_v3.contracts import V3Error
    except ImportError:
        from evidence_v3.contracts import V3Error
    policy._evidence(record)
    verification = record["verification"]
    health = _evidence_source_health(record)
    if health["status"] != "ok":
        raise V3Error("EVIDENCE_SOURCE_UNVERIFIED", health.get("reason", "source-unhealthy"))
    if record["source_kind"] == "measurement":
        run = next((r for r in state["runs"] if r["id"] == record.get("run_id")), None)
        if run is None:
            raise V3Error("MEASUREMENT_FINISHED_RUN_REQUIRED")
        if (run.get("needs_revalidation") or run.get("knowledge_snapshot_hash") !=
                (state.get("knowledge_snapshot") or {}).get("hash")):
            raise V3Error("STALE_RUN_KNOWLEDGE")
        receipt = next((r for r in state["receipts"] if r["id"] == run.get("receipt_id")), None)
        if not receipt or receipt.get("kind") != "RUN_RECEIPT":
            raise V3Error("RUN_RECEIPT_REQUIRED")
        store.verify_artifact(state["task_id"], "receipts", receipt)
        expected = {key: record[key] for key in
                    ("claim_id", "metric", "unit", "window", "tolerance") if key in record}
        policy._measurement({"claim_id": record["claim_id"], "measurement": expected},
                            [record], dict(state, run_id=run["id"]))
    elif verification["method"] != "content_review":
        raise V3Error("EVIDENCE_VERIFIER_UNSUPPORTED")


def _submit_evidence_candidate(record, task_id, data_root):
    try:
        from ..evidence_v3.contracts import V3Error, canonical_hash
    except ImportError:
        from evidence_v3.contracts import V3Error, canonical_hash
    if not isinstance(record, dict) or record.get("lifecycle") != "active":
        return {"status": "not_submitted", "reason": "evidence-not-active"}
    verification = record.get("verification")
    if not isinstance(verification, dict) or verification.get("status") != "verified":
        return {"status": "not_submitted", "reason": "evidence-not-verified"}
    claim = str(record.get("claim") or record.get("statement") or record.get("content") or "").strip()
    refs = [str(x) for x in (record.get("provenance_refs") or []) if str(x).strip()]
    locator = str(verification.get("source_locator") or "").strip()
    if not claim or not refs or not locator:
        raise V3Error("EVIDENCE_UKB_PROJECTION_REQUIRED")
    sources = [{"source_type": "legacy_ref", "title": str(record.get("source_kind") or "evidence"),
                "locator": ref, "independence_key": ref} for ref in refs]
    sources.append({"source_type": "legacy_ref", "title": "verified-evidence", "locator": locator,
                    "independence_key": locator})
    proposal = {"mode": "new", "type_id": "evidence_claim",
                "title": ("[evidence:%s] %s" % (task_id, claim))[:120],
                "fields": {"statement": claim,
                           "scope_note": "task_id=" + str(task_id) + "; record_hash=" + canonical_hash(record),
                           "raw_json": json.dumps(record, ensure_ascii=False)},
                "tags": [{"facet": "topic", "tag": "evidence"}],
                "conditions": [{"key": "legacy_id", "value": "evidence:" + str(task_id) + ":" + str(record["id"])},
                               {"key": "legacy_consumer", "value": "evidence"}],
                "sources": sources,
                "origin": {"kind": "task", "ref": "evidence_v3:" + str(task_id),
                           "summary_of_evidence": "verified task evidence projection; UKB review required"}}
    result = kb(data_root).propose(proposal, "mcp", "agent:evidence-v3")
    return {"status": "pending_review", "task_id": result["task_id"],
            "entry_id": result["entry_id"], "record_id": record["id"],
            "limitations": ["immutable-task-artifact-remains-authoritative", "requires-ukb-approval"]}


def knowledge_snapshot(data_root):
    """Bind task dependencies to approved UKB revisions, without reading legacy packages."""
    try:
        from ..evidence_v3.contracts import canonical_hash
    except ImportError:
        from evidence_v3.contracts import canonical_hash
    entries = {}
    with _knowledge_read(data_root) as handle:
        for row in _rows(data_root, handle=handle):
            entry = _pack_entry(row, data_root, handle=handle)
            identity = str(entry["id"])
            if identity in entries:
                raise RuntimeError("UKB duplicate knowledge identity: " + identity)
            revision = handle.conn.execute("SELECT rev_no FROM entry_revision WHERE rev_id=?", (row["head_rev"],)).fetchone()[0]
            metadata = _review_metadata(handle, row)
            refs = sorted({s["locator"] for s in metadata["sources"] + metadata["reinforcements"]})
            material = {"entry": entry, "metadata": metadata}
            record = {"id": identity, "version": revision, "entry_hash": canonical_hash(material),
                      "source": "ukb:" + row["entry_id"], "conditions": entry.get("conditions") or {},
                      "source_refs": refs or entry.get("source_refs") or entry.get("refs") or [],
                      "status": entry["status"], "record": entry, "ukb_metadata": metadata}
            entries[identity] = record
    result = {"entries": entries, "packages": []}
    result["hash"] = canonical_hash(result)
    return result


def publish_knowledge(proposal, approver=None, data_root=None, proposal_hash=None):
    """Submit reusable evidence knowledge to UKB; only its authenticated reviewer can publish it."""
    try:
        from ..evidence_v3 import knowledge, policy
    except ImportError:
        from evidence_v3 import knowledge, policy
    try:
        from ..evidence_v3.contracts import V3Error, canonical_hash, validate_id
    except ImportError:
        from evidence_v3.contracts import V3Error, canonical_hash, validate_id
    if not isinstance(proposal, dict) or proposal.get("class") != knowledge.CLASS_NAME or proposal.get("tier") != "human":
        raise V3Error("knowledge-registered-human-class-required")
    if proposal.get("op") != "publish":
        raise V3Error("KNOWLEDGE_TRANSITION_UNSUPPORTED", "use an authenticated UKB status/revision review")
    validate_id(proposal.get("id"))
    if proposal.get("status") not in ("active", "canary"):
        raise V3Error("knowledge-invalid-status")
    if not isinstance(approver, str) or not approver.strip() or (proposal.get("review") or {}).get("actor_id") != approver:
        raise V3Error("knowledge-approver-mismatch")
    checked = policy.validate_promotion(proposal)
    method = proposal.get("verification_method")
    if not isinstance(method, dict) or method.get("kind") not in ("deterministic", "measurement", "semantic"):
        raise V3Error("knowledge-verification-method-required")
    if method["kind"] == "deterministic" and not method.get("checker_id"):
        raise V3Error("knowledge-checker-reference-required")
    if {"code", "command", "python", "shell"} & set(method):
        raise V3Error("knowledge-executable-content-forbidden")
    content = proposal.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("question_or_claim"), str) or not content["question_or_claim"].strip():
        raise V3Error("knowledge-content-required")
    if proposal.get("owner_rule") not in ("E1", "E2", "E3", "E4", "E5"):
        raise V3Error("knowledge-owner-rule-required")
    handle = kb(data_root)
    for row in _rows(data_root):
        existing = _pack_entry(row, data_root)
        if str(existing["id"]) == proposal["id"]:
            raise V3Error("knowledge-duplicate-id")
    statement = content["question_or_claim"].strip()
    sources = [{"source_type": "legacy_ref", "title": src["id"], "locator": src["source_locator"]}
               for src in proposal["sources"] if src.get("source_locator")]
    if not sources:
        sources = [{"source_type": "legacy_ref", "title": "original", "locator": ref}
                   for ref in checked["independent_sources"]]
    payload = {"mode": "new", "type_id": "evidence_claim",
               "title": ("[evidence:%s] %s" % (proposal["kind"], statement))[:120],
               "fields": {"statement": statement,
                          "scope_note": "owner_rule=" + str(proposal["owner_rule"]),
                          "raw_json": json.dumps(proposal, ensure_ascii=False)},
               "tags": [{"facet": "topic", "tag": "evidence"}],
               "conditions": [{"key": "legacy_id", "value": proposal["id"]},
                              {"key": "legacy_kind", "value": proposal["kind"]},
                              {"key": "legacy_consumer", "value": "evidence"}],
               "sources": sources,
              "origin": {"kind": "task", "ref": "evidence_v3:knowledge",
                         "summary_of_evidence": "submitted for UKB review; previous approval is not UKB approval"}}
    result = handle.propose(payload, "mcp", "agent:evidence_v3")
    return {"status": "pending_review", "entry": {"id": proposal["id"], "status": "candidate"},
            "task_id": result["task_id"], "event_id": "ukb:" + result["entry_id"],
            "path": ukb_root(data_root), "limitations": ["requires-ukb-prereview-and-authenticated-admin-approval"]}
