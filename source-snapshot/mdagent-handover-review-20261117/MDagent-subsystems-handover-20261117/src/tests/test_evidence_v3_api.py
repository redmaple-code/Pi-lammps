"""End-to-end MCP contracts with external model and process boundaries injected."""
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "evolution"), str(ROOT / "tests")]
import evidence_api
import evidence_review
from evidence_v3.contracts import canonical_hash
from test_evidence_v3_review import intent, bundle, proposal, good_response


class EvidenceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.value = intent()

    def human_verifier(self, task_id, request, context):
        actor_id = "test-owner"
        return {"actor_id": actor_id, "source_refs": ["test-event:" + request["id"]],
                "authorization_source": {"kind": "trusted_human_event", "source_id": "event:" + request["id"],
                    "actor_id": actor_id, "request_hash": canonical_hash(request), "verified": True}}

    # The lifecycle cases below assert the unbatched transport contract. The
    # default byte budget is close to the size of the plan-review request, and
    # that request embeds absolute package paths from the knowledge snapshot, so
    # a deeper install directory can tip it into batching mid-test. Batching has
    # its own coverage in test_evidence_v3_review; keep this suite path-length
    # independent with an explicit generous budget.
    REVIEW_BUDGET = {"max_bytes": 200000, "max_tokens": 200000, "max_attempts": 2,
                     "max_batches": 64, "max_calls": 96, "deadline_seconds": 180}

    def call(self, action, **arguments):
        return evidence_api.dispatch(dict(action=action, task_id="task-api", **arguments),
                                     self.root, transport=good_response,
                                     human_verifier=self.human_verifier,
                                     review_budget=self.REVIEW_BUDGET)

    def planned(self):
        state = self.call("init", intent=self.value, request_id="create-api")
        self.assertNotIn("error", state, state)
        source_bundle = bundle(self.value)
        draft = proposal(self.value, source_bundle)
        design = copy.deepcopy(draft["items"][0])
        design.update(id="design-a", owner_rule="E1", claim_kind="design", allow_assumption=True)
        build = copy.deepcopy(draft["items"][0])
        build.update(id="build-a", owner_rule="E4", due_stage="BUILD")
        draft["items"].extend([design, build])
        review = draft["discovery_review"]
        review["items_hash"] = canonical_hash(draft["items"])
        review["goal_coverage"] = {"g1": [item["id"] for item in draft["items"]]}
        review["counterexample_review"]["reviewed_item_ids"] = [item["id"] for item in draft["items"]]
        state = self.call("plan", expected_revision=state["revision"], bundle=source_bundle,
                          proposal=draft)
        self.assertNotIn("error", state, state)
        return state

    def test_plan_review_evaluate_advance_and_revision(self):
        state = self.planned()
        authorization = {"id": "human-plan", "scope": "plan", "decision": "confirm",
                         "subject_hash": state["subject_hash"], "actor_kind": "human",
                         "actor_id": "owner", "reason": "Approve the exact stated goal and plan",
                         "source_refs": ["user:confirm-event"], "revoked": False}
        state = self.call("authorization", expected_revision=state["revision"],
                          authorization=authorization)
        self.assertNotIn("error", state, state)
        state = self.call("authorization", expected_revision=state["revision"], authorization={
            "id": "design-choice", "scope": "design-a", "decision": "accept_assumption",
            "subject_hash": state["subject_hash"], "actor_kind": "human", "actor_id": "owner",
            "reason": "Proceed with the explicitly stated exploratory choice",
            "source_refs": ["user:choice-event"], "revoked": False})
        self.assertNotIn("error", state, state)
        state = self.call("review", expected_revision=state["revision"])
        self.assertNotIn("error", state, state)
        receipt = self.call("evaluate")
        self.assertEqual(receipt.get("verdict"), "PASS", receipt)
        state = self.call("advance", expected_revision=state["revision"],
                          to_stage="BUILD", receipt_id=receipt["id"])
        self.assertEqual(state.get("current_stage"), "BUILD", state)
        changed = self.call("revise", expected_revision=state["revision"],
                            patch={"decisions": {"selection": "changed"}})
        self.assertEqual(changed["active_receipts"], {})
        receipt = self.call("evaluate")
        self.assertEqual(receipt["verdict"], "BLOCK")

    def test_client_pass_and_fixed_surface_cannot_authorize(self):
        state = self.planned()
        self.assertEqual(self.call("evaluate").get("verdict"), "BLOCK")
        self.assertNotIn("PASS", str(self.call("unknown", record={"decision": "PASS"})))
        with patch.dict(os.environ, {"MDSIM_DATA_ROOT": self.root}):
            import md_mcp
            self.assertIn("sim_evidence", md_mcp.HANDLERS)
            self.assertTrue(md_mcp.sim_surface({"case_id": "task-api", "action": "record",
                                               "status": "evidenced"})["fail_closed"])
            with patch.object(md_mcp, "_bridge_exec") as bridge:
                result = md_mcp.sim_exec({"cmd": "unbound-action"})
            self.assertTrue(result["fail_closed"])
            bridge.assert_not_called()

    def test_strict_contract_and_input_material(self):
        from evidence_v3.contracts import V3Error, read_json
        malformed = Path(self.root) / "bad.json"
        for content in ('{"x":1,"x":2}', '{"x":NaN}'):
            malformed.write_text(content, encoding="utf-8")
            with self.assertRaises(V3Error):
                read_json(malformed)
        source = Path(self.root) / "object.txt"
        source.write_text("full anonymous input", encoding="utf-8")
        dependencies = {"cwd": self.root, "inputs": [
            {"path": "object.txt", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
            "dependencies_complete": True}
        full = evidence_api.attach_inputs({"dependencies": dependencies, "input_dependencies_complete": True})
        self.assertEqual(full["inputs"][0]["content"], "full anonymous input")
        source.write_text("changed", encoding="utf-8")
        with self.assertRaises(V3Error):
            evidence_api.attach_inputs({"dependencies": dependencies})

    def test_public_recognize_string_schema_reaches_transport(self):
        calls = []
        def transport(request):
            calls.append(request)
            value = intent()
            refs = [{"path": "/request", "quote": "Compare anonymous outcomes"}]
            value["source_refs"] = refs
            value["goals"][0]["source_refs"] = refs
            return {"context_hash": request["context_hash"], "intent": value}
        result = evidence_api.dispatch({"action": "recognize", "request": "Compare anonymous outcomes"},
                                       self.root, transport=transport)
        self.assertEqual(result.get("action"), "design", result)
        self.assertEqual(len(calls), 1)

    def test_unconfigured_action_is_explicitly_unsupported(self):
        from evidence_v3.contracts import V3Error
        source = Path(self.root) / "object.txt"
        source.write_text("anonymous input", encoding="utf-8")
        spec = {"kind": "validation", "command": ["registered-action"], "cwd": self.root,
                "inputs": [{"path": "object.txt", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
                "limits": {"timeout_seconds": 2}, "question_id": "probe-a"}
        with self.assertRaises(V3Error) as raised:
            evidence_api.configured_action_adapter({})(spec, {})
        self.assertIn("unsupported", raised.exception.code.lower())


if __name__ == "__main__":
    unittest.main()
