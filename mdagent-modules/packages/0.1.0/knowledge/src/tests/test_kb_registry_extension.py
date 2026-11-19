"""Registry integration tests use isolated populated SQLite stores only."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tools.kb import ensure_evidence as tool


class RegistryExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="registry-extension-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ukb"
        kb = tool.UKB.init(str(self.root), actor="fixture")
        try:
            token = kb.add_admin("fixture", "fixture")
            proposed = kb.propose({"mode": "new", "type_id": "claim", "title": "populated fixture",
                "fields": {"statement": "existing populated fixture"},
                "tags": [{"facet": "topic", "tag": "general"}],
                "sources": [{"source_type": "literature", "title": "fixture", "locator": "fixture:source"}]},
                "cli", "fixture")
            bundle = kb.prereview_bundle(proposed["task_id"], 0)
            opinion = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True},
                       "recommendation": "approve", "note": "synthetic fixture",
                       "checklist": [{"item": item, "ok": True} for item in bundle["instructions"]["checklist"]]}
            tokens = [kb.prereview_bundle(proposed["task_id"], page)["page_token"] for page in range(bundle["pages"])]
            kb.prereview_submit(proposed["task_id"], "llm:independent-reviewer", tokens, opinion)
            view = kb.review_view(proposed["task_id"])
            kb.decide(proposed["task_id"], "fixture", token, "approve", "fixture approval",
                      view["bound_hash"], "cli")
        finally:
            kb.close()

    def rows(self):
        conn = sqlite3.connect(str(self.root / "ukb.sqlite"))
        try:
            # Never read credential columns, even in this synthetic store.
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                      if r[0] != "reviewer"]
            return {table: sorted(conn.execute('SELECT * FROM "' + table + '"').fetchall(), key=repr)
                    for table in tables}
        finally:
            conn.close()

    def apply(self):
        return tool.ensure_evidence(self.root, apply=True)

    def test_populated_store_preserves_every_existing_row_and_id(self):
        before = self.rows()
        result = self.apply()
        after = self.rows()
        self.assertEqual(len(result["added"]), 8)
        self.assertEqual(result["before"]["entry"], 1)
        for table, rows in before.items():
            for row in rows:
                self.assertIn(row, after[table], table)
        for table in ("entry", "entry_revision", "review_task", "review_decision", "rev_approval", "entry_tag", "source"):
            self.assertEqual(before[table], after[table], table)
        backup = Path(result["backup"]["path"])
        self.assertEqual(tool._backup_hash(backup), result["backup"]["sha256"])
        conn = sqlite3.connect(str(backup))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM entry").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM reg_type WHERE type_id='evidence_claim'").fetchone()[0], 0)
        finally:
            conn.close()

    def test_rerun_skips_identical_without_backup_or_audit_change(self):
        self.apply()
        before = self.rows()
        with patch.object(tool.UKB, "backup", side_effect=AssertionError("unnecessary backup")):
            result = self.apply()
        self.assertEqual(result["added"], [])
        self.assertIsNone(result["backup"])
        self.assertEqual(len(result["skipped"]), 9)
        self.assertEqual(before, self.rows())

    def test_conflicting_existing_condition_is_never_overwritten(self):
        kb = tool.UKB(str(self.root))
        try:
            with tempfile.TemporaryDirectory() as directory:
                tool._atomic_json(Path(directory) / "condition_key.json",
                    [{"key_id": "legacy_id", "label": "incompatible", "value_type": "integer"}])
                kb.load_pack(directory, "fixture")
        finally:
            kb.close()
        before = self.rows()
        with self.assertRaises(tool.RegistryConflict):
            self.apply()
        self.assertEqual(before, self.rows())
        self.assertFalse((self.root / "backups").exists())

    def test_invalid_type_is_prevalidated_before_live_backup_or_write(self):
        original = tool._canonical_specs
        def invalid(root):
            specs = copy.deepcopy(original(root))
            specs["type"]["spec"]["render"] = ["unknown"]
            return specs
        before = self.rows()
        with patch.object(tool, "_canonical_specs", invalid):
            with self.assertRaises(Exception):
                self.apply()
        self.assertEqual(before, self.rows())
        self.assertFalse((self.root / "backups").exists())

    def test_existing_backup_destination_is_not_clobbered(self):
        destination = Path(self.tmp.name) / "existing.sqlite"
        destination.write_bytes(b"existing backup sentinel")
        before = self.rows()
        with patch.object(tool, "_backup_path", return_value=destination):
            with self.assertRaises(FileExistsError):
                self.apply()
        self.assertEqual(destination.read_bytes(), b"existing backup sentinel")
        self.assertEqual(before, self.rows())

    def test_concurrent_identical_insert_is_not_overwritten(self):
        original = tool.UKB.backup
        def insert_after_backup(handle, path):
            result = original(handle, path)
            other = tool.UKB(str(self.root))
            try:
                specs = tool._canonical_specs(REPO)
                with tempfile.TemporaryDirectory() as directory:
                    tool._atomic_json(Path(directory) / "condition_key.json", [specs["conditions"][0]])
                    other.load_pack(directory, "concurrent:writer")
            finally:
                other.close()
            return result
        with patch.object(tool.UKB, "backup", insert_after_backup):
            with self.assertRaisesRegex(RuntimeError, "registry changed"):
                self.apply()
        kb = tool.UKB(str(self.root))
        try:
            self.assertEqual(kb.conn.execute("SELECT count(*) FROM reg_condition_key WHERE key_id='legacy_id'").fetchone()[0], 1)
            self.assertEqual(kb.conn.execute("SELECT count(*) FROM audit_log WHERE actor=?", (tool.ACTOR,)).fetchone()[0], 0)
            self.assertEqual(kb.conn.execute("SELECT count(*) FROM entry").fetchone()[0], 1)
        finally:
            kb.close()
        self.assertEqual(len(self.apply()["added"]), 7)

    def test_read_only_plan_changes_no_rows(self):
        before = self.rows()
        result = tool.ensure_evidence(self.root)
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(before, self.rows())


if __name__ == "__main__":
    unittest.main()
