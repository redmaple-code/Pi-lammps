"""Publication through the registered application channel and common locks."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evolution"), str(ROOT / "mcp")]
import apply_proposal
from evidence_v3 import knowledge, policy, retrieval, store
from evidence_v3.contracts import V3Error, read_json, atomic_json
from evidence_v3.service import TaskService


def approve(proposal):
    digest = policy.promotion_content_hash(proposal)
    proposal["review"] = {"actor_kind": "human", "actor_id": "reviewer1", "event_id": "approval1",
                          "status": "approved", "revoked": False, "reason": "Reviewed original source and scope",
                          "content_hash": digest}
    if proposal["kind"] == "expert_policy":
        proposal["confirmations"] = [{"id": "confirmation" + str(n), "actor_kind": "human", "actor_id": "reviewer1",
                                       "event_id": "event" + str(n), "content_hash": digest,
                                       "decision": "confirm", "revoked": False} for n in range(2)]
    return proposal


def candidate(identity="check1", kind="check_knowledge"):
    return approve({"class": knowledge.CLASS_NAME, "tier": "human", "op": "publish", "id": identity,
        "kind": kind, "content": {"title": "Anonymous check", "question_or_claim": "Does the evidence support the goal?"},
        "conditions": {}, "source_refs": ["s1"], "sources": [
            {"id": "s1", "source_kind": "human", "source_locator": "original:1", "provenance_refs": []}],
        "owner_rule": "E3", "verification_method": {"kind": "semantic"}, "status": "canary"})


def transition(original, op, version):
    value = copy.deepcopy(original)
    value.update(op=op, expected_version=version)
    value.pop("review", None)
    value.pop("confirmations", None)
    return approve(value)


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def test_cold_start_single_channel_roundtrip(self):
        proposal = candidate()
        result = knowledge.publish(proposal, "reviewer1", self.root)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["generation"], 1)
        self.assertEqual(result["entry"]["status"], "canary")
        self.assertEqual(result["entry"]["independent_sources"], ["original:1"])
        self.assertIn("not factual verification", " ".join(result["entry"]["limitations"]))
        loaded = retrieval.load_knowledge([str(Path(self.root) / "memory" / "review_experience")])
        self.assertEqual(len(loaded["entries"]), 1, loaded)
        self.assertEqual(loaded["entries"][0]["lifecycle"], "canary")
        with patch.object(apply_proposal, "apply_knowledge_proposal", return_value={"sentinel": True}) as channel:
            self.assertEqual(knowledge.publish(proposal, "reviewer1", self.root), {"sentinel": True})
            channel.assert_called_once()

    def test_active_revoke_and_backup_generation(self):
        original = candidate()
        knowledge.publish(original, "reviewer1", self.root)
        promoted = knowledge.publish(transition(original, "promote", 1), "reviewer1", self.root)
        self.assertEqual(promoted["entry"]["status"], "active")
        revoked = knowledge.publish(transition(original, "revoke", 2), "reviewer1", self.root)
        self.assertEqual(revoked["entry"]["status"], "revoked")
        self.assertEqual(revoked["generation"], 3)
        loaded = retrieval.load_knowledge([str(Path(self.root) / "memory" / "review_experience")])
        self.assertEqual(loaded["entries"], [])
        self.assertEqual(len(list((Path(self.root) / "memory" / "backups" / "evidence_v3").glob("*.json"))), 2)
        self.assertEqual(len(revoked["entry"]["history"]), 2)
        with self.assertRaises(V3Error):
            knowledge.publish(original, "reviewer1", self.root)
        with self.assertRaises(V3Error):
            knowledge.publish(transition(original, "promote", 3), "reviewer1", self.root)

    def test_approval_status_source_version_and_duplicate_fail_closed(self):
        mutations = [lambda x: x.update(tier="auto"), lambda x: x.update(status="missing"),
                     lambda x: x["review"].update(actor_kind="AI"), lambda x: x["review"].update(revoked=True),
                     lambda x: x["sources"][0].pop("source_locator"),
                     lambda x: x["sources"][0].update(provenance_refs=["s1"]),
                     lambda x: x["verification_method"].update(command=["arbitrary"])]
        for mutation in mutations:
            proposal = candidate()
            mutation(proposal)
            with self.assertRaises(V3Error):
                knowledge.publish(proposal, "reviewer1", self.root)
        self.assertEqual(store.read_generation(self.root), 0)
        proposal = candidate()
        with self.assertRaises(V3Error):
            knowledge.publish(proposal, "someone_else", self.root)
        knowledge.publish(proposal, "reviewer1", self.root)
        bad = transition(proposal, "revoke", 999)
        with self.assertRaises(V3Error):
            knowledge.publish(bad, "reviewer1", self.root)
        directory = Path(self.root) / "memory" / "review_experience"
        atomic_json(directory / "duplicate.json", {"pack": "other", "entries": [{"id": proposal["id"]}]})
        with self.assertRaises(V3Error):
            knowledge.publish(candidate("another"), "reviewer1", self.root)

    def test_two_distinct_confirmation_events_not_repeated_submissions(self):
        proposal = candidate(kind="expert_policy")
        broken = copy.deepcopy(proposal)
        broken["confirmations"][1]["event_id"] = broken["confirmations"][0]["event_id"]
        with self.assertRaises(V3Error):
            knowledge.publish(broken, "reviewer1", self.root)
        result = knowledge.publish(proposal, "reviewer1", self.root)
        self.assertEqual(result["entry"]["kind"], "expert_policy")
        self.assertEqual(result["entry"]["independent_sources"], ["original:1"])

    def test_source_copies_do_not_add_independent_support(self):
        proposal = candidate()
        proposal.pop("review")
        proposal["sources"] += [{"id": "copy", "source_kind": "experience", "source_locator": "projection:1", "provenance_refs": ["s1"]}]
        proposal["source_refs"].append("copy")
        approve(proposal)
        result = knowledge.publish(proposal, "reviewer1", self.root)
        self.assertEqual(result["entry"]["independent_sources"], ["original:1"])

    def test_corrupt_package_is_not_empty(self):
        directory = Path(self.root) / "memory" / "review_experience"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("broken", encoding="utf-8")
        with self.assertRaises(V3Error):
            knowledge.publish(candidate(), "reviewer1", self.root)
        self.assertEqual(store.read_generation(self.root), 0)

    def test_failed_generation_commit_leaves_pending_and_no_retry(self):
        with patch.object(store, "bump_generation", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                knowledge.publish(candidate(), "reviewer1", self.root)
        state = store.TaskStore(self.root).generation()
        self.assertEqual(state["status"], "pending")
        with self.assertRaises(V3Error) as error:
            knowledge.publish(candidate("next"), "reviewer1", self.root)
        self.assertEqual(error.exception.code, "GENERATION_PENDING")
        service = TaskService(self.root)
        with self.assertRaises(V3Error):
            service.create("t1", {"action": "design", "goals": [{"id": "g1", "text": "Anonymous goal"}],
                                  "conditions": {}, "unknowns": [], "source_refs": ["request:1"]})

    def test_publication_invalidates_prepared_probe_before_launcher(self):
        import hashlib
        import evidence_review
        from evidence_v3.execution import normalize_spec
        work = Path(self.root) / "work"
        work.mkdir()
        content = "anonymous input"
        (work / "object.txt").write_text(content, encoding="utf-8")
        inputs = [{"path": "object.txt", "sha256": hashlib.sha256(content.encode()).hexdigest()}]
        def action_adapter(spec, context):
            return {"kind": spec["kind"], "supported": True, "dependencies_complete": True,
                    "constraints_enforced": True, "registration_hash": policy.canonical_hash({
                        "adapter": "publication-probe", "kind": spec["kind"],
                        "command": spec["command"], "limits": spec["limits"]})}
        def human_verifier(task_id, request, context):
            actor_id = "synthetic-reviewer"
            request_hash = policy.canonical_hash(request)
            source_id = "synthetic-event:" + request_hash
            return {"actor_id": actor_id, "source_refs": [source_id],
                    "authorization_source": {"kind": "trusted_human_event", "source_id": source_id,
                        "actor_id": actor_id, "request_hash": request_hash, "verified": True}}
        service = TaskService(self.root, action_adapter=action_adapter, human_verifier=human_verifier)
        task = service.create("t1", {"action": "design", "goals": [{"id": "g1", "text": "Anonymous goal"}],
                                     "conditions": {}, "unknowns": [], "source_refs": ["request:1"]})
        task = service.revise("t1", {"dependencies": {"cwd": str(work), "inputs": inputs}}, task["revision"])
        spec = normalize_spec({"kind": "validation", "command": ["registered-action", "object.txt"],
            "cwd": str(work), "inputs": inputs, "limits": {"seconds": 2}, "question_id": "q1", "review_id": "probe-review"})
        task = service.record_authorization("t1", {"id": "auth1", "scope": "validation", "decision": "allow",
            "spec_hash": policy.canonical_hash(spec), "subject_hash": task["subject_hash"], "actor_kind": "human",
            "actor_id": "reviewer1", "source_refs": ["user:event1"], "reason": "Bounded probe", "revoked": False}, task["revision"])
        task = service.read("t1")
        task["validation_spec"] = spec
        def transport(request):
            citation = {"path": "/validation_spec/question_id", "quote": "q1"}
            return {"context_hash": request["context_hash"], "subject_hash": request["subject_hash"], "spec_hash": request["spec_hash"],
                "question_review": {"rationale": "Probe scope", "citations": [citation]},
                "resource_review": {"rationale": "Bounded limits", "citations": [citation]},
                "conclusion": {"verdict": "PASS", "reason": "Probe supported", "citations": [citation]},
                "reviewed_inputs": ["object.txt"], "input_reviews": [{"path": "object.txt", "rationale": "Full input inspected",
                    "citations": [{"path": "/inputs/0/content", "quote": content}]}], "findings": [], "limitations": []}
        artifact = evidence_review.run_review(task, transport, "validation")
        self.assertEqual(artifact.get("status"), "success", artifact)
        task = service.record_review("t1", artifact, task["revision"])
        permit = service.authorize("t1", spec, task["revision"])
        knowledge.publish(candidate(), "reviewer1", self.root)
        calls = []
        with self.assertRaises(V3Error) as error:
            service.start("t1", permit["id"], spec, "start1", lambda frozen: calls.append(frozen))
        self.assertEqual(error.exception.code, "REVALIDATION_REQUIRED")
        self.assertEqual(calls, [])

    def test_generation_lock_serializes_task_transaction(self):
        acquired = threading.Event()
        finished = threading.Event()
        def worker():
            with store.TaskStore(self.root).transaction("t1"):
                acquired.set()
            finished.set()
        with store.generation_lock(self.root):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertFalse(acquired.wait(0.05))
        thread.join(3)
        self.assertTrue(finished.is_set())

    def test_registered_class_keeps_existing_policy_classes(self):
        self.assertIn(knowledge.CLASS_NAME, apply_proposal.POLICY["human_review_classes"])
        self.assertIsNone(apply_proposal.check_policy({"class": "persona_wording", "tier": "human"}, ["persona.txt"], 1))
        self.assertIsNotNone(apply_proposal.check_policy({"class": "persona_wording", "tier": "auto"}, ["persona.txt"], 1))

    def test_diff_classes_cannot_bypass_knowledge_generation(self):
        self.assertIsNotNone(apply_proposal.check_policy(
            {"class": knowledge.CLASS_NAME, "tier": "human"}, ["arbitrary.json"], 1))
        self.assertIsNotNone(apply_proposal.check_policy(
            {"class": "persona_wording", "tier": "human"}, ["memory/review_experience/package.json"], 1))

    def test_legacy_rollback_cannot_bypass_generation(self):
        directory = Path(self.root) / "memory"
        directory.mkdir()
        ledger = directory / "LEDGER.jsonl"
        ledger.write_text(json.dumps({"id": "legacy1", "result": "APPLIED", "root": "dsh_agent",
            "files": ["memory/review_experience/pack.json"], "backup": "backups/legacy"}) + "\n", encoding="utf-8")
        with patch.object(apply_proposal, "LEDGER", str(ledger)), patch.object(apply_proposal, "_restore") as restore:
            self.assertEqual(apply_proposal.rollback("legacy1"), 2)
        restore.assert_not_called()
        self.assertEqual(store.read_generation(self.root), 0)

    def test_cli_registered_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for name in apply_proposal.REQUIRED_FILES:
                (path / name).write_text("" if name != "meta.json" else json.dumps(candidate()), encoding="utf-8")
            with patch.object(sys, "argv", ["apply_proposal.py", folder, "--approver", "reviewer1"]), \
                 patch.object(apply_proposal.paths, "data_root", return_value=self.root):
                self.assertEqual(apply_proposal.main(), 0)
        self.assertEqual(store.read_generation(self.root), 1)


if __name__ == "__main__":
    unittest.main()
