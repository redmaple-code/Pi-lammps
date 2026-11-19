"""Byte-exact migration tests using real temporary task stores."""
import concurrent.futures
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evolution"))
from evidence_v3 import migration
from evidence_v3.contracts import V3Error, canonical_hash
from evidence_v3.service import TaskService
from evidence_v3.store import TaskStore


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.source = self.root / "legacy" / "object-a"
        self.source.mkdir(parents=True)
        self.intent = {"action": "design", "goals": [{"id": "goal-a", "text": "goal-a"}],
                       "conditions": {}, "unknowns": [], "source_refs": ["source-a"]}
        self.service = TaskService(self.data)
        self.write(self.source / "state.json", {"schema_version": 1, "case_id": "object-a", "status": "active",
                                                "decisions": {"a": "GO"}, "gate": "PASS"})
        self.write(self.source / "intent.json", {"id": "object-a", "choice": "ACCEPT"})
        (self.source / "binary.dat").write_bytes(b"\x00\xff\r\n")

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(value, indent=2).encode("utf-8") + b"\r\n")

    def tree(self, path):
        if not path.exists():
            return {}
        return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}

    def migrate(self, **updates):
        arguments = dict(source_path=self.source, service=self.service, task_id="task-new",
                         intent=self.intent, request_id="request-a")
        arguments.update(updates)
        return migration.migrate(**arguments)

    def test_read_only_inventory_and_exact_manifest(self):
        before = self.tree(self.root)
        result = migration.inventory(self.data, [self.source.parent])
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.tree(self.root), before)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["source_path"], str(self.source))
        self.assertEqual(candidate["manifest_hash"], canonical_hash(candidate["manifest"]))
        manifest = {row["path"]: row for row in candidate["manifest"]}
        self.assertEqual(set(manifest), {"state.json", "intent.json", "binary.dat"})
        self.assertEqual(manifest["binary.dat"]["sha256"], hashlib.sha256(b"\x00\xff\r\n").hexdigest())
        self.assertTrue(result["read_only"])
        self.assertFalse(self.data.exists())

    def test_inventory_default_roots_and_same_name_ambiguity(self):
        other = self.data / "memory" / "tasks" / "object-a"
        self.write(other / "state.json", {"schema_version": 1, "case_id": "object-a"})
        result = migration.inventory(self.data, [self.source])
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["ambiguities"][0]["candidates"], sorted([str(other), str(self.source)]))
        self.assertTrue(result["ambiguities"][0]["selection_required"])

    def test_inventory_same_alias_different_names_and_multiple_candidates(self):
        other = self.source.parent / "object-b"
        self.write(other / "intent.json", {"id": "object-a"})
        result = migration.inventory(self.data, [self.source.parent, self.source])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["status"], "ambiguous")
        with self.assertRaises(V3Error):
            self.migrate(source_path=self.source.parent)
        self.assertFalse((self.data / "memory" / "tasks" / "task-new" / "state.json").exists())

    def test_inventory_corruption_missing_root_and_current_schema_visible(self):
        other = self.source.parent / "object-b"
        other.mkdir()
        (other / "state.json").write_text("{", encoding="utf-8")
        current = self.service.create("current-a", self.intent)
        before = self.tree(self.root)
        result = migration.inventory(self.data, [self.source.parent, self.root / "missing"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(result["current"][0]["schema_version"], current["schema_version"])
        self.assertEqual(self.tree(self.root), before)

    def test_migration_preserves_every_source_byte_and_grants_no_authority(self):
        before = self.tree(self.source)
        state = self.migrate()
        self.assertEqual(self.tree(self.source), before)
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(state["current_stage"], "PLAN")
        self.assertEqual(state["status"], "REVALIDATION_REQUIRED")
        self.assertTrue(state["revalidation_required"])
        for key in ("evidence", "reviews", "rule_results", "receipts", "permits", "runs", "authorizations"):
            self.assertEqual(state[key], [], key)
        self.assertEqual(state["active_receipts"], {})
        self.assertIsNone(state["plan"])
        record = state["migration"]
        self.assertTrue(record["historical_only"])
        backup = self.service.store.task_dir("task-new") / record["backup_directory"]
        self.assertEqual(self.tree(backup), before)
        for row in record["manifest"]:
            self.assertEqual(row["sha256"], hashlib.sha256((backup / row["path"]).read_bytes()).hexdigest())
        self.assertEqual(self.service.read("task-new")["subject_hash"], state["subject_hash"])
        self.assertEqual(self.service.evaluate("task-new")["verdict"], "BLOCK")
        for path in self.data.rglob("*"):
            if path.is_file():
                self.assertTrue(path.is_relative_to(self.data / "memory" / "tasks"), path)
        self.assertFalse((self.data / "memory" / "atoms.jsonl").exists())
        self.assertFalse((self.data / "review_experience").exists())

    def test_old_completed_and_running_statuses_are_history_only(self):
        self.write(self.source / "state.json", {"schema_version": 1, "status": "completed",
                                                "runs": [{"id": "run-a", "status": "RUNNING"},
                                                         {"id": "run-b", "status": "FINISHED"}]})
        self.write(self.source / "runs" / "run-c.json", {"run_id": "run-c", "status": "START_UNKNOWN"})
        result = migration.inventory(self.data, [self.source])
        summary = result["candidates"][0]
        self.assertTrue(summary["legacy_completed"])
        self.assertEqual(summary["run_status"], "LEGACY_RUNNING")
        self.assertEqual({row["id"] for row in summary["running"]}, {"run-a", "run-c"})
        state = self.migrate()
        self.assertEqual(state["status"], "REVALIDATION_REQUIRED")
        self.assertEqual(state["runs"], [])
        self.assertEqual(state["migration"]["summary"]["original_status"], "completed")
        self.assertEqual(state["migration"]["summary"]["run_status"], "LEGACY_RUNNING")

    def test_intent_only_requires_explicit_valid_intent(self):
        (self.source / "state.json").unlink()
        for intent in (None, {}, {"goals": []}):
            with self.subTest(intent=intent), self.assertRaises(V3Error):
                self.migrate(intent=intent)
        self.assertFalse(self.data.exists())
        state = self.migrate(source_path=self.source / "intent.json")
        self.assertEqual(state["intent"], self.intent)
        self.assertIsNone(state["migration"]["summary"]["schema_version"])

    def test_idempotent_replay_retains_progress_and_no_new_files(self):
        first = self.migrate()
        before = self.tree(self.data)
        self.assertEqual(self.migrate(), first)
        self.assertEqual(self.tree(self.data), before)
        # New valid work after migration must survive a replay.
        revised = self.service.revise("task-new", {"decisions": {"a": 2}}, expected_revision=1)
        replay = self.migrate()
        self.assertEqual(replay, revised)
        self.assertEqual(replay["decisions"], {"a": 2})

    def test_existing_task_never_overwritten_even_same_intent(self):
        existing = self.service.create("task-new", self.intent, request_id="request-a")
        before = self.tree(self.data)
        with self.assertRaises(V3Error):
            self.migrate()
        self.assertEqual(self.tree(self.data), before)
        self.assertEqual(self.service.store.load("task-new"), existing)

    def test_existing_legacy_destination_and_source_overlap_refused(self):
        target = self.data / "memory" / "tasks" / "task-new"
        self.write(target / "state.json", {"schema_version": 1})
        before = self.tree(self.data)
        with self.assertRaises(V3Error):
            self.migrate()
        self.assertEqual(self.tree(self.data), before)
        nested_service = TaskService(self.source)
        with self.assertRaises(V3Error):
            self.migrate(service=nested_service)

    def test_changed_source_request_or_intent_conflicts(self):
        self.migrate()
        before = self.tree(self.data)
        for updates in ({"request_id": "request-b"}, {"intent": dict(self.intent, action="consult")}):
            with self.subTest(updates=updates), self.assertRaises(V3Error):
                self.migrate(**updates)
            self.assertEqual(self.tree(self.data), before)
        (self.source / "binary.dat").write_bytes(b"changed")
        with self.assertRaises(V3Error):
            self.migrate()
        self.assertEqual(self.tree(self.data), before)

    def test_recovery_after_real_create_before_metadata_commit(self):
        class InterruptedService(TaskService):
            interrupted = False

            def create(inner, *args, **kwargs):
                state = super().create(*args, **kwargs)
                if not inner.interrupted:
                    inner.interrupted = True
                    raise OSError("interrupted-a")
                return state

        service = InterruptedService(self.data)
        with self.assertRaises(OSError):
            self.migrate(service=service)
        pending = service.store.load("task-new")
        self.assertEqual(pending["state_seq"], 1)
        self.assertIsNone(pending["plan"])
        self.assertEqual(pending["permits"], [])
        result = self.migrate(service=service)
        self.assertEqual(result["status"], "REVALIDATION_REQUIRED")
        self.assertEqual(len(list((service.store.task_dir("task-new") / "migration" / "backups").iterdir())), 1)

    def test_recovery_after_metadata_commit_failure_keeps_backup(self):
        class InterruptedStore(TaskStore):
            interrupted = False

            def commit(inner, task_id, state):
                if state.get("migration") and not inner.interrupted:
                    inner.interrupted = True
                    raise OSError("interrupted-a")
                return super().commit(task_id, state)

        self.service.store = InterruptedStore(self.data)
        with self.assertRaises(OSError):
            self.migrate()
        self.assertIsNone(self.service.store.load("task-new").get("migration"))
        state = self.migrate()
        self.assertEqual(state["migration"]["status"], "COMPLETED")
        self.assertEqual(self.migrate(), state)

    def test_incomplete_backup_is_inert_retry_uses_fresh_exclusive_directory(self):
        original = migration._exclusive_bytes
        calls = []

        def interrupted(path, data):
            original(path, data)
            calls.append(path)
            raise OSError("interrupted-a")

        with patch.object(migration, "_exclusive_bytes", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertFalse((self.service.store.task_dir("task-new") / "state.json").exists())
        state = self.migrate()
        self.assertTrue(calls[0].exists())
        backup = self.service.store.task_dir("task-new") / state["migration"]["backup_directory"]
        self.assertFalse(calls[0].is_relative_to(backup))
        self.assertEqual(self.tree(backup), self.tree(self.source))

    def test_corrupted_backup_never_repaired_by_overwriting(self):
        state = self.migrate()
        backup = self.service.store.task_dir("task-new") / state["migration"]["backup_directory"] / "binary.dat"
        backup.write_bytes(b"changed")
        with self.assertRaises(V3Error):
            self.migrate()
        self.assertEqual(backup.read_bytes(), b"changed")

    def test_exclusive_backup_creation_refuses_overwrite(self):
        path = self.root / "a.dat"
        migration._exclusive_bytes(path, b"a")
        with self.assertRaises(FileExistsError):
            migration._exclusive_bytes(path, b"b")
        self.assertEqual(path.read_bytes(), b"a")

    def test_parallel_identical_calls_create_one_task_and_one_backup(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.migrate(), range(2)))
        self.assertEqual(results[0], results[1])
        directory = self.service.store.task_dir("task-new") / "migration" / "backups"
        self.assertEqual(len(list(directory.iterdir())), 1)

    def test_source_changes_during_create_are_not_committed_as_history(self):
        source = self.source

        class ChangingService(TaskService):
            def create(inner, *args, **kwargs):
                state = super().create(*args, **kwargs)
                (source / "binary.dat").write_bytes(b"changed")
                return state

        with self.assertRaises(V3Error):
            self.migrate(service=ChangingService(self.data))
        state = self.service.store.load("task-new")
        self.assertNotIn("migration", state)
        self.assertEqual(state["permits"], [])

    def test_symlink_source_fails_closed_when_supported(self):
        link = self.root / "link-a"
        try:
            link.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(V3Error):
            self.migrate(source_path=link)
        result = migration.inventory(self.data, [link])
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
