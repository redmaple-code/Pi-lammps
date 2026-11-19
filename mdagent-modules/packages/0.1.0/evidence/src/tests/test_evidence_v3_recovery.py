"""Recovery and refresh tests use isolated data roots and anonymous knowledge."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evolution"), str(ROOT / "mcp")]
import apply_proposal
from evidence_v3 import knowledge, policy, store
from evidence_v3.contracts import V3Error, canonical_hash, read_json
from evidence_v3.execution import file_hash
from evidence_v3.service import TaskService


def proposal(identity, status="active", conditions=None):
    value = {"class": knowledge.CLASS_NAME, "tier": "human", "op": "publish", "id": identity,
             "kind": "check_knowledge", "content": {"title": "Anonymous obligation",
             "question_or_claim": "Does the requested object satisfy the declared condition?"},
             "conditions": conditions or {}, "source_refs": ["source-" + identity],
             "sources": [{"id": "source-" + identity, "source_kind": "human",
                          "source_locator": "original:" + identity, "provenance_refs": []}],
             "owner_rule": "E3", "verification_method": {"kind": "semantic"}, "status": status}
    value["review"] = {"actor_kind": "human", "actor_id": "reviewer", "event_id": "approval-" + identity,
                       "status": "approved", "revoked": False, "reason": "Reviewed scope and source",
                       "content_hash": policy.promotion_content_hash(value)}
    return value


def intent():
    return {"action": "design", "goals": [{"id": "goal-a", "text": "Inspect the requested object"}],
            "conditions": {}, "unknowns": [], "source_refs": ["request:a"]}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def publish_interrupted(self, identity, *, fail_package=False):
        candidate = proposal(identity)
        if fail_package:
            with patch.object(knowledge, "_atomic_bytes", side_effect=OSError("interrupted package replacement")):
                with self.assertRaises(OSError):
                    knowledge.publish(candidate, "reviewer", str(self.root))
        else:
            with patch.object(store, "bump_generation", side_effect=OSError("interrupted generation commit")):
                with self.assertRaises(OSError):
                    knowledge.publish(candidate, "reviewer", str(self.root))
        return candidate

    def test_inspect_and_recover_commit_is_idempotent(self):
        self.publish_interrupted("entry-a")
        state = store.TaskStore(self.root).generation()
        self.assertEqual(state["status"], "pending")
        event_id = state["pending"]["transaction_id"]
        inspected = knowledge.inspect_publication(str(self.root), event_id)
        row = inspected["transactions"][0]
        self.assertEqual(row["journal"]["event"]["changed_ids"], ["entry-a"])
        self.assertEqual(row["journal"]["event"]["changes"][0]["after"]["version"], 1)
        self.assertTrue(row["journal"]["before"]["exists"] is False)
        result = knowledge.recover_publication(str(self.root), event_id, "commit", approver="operator",
                                               reason="Inspect exact staged package and resume")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(store.read_generation(str(self.root)), 1)
        self.assertEqual(knowledge.recover_publication(str(self.root), event_id, "commit", approver="operator",
                                                       reason="Repeat same recovery"), result)
        self.assertEqual(knowledge.snapshot(str(self.root))["entries"]["entry-a"]["status"], "active")
        with self.assertRaises(V3Error):
            knowledge.recover_publication(str(self.root), event_id, "rollback", approver="operator",
                                          reason="Conflicting terminal decision")

    def test_interrupted_before_package_replace_can_commit_or_restore_backup(self):
        self.publish_interrupted("entry-commit", fail_package=True)
        pending = store.TaskStore(self.root).generation()
        self.assertEqual(pending["status"], "pending")
        event_id = pending["pending"]["transaction_id"]
        result = knowledge.recover_publication(str(self.root), event_id, "commit", approver="operator",
                                               reason="Complete the journaled transaction")
        self.assertEqual(result["status"], "applied")

        other = Path(self.temp.name) / "rollback-root"
        other.mkdir()
        knowledge.publish(proposal("baseline"), "reviewer", str(other))
        original_write = store.TaskStore.write_generation

        def interrupt_before_decision(task_store, value):
            pending_decision = value.get("pending", {}).get("decision")
            if pending_decision and pending_decision.get("decision") == "commit":
                raise OSError("interrupted before durable commit decision")
            return original_write(task_store, value)

        with patch.object(store.TaskStore, "write_generation", interrupt_before_decision):
            with self.assertRaises(OSError):
                knowledge.publish(proposal("next"), "reviewer", str(other))
        pending = store.TaskStore(other).generation()
        event_id = pending["pending"]["transaction_id"]
        info = knowledge.inspect_publication(str(other), event_id)["transactions"][0]
        before_path = other / info["journal"]["before"]["path"]
        self.assertEqual(file_hash(before_path), info["journal"]["before"]["sha256"])
        rolled = knowledge.recover_publication(str(other), event_id, "rollback", approver="operator",
                                               reason="Restore the full pre-publication package")
        self.assertEqual(rolled["status"], "rolled_back")
        entries = knowledge.snapshot(str(other))["entries"]
        self.assertIn("baseline", entries)
        self.assertNotIn("next", entries)
        terminal = store.TaskStore(other).generation()["events"][-1]
        self.assertEqual(terminal["outcome"], "rollback")
        self.assertEqual(terminal["effective_changed_ids"], [])
        self.assertEqual(terminal["changed_ids"], ["next"])

    def test_failure_after_generation_replace_inspects_as_committed(self):
        original = store.TaskStore.write_generation
        calls = []

        def replace_then_interrupt(task_store, value):
            original(task_store, value)
            calls.append(value["status"])
            if value["status"] == "ready":
                raise OSError("lost response after durable generation commit")

        with patch.object(store.TaskStore, "write_generation", replace_then_interrupt):
            with self.assertRaises(OSError):
                knowledge.publish(proposal("entry-after"), "reviewer", str(self.root))
        inspection = knowledge.inspect_publication(str(self.root))["transactions"][0]
        self.assertEqual(inspection["terminal"]["outcome"], "commit")
        recovered = knowledge.recover_publication(str(self.root), inspection["id"], "commit",
                                                  approver="operator", reason="Confirm durable terminal event")
        self.assertEqual(recovered["status"], "applied")
        self.assertEqual(store.read_generation(str(self.root)), 1)

    def test_refresh_reuses_only_with_backend_applicability_review(self):
        knowledge.publish(proposal("bound"), "reviewer", str(self.root))
        reviews = []

        def reviewer(context):
            reviews.append(context)
            return {"status": "success", "context_hash": context["context_hash"],
                    "source_refs": ["review:semantic"], "items": [
                        {"entry_id": row["entry_id"], "change_hash": canonical_hash(row),
                         "status": "inapplicable", "reason": "Not applicable to this task",
                         "source_refs": ["review:item"]} for row in context["changes"]]}

        service = TaskService(self.root, knowledge_reviewer=reviewer)
        state = service.create("task-a", intent())
        state = service.revise("task-a", {"dependencies": {"knowledge": ["bound"]}}, state["revision"])
        # An existing BLOCK receipt still binds a real stage evaluation artifact.
        receipt = service.evaluate("task-a")
        knowledge.publish(proposal("unrelated"), "reviewer", str(self.root))
        current = service.read("task-a")
        state = service.refresh_knowledge("task-a", current["revision"], current["state_seq"])
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["generation"], 2)
        self.assertTrue(state["knowledge_refreshes"][-1]["reused"])
        self.assertEqual(len(reviews), 1)
        self.assertTrue(service._record_generation_valid(state, receipt))

    def test_related_revocation_new_obligation_and_opaque_event_force_revalidation(self):
        active = proposal("related")
        knowledge.publish(active, "reviewer", str(self.root))
        service = TaskService(self.root, knowledge_reviewer=lambda context: {
            "status": "success", "context_hash": context["context_hash"], "source_refs": ["review:semantic"],
            "items": [{"entry_id": row["entry_id"], "change_hash": canonical_hash(row),
                       "status": "applicable", "reason": "A new applicable obligation",
                       "source_refs": ["review:item"]} for row in context["changes"]]})
        state = service.create("task-a", intent())
        state = service.revise("task-a", {"dependencies": {"knowledge": ["related"]}}, state["revision"])
        receipt = service.evaluate("task-a")
        revoked = copy.deepcopy(active)
        revoked.update(op="revoke", expected_version=1)
        revoked["review"] = {"actor_kind": "human", "actor_id": "reviewer", "event_id": "revoke-related",
                             "status": "approved", "revoked": False, "reason": "Reviewed withdrawal",
                             "content_hash": policy.promotion_content_hash(revoked)}
        knowledge.publish(revoked, "reviewer", str(self.root))
        current = service.read("task-a")
        state = service.refresh_knowledge("task-a", current["revision"], current["state_seq"])
        self.assertEqual(state["revision"], 3)
        self.assertFalse(state["knowledge_refreshes"][-1]["reused"])
        self.assertFalse(service._record_generation_valid(state, receipt))

        second = Path(self.temp.name) / "second-root"
        second.mkdir()
        knowledge.publish(proposal("first"), "reviewer", str(second))
        other_service = TaskService(second)
        task = other_service.create("task-b", intent())
        task = other_service.revise("task-b", {"dependencies": {"knowledge": ["first"]}}, task["revision"])
        with store.generation_lock(str(second)):
            store.bump_generation(str(second))  # No signed knowledge event: conservative refresh.
        task = other_service.refresh_knowledge("task-b", task["revision"], task["state_seq"])
        self.assertEqual(task["revision"], 3)
        self.assertIn("authority-or-unknown-change", task["knowledge_refreshes"][-1]["reasons"])

    def test_client_generation_success_cannot_skip_revalidation(self):
        service = TaskService(self.root)
        state = service.create("task-a", intent())
        with store.generation_lock(str(self.root)):
            store.bump_generation(str(self.root))
        with self.assertRaises(V3Error) as error:
            service.revise("task-a", {"generation_review": {"generation": 1, "subject_hash": state["subject_hash"],
                "status": "success", "source_refs": ["caller:claim"]}}, state["revision"])
        self.assertEqual(error.exception.code, "GENERATION_REVIEW_REQUIRED")

    def test_old_review_result_is_archived_stale_without_changing_active_state(self):
        service = TaskService(self.root)
        initial = service.create("task-a", intent())
        current = service.revise("task-a", {"decisions": {"changed": True}}, initial["revision"])
        archived = service.record_review("task-a", {"id": "late-review", "task_id": "task-a",
            "revision": initial["revision"], "subject_hash": initial["subject_hash"], "status": "success",
            "source_refs": ["review:late"]}, initial["revision"])
        self.assertTrue(archived["reviews"][-1]["stale"])
        self.assertEqual(archived["revision"], current["revision"])
        self.assertEqual(archived["subject_hash"], current["subject_hash"])
        self.assertEqual(archived["active_receipts"], current["active_receipts"])

    def test_task_measurement_materializes_only_from_original_finished_output(self):
        service = TaskService(self.root)
        state = service.create("task-a", intent())
        task_dir = service.store.task_dir("task-a")
        frozen = task_dir / "frozen" / "permit-a"
        output = task_dir / "outputs" / "permit-a"
        frozen.mkdir(parents=True)
        output.mkdir(parents=True)
        input_file = frozen / "input.txt"
        input_file.write_text("frozen input", encoding="utf-8")
        input_manifest = [{"path": "input.txt", "sha256": file_hash(input_file)}]
        input_record = [{"path": str(input_file), "sha256": file_hash(input_file)}]
        raw_file = output / "raw.json"
        raw_file.write_text('{"values":[2,4,6]}\n', encoding="utf-8")
        raw_manifest = [{"path": str(raw_file), "sha256": file_hash(raw_file)}]
        recipe = {"source": raw_manifest[0], "format": "json", "pointer": "/values", "unit": "count",
                  "source_unit": "count", "aggregate": "mean", "window": {"start": 0, "stop": 3}}
        item = {"id": "measurement-item", "owner_rule": "E2", "due_stage": "ANALYZE",
                "claim": "claim-a", "claim_id": "claim-a", "run_id": "run-a",
                "measurement": {"claim_id": "claim-a", "metric": "value", "unit": "count",
                                 "window": {"start": 0, "stop": 3}, "tolerance": 0, "recipe": recipe}}
        state["plan"] = {"items": [item], "plan_hash": canonical_hash([item])}
        state["subject_hash"] = service._subject(state)
        permit = {"id": "permit-a", "task_id": "task-a", "revision": state["revision"],
                  "subject_hash": state["subject_hash"], "generation": state["generation"],
                  "frozen_spec": {"cwd": str(frozen), "output_dir": str(output),
                                  "inputs": input_manifest, "kind": "validation"}}
        run = {"id": "run-a", "task_id": "task-a", "permit_id": "permit-a", "kind": "validation",
               "revision": state["revision"], "subject_hash": state["subject_hash"], "status": "FINISHED",
               "inputs": input_record, "input_hash": canonical_hash(input_record), "outputs": raw_manifest,
               "output_hash": canonical_hash(raw_manifest), "receipt_id": "run-receipt-a",
               "knowledge_snapshot_hash": state["knowledge_snapshot"]["hash"],
               "result": {"outputs": raw_manifest}, "started_at": "synthetic"}
        receipt = {"id": "run-receipt-a", "kind": "RUN_RECEIPT", "task_id": "task-a", "run_id": "run-a",
                   "result": {"outputs": raw_manifest}}
        service.store.artifact("task-a", "permits", permit)
        service.store.artifact("task-a", "receipts", receipt)
        state["permits"].append(permit)
        state["receipts"].append(receipt)
        state["runs"].append(run)
        service.store.commit("task-a", state)

        recorded = service.record_measurement("task-a", "run-a", "measurement-item", recipe,
                                              "measurement-a", state["revision"])
        evidence = next(row for row in recorded["evidence"] if row["id"] == "measurement-a")
        self.assertEqual(evidence["value"], 4.0)
        self.assertEqual(evidence["source_kind"], "measurement")
        updated_run = recorded["runs"][0]
        self.assertEqual(updated_run["output_hash"], canonical_hash(updated_run["outputs"]))
        self.assertEqual(len(updated_run["outputs"]), 2)
        self.assertEqual(read_json(recorded["receipts"][0]["result"]["outputs"][0]["path"]), {"values": [2, 4, 6]})
        derived = updated_run["derived_outputs"][0]
        self.assertEqual(derived["run_receipt_id"], "run-receipt-a")
        self.assertEqual(recorded["revision"], state["revision"] + 1)
        with self.assertRaises(V3Error):
            service.record_measurement("task-a", "run-a", "measurement-item", recipe,
                                       "measurement-b", recorded["revision"])


if __name__ == "__main__":
    unittest.main()
