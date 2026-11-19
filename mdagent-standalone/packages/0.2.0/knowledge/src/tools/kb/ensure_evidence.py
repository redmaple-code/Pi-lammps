"""Add only missing evidence definitions through public UKB.load_pack.

Compare canonical evidence_claim, topic/evidence, legacy condition keys and
legacy_ref, including the topic facet prerequisite. Conflicts abort before writes.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
UKB_VENDOR = REPO / "ukb"
if str(UKB_VENDOR) not in sys.path:
    sys.path.insert(0, str(UKB_VENDOR))

from ukb.api import UKB  # noqa: E402
from ukb.core import registry  # noqa: E402
from ukb.core.canon import alias_norm, canonical_json  # noqa: E402

ACTOR = "agent:evidence-integration"
TYPE_ID = "evidence_claim"
TOPIC_FACET = "topic"
TARGET_TAGS = ("evidence",)
TARGET_CONDITIONS = ("legacy_id", "legacy_kind", "legacy_pack", "legacy_family", "legacy_consumer")
SOURCE_TYPE_ID = "legacy_ref"


class RegistryConflict(RuntimeError):
    """A canonical key exists but its definition differs."""

    def __init__(self, kind, key, differences):
        self.kind = kind
        self.key = key
        self.differences = differences
        super().__init__("registry conflict for %s:%s (%s)" %
                         (kind, key, ", ".join(differences)))


def _read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _canonical_specs(canonical_root):
    """Read only the public canonical registry files needed by this extension."""
    root = Path(canonical_root)
    legacy = root / "data" / "domain" / "mdagent_legacy"
    seed = root / "ukb" / "data" / "seed"
    type_spec = _read_json(legacy / "types" / (TYPE_ID + ".json"))
    facets = [item for item in _read_json(seed / "facet.json") if item.get("facet_id") == TOPIC_FACET]
    tags = [item for item in _read_json(legacy / "tag.json")
            if item.get("facet") == TOPIC_FACET and item.get("slug") in TARGET_TAGS]
    conditions = [item for item in _read_json(legacy / "condition_key.json")
                  if item.get("key_id") in TARGET_CONDITIONS]
    sources = [item for item in _read_json(legacy / "source_type.json")
               if item.get("source_type_id") == SOURCE_TYPE_ID]
    if type_spec.get("type_id") != TYPE_ID or len(facets) != 1 or len(tags) != 1 \
            or len(conditions) != len(TARGET_CONDITIONS) or len(sources) != 1:
        raise RuntimeError("canonical evidence registry definitions are incomplete")
    return {
        "type": type_spec,
        "facet": facets[0],
        "tags": tags,
        "conditions": conditions,
        "source_type": sources[0],
    }


def _active(value, default="active"):
    return value if value is not None else default


def _type_existing(conn, spec):
    row = conn.execute("SELECT * FROM reg_type WHERE type_id=?", (spec["type_id"],)).fetchone()
    if row is None:
        return None
    version = conn.execute(
        "SELECT * FROM reg_type_version WHERE type_id=? AND version=?",
        (spec["type_id"], row["current_version"])).fetchone()
    return {
        "type_id": row["type_id"], "label": row["label"], "description": row["description"],
        "status": row["status"], "spec": json.loads(version["schema_json"]) if version else None,
    }


def _facet_existing(conn, spec):
    row = conn.execute("SELECT * FROM reg_facet WHERE facet_id=?", (spec["facet_id"],)).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in
            ("facet_id", "label", "description", "multi", "governing", "scope_budget_chars", "ord", "status")}


def _tag_existing(conn, spec):
    row = conn.execute("SELECT * FROM tag WHERE facet_id=? AND slug=?",
                       (spec["facet"], spec["slug"])).fetchone()
    if row is None:
        return None
    aliases = [r[0] for r in conn.execute(
        "SELECT alias_norm FROM tag_alias WHERE tag_id=? ORDER BY alias_norm", (row["tag_id"],))]
    parent = None
    if row["parent_tag_id"] is not None:
        parent_row = conn.execute("SELECT slug FROM tag WHERE tag_id=?", (row["parent_tag_id"],)).fetchone()
        parent = parent_row[0] if parent_row else "<invalid-parent>"
    return {
        "facet": row["facet_id"], "slug": row["slug"], "label": row["label"],
        "description": row["description"], "parent": parent, "status": row["status"],
        "aliases": aliases,
    }


def _condition_existing(conn, spec):
    row = conn.execute("SELECT * FROM reg_condition_key WHERE key_id=?", (spec["key_id"],)).fetchone()
    if row is None:
        return None
    return {
        "key_id": row["key_id"], "label": row["label"], "value_type": row["value_type"],
        "unit": row["unit"], "enum": json.loads(row["enum_json"]) if row["enum_json"] else None,
        "status": row["status"],
    }


def _source_existing(conn, spec):
    row = conn.execute("SELECT * FROM reg_source_type WHERE source_type_id=?",
                       (spec["source_type_id"],)).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in ("source_type_id", "label", "default_grade", "status")}


def _normalized(kind, spec):
    """Normalize canonical JSON and SQLite representations for exact comparison."""
    if kind == "type":
        return {"type_id": spec["type_id"], "label": spec["label"],
                "description": spec["description"], "status": _active(spec.get("status")),
                "spec": spec.get("spec")}
    if kind == "facet":
        return {"facet_id": spec["facet_id"], "label": spec["label"],
                "description": spec["description"], "multi": int(bool(spec.get("multi", True))),
                "governing": int(bool(spec.get("governing", False))),
                "scope_budget_chars": spec.get("scope_budget_chars"), "ord": int(spec.get("ord", 0)),
                "status": _active(spec.get("status"))}
    if kind == "tag":
        return {"facet": spec["facet"], "slug": spec["slug"], "label": spec["label"],
                "description": spec["description"], "parent": spec.get("parent"),
                "status": _active(spec.get("status")),
                "aliases": sorted({alias_norm(value) for value in spec.get("aliases", [])})}
    if kind == "condition_key":
        return {"key_id": spec["key_id"], "label": spec["label"],
                "value_type": spec["value_type"], "unit": spec.get("unit"),
                "enum": spec.get("enum"), "status": _active(spec.get("status"))}
    if kind == "source_type":
        return {"source_type_id": spec["source_type_id"], "label": spec["label"],
                "default_grade": spec.get("default_grade", 0), "status": _active(spec.get("status"))}
    raise ValueError("unsupported comparison kind: %s" % kind)


def _differences(expected, actual):
    return sorted(key for key in set(expected) | set(actual)
                  if canonical_json(expected.get(key)) != canonical_json(actual.get(key)))


def _compare(kind, key, expected, actual):
    if actual is None:
        return "missing"
    expected_n = _normalized(kind, expected)
    actual_n = _normalized(kind, actual)
    differences = _differences(expected_n, actual_n)
    if differences:
        raise RegistryConflict(kind, key, differences)
    return "identical"


def _plan(kb, canonical_root):
    specs = _canonical_specs(canonical_root)
    conn = kb.conn
    pack = {"facet": [], "tags": [], "conditions": [], "source_type": [], "type": []}
    skipped = []

    checks = [("facet", TOPIC_FACET, specs["facet"], _facet_existing(conn, specs["facet"])),
              ("source_type", SOURCE_TYPE_ID, specs["source_type"], _source_existing(conn, specs["source_type"])),
              ("type", TYPE_ID, specs["type"], _type_existing(conn, specs["type"]))]
    for kind, key, expected, actual in checks:
        state = _compare(kind, key, expected, actual)
        if state == "missing":
            pack[kind].append(expected)
        else:
            skipped.append("%s:%s" % (kind, key))

    for spec in specs["tags"]:
        key = "%s/%s" % (spec["facet"], spec["slug"])
        state = _compare("tag", key, spec, _tag_existing(conn, spec))
        if state == "missing":
            pack["tags"].append(spec)
        else:
            skipped.append("tag:%s" % key)

    for spec in specs["conditions"]:
        key = spec["key_id"]
        state = _compare("condition_key", key, spec, _condition_existing(conn, spec))
        if state == "missing":
            pack["conditions"].append(spec)
        else:
            skipped.append("condition_key:%s" % key)

    return specs, pack, skipped


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _write_pack(pack):
    """Stage a minimal pack; every generated source file is written atomically."""
    root = Path(tempfile.mkdtemp(prefix="ukb-evidence-pack-"))
    try:
        for key in ("facet", "tags", "conditions", "source_type"):
            if pack[key]:
                _atomic_json(root / ("tag.json" if key == "tags" else
                                     "condition_key.json" if key == "conditions" else key + ".json"), pack[key])
        if pack["type"]:
            types = root / "types"
            types.mkdir()
            _atomic_json(types / (TYPE_ID + ".json"), pack["type"][0])
        return root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _pack_count(pack):
    return sum(len(items) for key, items in pack.items() if key != "tags") + len(pack["tags"])


def _pack_keys(pack):
    return {_registry_key(kind, spec) for kind, specs in pack.items() for spec in specs}


def _backup_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup_path(root, backup_dir):
    directory = Path(backup_dir) if backup_dir else Path(root) / "backups"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / ("evidence-registry-" + stamp + ".sqlite")


def _safe_counts(kb):
    """Counts intentionally omit the reviewer table and all credential-bearing columns."""
    tables = ("entry", "entry_revision", "review_task", "review_decision", "rev_approval", "audit_log",
              "reg_type", "reg_type_version", "reg_facet", "tag", "reg_condition_key", "reg_source_type")
    return {table: kb.conn.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in tables}


class _LockedPlanConnection:
    """Recheck the exact missing-key set after BEGIN IMMEDIATE acquires the lock."""
    def __init__(self, inner, canonical_root, expected_pack, data_version):
        self._inner = inner
        self._canonical_root = canonical_root
        self._expected_pack = canonical_json(expected_pack)
        self._data_version = data_version

    @property
    def in_transaction(self):
        return self._inner.in_transaction

    def execute(self, sql, parameters=()):
        result = self._inner.execute(sql, parameters)
        if sql.lstrip().upper() == "BEGIN IMMEDIATE":
            try:
                _, current, _ = _plan(type("Handle", (), {"conn": self._inner})(), self._canonical_root)
                if canonical_json(current) != self._expected_pack:
                    raise RuntimeError("registry changed since planning; refusing write, rerun to recompare")
                current_version = self._inner.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                if current_version != self._data_version:
                    raise RuntimeError("database schema changed since planning; refusing write")
            except BaseException:
                self._inner.execute("ROLLBACK")
                raise
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _registry_key(kind, spec):
    kind = {"tags": "tag", "conditions": "condition_key"}.get(kind, kind)
    keys = {"type": "type_id", "facet": "facet_id", "condition_key": "key_id",
            "source_type": "source_type_id"}
    if kind == "tag":
        return "tag", "%s/%s" % (spec["facet"], spec["slug"])
    return kind, spec[keys[kind]]


def _validate_pack(kb, pack_dir):
    """Exercise public load_pack against an isolated SQLite backup before live apply."""
    with tempfile.TemporaryDirectory(prefix="ukb-evidence-validate-") as directory:
        destination = sqlite3.connect(str(Path(directory) / "ukb.sqlite"))
        try:
            kb.conn.backup(destination)
        finally:
            destination.close()
        isolated = UKB(directory)
        try:
            isolated.load_pack(str(pack_dir), ACTOR)
        finally:
            isolated.close()


def ensure_evidence(db_root, canonical_root=None, apply=False, backup_dir=None):
    """Plan or apply the evidence registry extension without overwrites."""
    canonical_root = Path(canonical_root or REPO)
    kb = UKB(str(db_root))
    try:
        before = _safe_counts(kb)
        _, pack, skipped = _plan(kb, canonical_root)
        result = {"actor": ACTOR, "applied": bool(apply), "before": before,
                  "added": [], "skipped": sorted(skipped), "backup": None}
        if not apply or _pack_count(pack) == 0:
            result["after"] = before
            return result
        pack_dir = _write_pack(pack)
        try:
            _validate_pack(kb, pack_dir)
            backup = _backup_path(db_root, backup_dir)
            backup_info = kb.backup(str(backup))
            snapshot = sqlite3.connect(Path(backup_info["path"]).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite backup failed integrity validation")
            finally:
                snapshot.close()
            result["backup"] = {"path": backup_info["path"], "sha256": _backup_hash(backup_info["path"])}
            expected_pack = {key: list(value) for key, value in pack.items()}
            original_conn = kb.conn
            kb.conn = _LockedPlanConnection(original_conn, canonical_root, expected_pack,
                                            original_conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            try:
                kb.load_pack(str(pack_dir), ACTOR)
            finally:
                kb.conn = original_conn
        finally:
            shutil.rmtree(pack_dir, ignore_errors=True)
        result["added"] = sorted(
            ["facet:" + x["facet_id"] for x in pack["facet"]] +
            ["source_type:" + x["source_type_id"] for x in pack["source_type"]] +
            ["type:" + x["type_id"] for x in pack["type"]] +
            ["tag:%s/%s" % (x["facet"], x["slug"]) for x in pack["tags"]] +
            ["condition_key:" + x["key_id"] for x in pack["conditions"]])
        result["after"] = _safe_counts(kb)
        return result
    finally:
        kb.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ensure evidence registry definitions in a UKB store")
    parser.add_argument("--db-root", default=str(REPO / "data" / "ukb"))
    parser.add_argument("--canonical-root", default=str(REPO))
    parser.add_argument("--backup-dir")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = ensure_evidence(args.db_root, args.canonical_root, args.apply, args.backup_dir)
    except RegistryConflict as exc:
        print(json.dumps({"error": "REGISTRY_CONFLICT", "kind": exc.kind, "key": exc.key,
                          "differences": exc.differences}, ensure_ascii=False), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
