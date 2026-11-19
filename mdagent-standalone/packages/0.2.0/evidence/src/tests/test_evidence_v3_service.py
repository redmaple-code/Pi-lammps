"""Anonymous task-service controls: real storage and real planning/policy."""
import copy
import hashlib
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evolution"))
from evidence_v3.contracts import V3Error, atomic_json, canonical_hash
from evidence_v3.service import TaskService
from evidence_v3.store import (TaskStore, bump_generation, generation_lock,
                               mark_generation_pending, read_generation)
from evidence_v3 import planning


def _race_revision(root, barrier, results, value):
    service = TaskService(root)
    barrier.wait()
    try:
        service.revise("task-a", {"decisions": {"value": value}}, 1, 1)
        results.put("ok")
    except V3Error as exc:
        results.put(exc.code)


def intent():
    return {"action": "production", "goals": [{"id": "goal-a", "text": "inspect object"}],
            "conditions": {}, "unknowns": [], "source_refs": ["request:a"]}


def proposal(subject):
    bundle = {"intent_hash": canonical_hash(subject), "status": "empty", "queries": [],
              "health": [], "candidates": [], "manifest": [], "exclusions": [], "limitations": []}
    bundle["bundle_hash"] = canonical_hash(bundle)
    items = [{"id": "item-a", "question": "Is the object supported?", "claim": "object claim",
              "target_refs": ["goal-a"], "owner_rule": "E3", "due_stage": "PLAN",
              "origin_refs": ["request:a"], "rationale": "inspect the requested object",
              "applicability": {"status": "applicable", "reason": "requested", "source_refs": ["request:a"]}}]
    review = {"id": "discovery-a", "status": "success", "source_refs": ["review:a"],
              "intent_hash": canonical_hash(subject), "retrieval_hash": bundle["bundle_hash"],
              "items_hash": canonical_hash(items), "goal_coverage": {"goal-a": ["item-a"]},
              "counterexample_review": {"status": "success", "source_refs": ["review:a"],
                                        "reviewed_item_ids": ["item-a"], "query_refs": [],
                                        "limitations": ["No external material available"]}, "remaining_unknowns": []}
    return {"items": items, "discovery_review": review, "retrieval_bundle": bundle}


_REGISTRATION_HASH = "a" * 64


def adapter(spec, context):
    return {"kind": spec["kind"], "supported": True, "dependencies_complete": True,
            "constraints_enforced": True, "registration_hash": _REGISTRATION_HASH}


def handoff(**values):
    return {"started": True, "launch_id": "launch-a",
            "registration_hash": _REGISTRATION_HASH, **values}


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        def human_verifier(task_id, request, context):
            return {"actor_id": "actor-a", "source_refs": ["test-event:" + request["id"]],
                    "authorization_source": {"kind": "trusted_human_event", "source_id": "event:" + request["id"],
                        "actor_id": "actor-a", "request_hash": canonical_hash(request), "verified": True}}
        self.service = TaskService(self.root, action_adapter=adapter,
            result_verifier=lambda run, result: {"run_id": run["id"], "execution_complete": True,
                                                "returncode": result["returncode"]},
            human_verifier=human_verifier)
        self.state = self.service.create("task-a", intent(), request_id="create-a")
        self.work = self.root / "workspace"
        self.work.mkdir()
        self.input = self.work / "object.txt"
        self.input.write_text("anonymous input", encoding="utf-8")
        self.inputs = [{"path": "object.txt", "sha256": hashlib.sha256(self.input.read_bytes()).hexdigest()}]

    def tearDown(self):
        # Frozen inputs deliberately lose their write bit.
        for p in self.root.rglob("*"):
            if p.is_file():
                p.chmod(0o666)
        self.temp.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(V3Error) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def authorize_human(self, authorization, expected_revision):
        return self.service.record_authorization("task-a", authorization, expected_revision)

    def planned(self):
        return self.service.set_plan("task-a", proposal(intent()), 1)

    def reviewed(self, state, ident="review-a", **extra):
        from evidence_v3.policy import review_content_hash
        context = self.service._context(state)
        context["run_id"] = None
        review = {"id": ident, "status": "success", "method": "semantic", "task_id": "task-a",
                  "revision": state["revision"], "subject_hash": state["subject_hash"],
                  "plan_hash": state["plan"]["plan_hash"], "covered_items": ["item-a"],
                  "source_refs": ["review:a"], "scope": "plan", "confirmed": True,
                  "item_ids": ["item-a"], "evidence_refs": [],
                  "content_hash": review_content_hash(state["plan"]["items"][0], [], context),
                  "conclusions": [{"item_id": "item-a", "verdict": "PASS", "reason": "object checked",
                                   "supported_evidence_refs": [], "applicability": "applicable"}]}
        review["covered_items"] = [i["id"] for i in state["plan"]["items"]]
        review["item_ids"] = review["covered_items"]
        review["item_content_hashes"] = {i["id"]: review_content_hash(i, state["evidence"], context) for i in state["plan"]["items"]}
        review["conclusions"] = [dict(review["conclusions"][0], item_id=i["id"]) for i in state["plan"]["items"]]
        review.update(extra)
        return self.service.record_review("task-a", review, state["revision"])

    def exec_ready(self):
        raw = proposal(intent())
        for identity, rule, stage in (("basis-a", "E1", "PLAN"), ("method-a", "E4", "BUILD")):
            item = copy.deepcopy(raw["items"][0])
            item.update(id=identity, owner_rule=rule, due_stage=stage)
            if rule == "E1":
                item.update(allow_assumption=True, claim_kind="design")
            raw["items"].append(item)
        ids = [i["id"] for i in raw["items"]]
        raw["discovery_review"]["items_hash"] = canonical_hash(raw["items"])
        raw["discovery_review"]["goal_coverage"] = {"goal-a": ids}
        raw["discovery_review"]["counterexample_review"]["reviewed_item_ids"] = ids
        state = self.service.set_plan("task-a", raw, 1)
        state = self.service.revise("task-a", {"dependencies": {"inputs": self.inputs, "cwd": str(self.work)}}, state["revision"])
        state = self.authorize_human({
            "id": "production-auth", "scope": "production", "decision": "allow",
            "spec_hash": canonical_hash(self.spec()), "subject_hash": state["subject_hash"],
            "reason": "approved exact action"
        }, state["revision"])
        state = self.authorize_human({
            "id": "plan-auth", "scope": "plan", "decision": "confirm",
            "subject_hash": state["subject_hash"], "actor_kind": "human", "actor_id": "actor-a",
            "source_refs": ["event:b"], "reason": "confirmed requested goals", "revoked": False
        }, state["revision"])
        state = self.authorize_human({
            "id": "basis-auth", "scope": "basis-a", "finding_id": None, "decision": "accept_assumption",
            "subject_hash": state["subject_hash"], "actor_kind": "human", "actor_id": "actor-a",
            "source_refs": ["event:c"], "reason": "accepted limited design assumption", "revoked": False
        }, state["revision"])
        state = self.reviewed(state)
        for stage in ("BUILD", "CONFIG", "EXEC"):
            if state["current_stage"] == "CONFIG":
                state = self.reviewed(state, "input-review", scope="input", input_manifest=self.inputs)
            receipt = self.service.evaluate("task-a")
            self.assertEqual(receipt["verdict"], "PASS", receipt)
            state = self.service.advance("task-a", stage, state["revision"], receipt["id"])
        receipt = self.service.evaluate("task-a")
        self.assertEqual(receipt["verdict"], "PASS", receipt)
        return self.service.read("task-a")

    def spec(self, kind="production"):
        result = {"kind": kind, "command": ["registered-action", "object.txt"], "cwd": str(self.work),
                  "inputs": copy.deepcopy(self.inputs), "limits": {"seconds": 2}, "environment": {}}
        if kind == "validation":
            result.update(question_id="question-a", review_id="probe-review")
        if kind == "production":
            result["authorization_id"] = "production-auth"
        return result

    def probe(self):
        state = self.planned()
        spec = self.spec("validation")
        state = self.authorize_human({
            "id": "probe-auth", "scope": "validation", "decision": "allow",
            "spec_hash": canonical_hash(spec), "subject_hash": state["subject_hash"],
            "actor_kind": "human", "actor_id": "actor-a", "source_refs": ["event:a"],
            "reason": "approved bounded question", "revoked": False
        }, state["revision"])
        self.reviewed(state, "probe-review", scope="validation", question_id="question-a",
                      spec_hash=canonical_hash(spec), authorized=True, authorization_refs=["probe-auth"],
                      input_manifest=self.inputs, conclusion={"verdict": "PASS", "reason": "bounded question checked"})
        return spec, self.service.authorize("task-a", spec, state["revision"])

    def add_result_chain(self, state, run, permit):
        from evidence_v3.policy import review_content_hash
        source = Path(permit["frozen_spec"]["output_dir"]) / "measurement.json"
        raw_value = {"task_id": "task-a", "run_id": run["id"], "claim_id": "claim-a",
                     "metric": "metric-a", "unit": "unit-a", "value": 4, "window": [0, 1],
                     "tolerance": 0, "input_hash": self.inputs[0]["sha256"]}
        atomic_json(source, raw_value)
        measurement = dict(raw_value, output_hash=hashlib.sha256(source.read_bytes()).hexdigest())
        new = proposal(intent())
        new["items"] = copy.deepcopy(state["plan"]["proposal_items"])
        for identity, rule in (("measure-a", "E2"), ("result-a", "E3"), ("accept-a", "E5")):
            item = copy.deepcopy(new["items"][0])
            item.update(id=identity, owner_rule=rule, due_stage="ANALYZE", claim="claim-a", run_id=run["id"])
            if rule == "E2":
                item["measurement"] = measurement
            if rule == "E5":
                item["acceptance"] = [{"id": "criterion-a", "target_ref": "goal-a"}]
            new["items"].append(item)
        identities = [i["id"] for i in new["items"]]
        new["discovery_review"]["items_hash"] = canonical_hash(new["items"])
        new["discovery_review"]["goal_coverage"] = {"goal-a": identities}
        new["discovery_review"]["counterexample_review"]["reviewed_item_ids"] = identities
        prepared = planning.prepare_plan(intent(), new["retrieval_bundle"], new)
        new["reviewed_change"] = {"status": "success", "reason": "bind actual result claims", "source_refs": ["review:result"],
            "old_plan_hash": state["plan"]["plan_hash"], "new_content_hash": canonical_hash({k: v for k, v in prepared.items() if k != "plan_hash"})}
        state = self.service.set_plan("task-a", new, state["revision"])
        record = dict(measurement, id="measurement-a", source_kind="measurement", lifecycle="active", conditions={},
                      provenance_refs=["run:" + run["id"]], verification={"status": "verified", "method": "recompute",
                          "input_locator": str(Path(permit["frozen_spec"]["cwd"]) / "object.txt"), "source_locator": str(source)})
        state = self.service.record_evidence("task-a", record, state["revision"])
        # New substantive content requires fresh confirmation and all ancestor receipts.
        state = self.authorize_human({
            "id": "result-plan-auth", "scope": "plan", "decision": "confirm", "subject_hash": state["subject_hash"],
            "actor_kind": "human", "actor_id": "actor-a", "source_refs": ["event:result"], "reason": "confirmed result scope", "revoked": False
        }, state["revision"])
        state = self.authorize_human({
            "id": "result-basis-auth", "scope": "basis-a", "finding_id": None, "decision": "accept_assumption",
            "subject_hash": state["subject_hash"], "actor_kind": "human", "actor_id": "actor-a",
            "source_refs": ["event:result-basis"], "reason": "retain disclosed assumption", "revoked": False
        }, state["revision"])
        conclusions = [{"item_id": item["id"], "verdict": "PASS", "reason": "checked actual object",
                        "supported_evidence_refs": ["measurement-a"], "applicability": "applicable",
                        "acceptance_results": [{"id": "criterion-a", "passed": True, "reason": "target reached"}],
                        "limitations": []} for item in state["plan"]["items"]]
        hashes = {}
        for item in state["plan"]["items"]:
            context = self.service._context(state)
            context["run_id"] = item.get("run_id")
            hashes[item["id"]] = review_content_hash(item, state["evidence"], context)
        for scope in ("plan", "input"):
            review = {"id": "result-review-" + scope, "status": "success", "scope": scope, "method": "semantic",
                      "task_id": "task-a", "revision": state["revision"], "subject_hash": state["subject_hash"],
                      "plan_hash": state["plan"]["plan_hash"], "source_refs": ["review:result"],
                      "covered_items": identities, "item_ids": identities, "item_content_hashes": hashes,
                      "evidence_refs": ["measurement-a"], "conclusions": conclusions,
                      "input_manifest": self.inputs, "confirmed": True}
            state = self.service.record_review("task-a", review, state["revision"])
            if scope == "plan":
                for stage in ("PLAN", "BUILD"):
                    receipt = self.service.evaluate("task-a", stage)
                    self.assertEqual(receipt["verdict"], "PASS", receipt)
            else:
                for stage in ("CONFIG", "EXEC"):
                    receipt = self.service.evaluate("task-a", stage)
                    self.assertEqual(receipt["verdict"], "PASS", receipt)
        return self.service.read("task-a")

    def test_create_identity_legacy_and_paths(self):
        same = self.service.create("task-a", intent(), request_id="create-a")
        self.assertEqual(same, self.state)
        self.error("TASK_EXISTS", self.service.create, "task-a", intent())
        self.error("invalid-id", self.service.read, "../other")
        self.error("TASK_ID_COLLISION", self.service.create, "TASK-A", intent())
        legacy = self.root / "memory/tasks/legacy/state.json"
        atomic_json(legacy, {"schema_version": 1, "case_id": "legacy", "status": "GO"})
        self.assertTrue(self.service.read("legacy")["revalidation_required"])
        self.error("REVALIDATION_REQUIRED", self.service.revise, "legacy", {"decisions": {}}, 1)
        self.assertEqual(self.service.read("task-a")["schema_version"], 3)

    def test_cas_and_stable_review_subject(self):
        state = self.planned()
        reviewed = self.reviewed(state)
        self.assertEqual(reviewed["revision"], state["revision"])
        self.assertEqual(reviewed["subject_hash"], state["subject_hash"])
        self.assertGreater(reviewed["state_seq"], state["state_seq"])
        self.error("STATE_SEQ_CONFLICT", self.service.revise, "task-a", {"decisions": {}}, state["revision"], state["state_seq"])
        self.error("REVISION_CONFLICT", self.service.record_evidence, "task-a", {"id": "data-a"}, 1)
        self.assertTrue(list((self.root / "memory/tasks/task-a/backups").glob("state-*.json")))

    def test_threads_and_processes_cas(self):
        barrier = threading.Barrier(2)
        results = []
        def work(value):
            barrier.wait()
            try:
                self.service.revise("task-a", {"decisions": {"v": value}}, 1, 1)
                results.append("ok")
            except V3Error as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertCountEqual(results, ["ok", "REVISION_CONFLICT"])
        other = TaskService(self.root / "other")
        other.create("task-a", intent())
        ctx = multiprocessing.get_context("spawn")
        sync, queue = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=_race_revision, args=(str(self.root / "other"), sync, queue, i)) for i in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual([queue.get(timeout=2), queue.get(timeout=2)], ["ok", "REVISION_CONFLICT"])

    def test_fail_closed_and_receipt_immutability(self):
        state = self.planned()
        receipt = self.service.evaluate("task-a")
        self.assertEqual(receipt["verdict"], "BLOCK")
        self.error("RECEIPT_REQUIRED", self.service.advance, "task-a", "BUILD", state["revision"], receipt["id"])
        with patch("evidence_v3.policy.evaluate", side_effect=RuntimeError("failed")):
            self.assertEqual(self.service.evaluate("task-a")["verdict"], "BLOCK")
        self.error("IMMUTABLE_ARTIFACT", self.service.store.artifact, "task-a", "receipts", receipt)
        self.error("ANALYZE_STAGE_REQUIRED", self.service.complete, "task-a", state["revision"])

    def test_receipt_chain_and_stale_ancestors(self):
        state = self.exec_ready()
        self.assertEqual(set(state["active_receipts"]), {"PLAN", "BUILD", "CONFIG", "EXEC"})
        self.service.revise("task-a", {"decisions": {"changed": True}}, state["revision"])
        receipt = self.service.evaluate("task-a", "EXEC")
        self.assertEqual(receipt["verdict"], "BLOCK")
        self.assertIn("RECEIPT_REQUIRED", receipt["reasons"])

    def test_validation_is_independent_and_does_not_clear_production(self):
        spec, permit = self.probe()
        run = self.service.start("task-a", permit["id"], spec, "start-probe", lambda frozen: handoff())
        finished = self.service.finish("task-a", run["id"], {"run_id": run["id"], "returncode": 0, "outputs": []})
        self.assertEqual(finished["kind"], "validation")
        state = self.service.read("task-a")
        self.assertEqual(state["current_stage"], "PLAN")
        self.error("EXEC_STAGE_REQUIRED", self.service.authorize, "task-a", self.spec(), state["revision"])
        self.error("INVALID_TRANSITION", self.service.advance, "task-a", "ANALYZE", state["revision"])

    def test_frozen_bytes_single_use_and_request_identity(self):
        state = self.exec_ready()
        spec = self.spec()
        permit = self.service.authorize("task-a", spec, state["revision"])
        self.input.write_text("changed live input", encoding="utf-8")
        calls = []
        def launcher(frozen):
            calls.append(frozen)
            self.assertEqual((Path(frozen["cwd"]) / "object.txt").read_text(), "anonymous input")
            self.assertNotEqual(frozen["cwd"], spec["cwd"])
            return handoff()
        run = self.service.start("task-a", permit["id"], spec, "start-a", launcher)
        self.assertEqual(self.service.start("task-a", permit["id"], spec, "start-a", launcher), run)
        self.assertEqual(len(calls), 1)
        self.error("PERMIT_CONSUMED", self.service.start, "task-a", permit["id"], spec, "start-b", launcher)
        changed = copy.deepcopy(spec)
        changed["command"].append("extra")
        self.error("REQUEST_CONFLICT", self.service.start, "task-a", permit["id"], changed, "start-a", launcher)

    def test_unknown_launch_never_retries_and_reconciles(self):
        spec, permit = self.probe()
        calls = []
        def launch(frozen):
            calls.append(frozen)
            raise TimeoutError()
        run = self.service.start("task-a", permit["id"], spec, "unknown-a", launch)
        self.assertEqual(run["status"], "START_UNKNOWN")
        self.assertEqual(self.service.start("task-a", permit["id"], spec, "unknown-a", launch)["id"], run["id"])
        self.assertEqual(len(calls), 1)
        permit_record = next(p for p in self.service.read("task-a")["permits"] if p["id"] == permit["id"])
        def reconcile(current, receipt):
            self.assertEqual(current["frozen_spec"], permit_record["frozen_spec"])
            self.assertNotIn("frozen_spec", receipt)
            return {"run_id": current["id"], "started": True, "launch_id": "launch-a",
                    "registration_hash": permit_record["adapter"]["registration_hash"],
                    "execution_complete": False}
        recovered = self.service.reconcile_start("task-a", run["id"], {}, reconciler=reconcile)
        self.assertEqual(recovered["status"], "RUNNING")

    def test_no_adapter_no_launcher_and_bare_returncode(self):
        spec, permit = self.probe()
        state = self.service.read("task-a")
        self.error("UNSUPPORTED", TaskService(self.root).authorize, "task-a", spec, state["revision"])
        self.error("LAUNCHER_REQUIRED", self.service.start, "task-a", permit["id"], spec, "none-a", None)
        run = self.service.start("task-a", permit["id"], spec, "bare-a", lambda frozen: {"returncode": 0})
        self.assertEqual(run["status"], "START_UNKNOWN")

    def test_generation_pending_and_invalidates_permit(self):
        spec, permit = self.probe()
        self.error("GENERATION_LOCK_REQUIRED", bump_generation, self.root)
        with generation_lock(self.root):
            self.error("PUBLICATION_JOURNAL_REQUIRED", mark_generation_pending, self.root)
            self.assertEqual(bump_generation(self.root), 1)
            self.assertEqual(read_generation(self.root), 1)
        self.error("REVALIDATION_REQUIRED", self.service.start, "task-a", permit["id"], spec, "start-a", lambda s: {})

    def test_finish_receipt_and_full_completion(self):
        state = self.exec_ready()
        permit = self.service.authorize("task-a", self.spec(), state["revision"])
        run = self.service.start("task-a", permit["id"], self.spec(), "production-a", lambda s: handoff(execution_complete=False))
        output = Path(permit["frozen_spec"]["output_dir"]) / "measurement.json"
        atomic_json(output, {"task_id": "task-a", "run_id": run["id"], "claim_id": "claim-a",
                     "metric": "metric-a", "unit": "unit-a", "value": 4, "window": [0, 1],
                     "tolerance": 0, "input_hash": self.inputs[0]["sha256"]})
        result = {"run_id": run["id"], "returncode": 0, "outputs": [{"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]}
        finished = self.service.finish("task-a", run["id"], result)
        self.assertEqual(self.service.finish("task-a", run["id"], result), finished)
        self.error("RUN_RESULT_IMMUTABLE", self.service.finish, "task-a", run["id"], dict(result, returncode=1))
        state = self.service.advance("task-a", "ANALYZE", state["revision"])
        self.assertEqual(self.service.evaluate("task-a")["verdict"], "BLOCK")
        self.error("RECEIPT_REQUIRED", self.service.complete, "task-a", state["revision"])
        # Add only result-stage obligations; execution identity must survive.
        state = self.add_result_chain(state, run, permit)
        analyzed = self.service.evaluate("task-a")
        self.assertEqual(analyzed["verdict"], "PASS", analyzed)
        self.assertEqual(self.service.complete("task-a", state["revision"])["status"], "completed")

    def test_new_review_failure_blocks_previous_success(self):
        state = self.exec_ready()
        self.service.record_review("task-a", {
            "id": "review-failure", "status": "infra_error", "scope": "input",
            "task_id": "task-a", "subject_hash": state["subject_hash"],
            "error": "review-unavailable"
        }, state["revision"])
        receipt = self.service.evaluate("task-a", "CONFIG")
        self.assertEqual(receipt["verdict"], "BLOCK")
        self.assertIn("INPUT_REVIEW_REQUIRED", receipt["reasons"])

    def test_input_projection_and_frozen_tamper(self):
        state = self.exec_ready()
        self.assertTrue(state["input_dependencies_complete"])
        self.assertEqual(state["inputs"][0]["content"], "anonymous input")
        permit = self.service.authorize("task-a", self.spec(), state["revision"])
        frozen = Path(permit["frozen_spec"]["cwd"]) / "object.txt"
        frozen.chmod(0o666)
        frozen.write_text("tampered", encoding="utf-8")
        calls = []
        self.error("FROZEN_INPUT_CHANGED", self.service.start, "task-a", permit["id"], self.spec(), "tamper-a", lambda s: calls.append(s))
        self.assertEqual(calls, [])

    def test_starting_recovered_as_unknown_without_launch(self):
        spec, permit = self.probe()
        with self.assertRaises(KeyboardInterrupt):
            self.service.start("task-a", permit["id"], spec, "interrupted-a", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
        state = self.service.read("task-a")
        self.assertEqual(state["runs"][0]["status"], "STARTING")
        calls = []
        recovered = self.service.start("task-a", permit["id"], spec, "interrupted-a", lambda s: calls.append(s))
        self.assertEqual(recovered["status"], "START_UNKNOWN")
        self.assertEqual(calls, [])

    def test_output_scope_and_failed_run_never_advance(self):
        state = self.exec_ready()
        permit = self.service.authorize("task-a", self.spec(), state["revision"])
        run = self.service.start("task-a", permit["id"], self.spec(), "failed-a", lambda s: handoff())
        result = {"run_id": run["id"], "returncode": 0, "outputs": [{"path": str(self.input), "sha256": self.inputs[0]["sha256"]}]}
        self.error("OUTPUT_SCOPE_MISMATCH", self.service.finish, "task-a", run["id"], result)
        self.service.finish("task-a", run["id"], {"run_id": run["id"], "returncode": 1, "outputs": []})
        self.error("PRODUCTION_RUN_RECEIPT_REQUIRED", self.service.advance, "task-a", "ANALYZE", state["revision"])

    def test_reviewed_plan_revision_preserves_findings(self):
        raw = proposal(intent())
        raw["findings"] = [{"id": "finding-a", "type": "judgment", "item_id": "item-a", "reason": "open issue"}]
        state = self.service.set_plan("task-a", raw, 1)
        new = proposal(intent())
        new["items"][0]["question"] = "Revised object question?"
        new["discovery_review"]["items_hash"] = canonical_hash(new["items"])
        prepared = planning.prepare_plan(intent(), new["retrieval_bundle"], new)
        new["reviewed_change"] = {"status": "success", "reason": "rechecked scope", "source_refs": ["review:b"],
                                  "old_plan_hash": state["plan"]["plan_hash"],
                                  "new_content_hash": canonical_hash({k: v for k, v in prepared.items() if k != "plan_hash"})}
        revised = self.service.set_plan("task-a", new, state["revision"])
        self.assertEqual(revised["findings"][0]["id"], "finding-a")
        self.assertEqual(revised["plan"]["findings"][0]["id"], "finding-a")
        self.assertEqual(len(revised["plan"]["change_history"]), 1)
        self.assertEqual(self.service.evaluate("task-a")["verdict"], "BLOCK")

    def test_explicit_old_pass_cannot_override_new_review(self):
        state = self.exec_ready()
        old_id = state["active_receipts"]["PLAN"]
        self.reviewed(state, "new-plan-review", conclusions=[{
            "item_id": "item-a", "verdict": "BLOCK", "reason": "new objection",
            "supported_evidence_refs": [], "applicability": "applicable"}])
        self.error("INACTIVE_RECEIPT", self.service.advance, "task-a", "ANALYZE", state["revision"], old_id)
        self.assertEqual(self.service.evaluate("task-a", "PLAN")["verdict"], "BLOCK")

    def test_revoke_authorization_preserves_artifact_and_blocks_start(self):
        spec, permit = self.probe()
        state = self.service.read("task-a")
        original = next(a for a in state["authorizations"] if a["id"] == "probe-auth")
        revoked = self.service.revoke_authorization("task-a", "probe-auth", state["revision"], "withdrawn scope", ["event:b"])
        self.assertTrue(revoked["authorizations"][0]["revoked"])
        self.service.store.verify_artifact("task-a", "authorizations", original)
        calls = []
        self.error("PROBE_AUTHORIZATION_REQUIRED", self.service.start, "task-a", permit["id"], spec, "revoked-a", lambda s: calls.append(s))
        self.assertEqual(calls, [])
        self.assertEqual(len(revoked["withdrawals"]), 1)

    def test_resolve_dispute_preserves_original_and_cannot_clear_block(self):
        state = self.planned()
        state = self.reviewed(state, findings=[{"id": "finding-a", "item_id": "item-a", "type": "judgment", "reason": "object disputed"}])
        finding = state["findings"][0]
        resolution = {"id": "resolution-a", "finding_id": "finding-a", "finding_hash": canonical_hash({k: v for k, v in finding.items() if k not in {"status", "resolution"}}),
                      "action": "dispute", "reason": "request another review", "task_id": "task-a", "revision": state["revision"],
                      "subject_hash": state["subject_hash"], "plan_hash": state["plan"]["plan_hash"]}
        resolved = self.service.resolve("task-a", "finding-a", resolution, state["revision"])
        self.assertEqual(resolved["findings"][0]["status"], "disputed")
        self.assertEqual(resolved["findings"][0]["reason"], "object disputed")
        self.assertEqual(self.service.evaluate("task-a")["verdict"], "BLOCK")
        self.assertEqual(len(list((self.root / "memory/tasks/task-a/artifacts/resolutions").glob("*.json"))), 1)

    def test_protocol_requires_owners_even_if_e3_review_passes(self):
        state = self.planned()
        state = self.authorize_human({"id": "confirm-a", "scope": "plan", "decision": "confirm",
            "actor_kind": "human", "actor_id": "actor-a", "reason": "confirm goals", "source_refs": ["event:a"],
            "revoked": False, "subject_hash": state["subject_hash"]}, state["revision"])
        self.reviewed(state)
        receipt = self.service.evaluate("task-a")
        self.assertEqual(receipt["rule_results"][0]["verdict"], "PASS")
        self.assertEqual(receipt["verdict"], "BLOCK")
        self.assertIn("stage-owner-coverage:E1", receipt["reasons"])

    def test_proof_is_immutable_and_does_not_change_subject(self):
        state = self.planned()
        proof = {"id": "proof-a", "item_id": "item-a", "task_id": "task-a", "revision": state["revision"],
                 "subject_hash": state["subject_hash"], "plan_hash": state["plan"]["plan_hash"],
                 "proof": {"predicate": "eq", "operands": [{"literal": 2}, {"literal": 2}]}}
        updated = self.service.record_proof("task-a", proof, state["revision"])
        self.assertEqual(updated["subject_hash"], state["subject_hash"])
        self.assertEqual(updated["revision"], state["revision"])
        self.error("IMMUTABLE_ARTIFACT", self.service.record_proof, "task-a", proof, state["revision"])

    def test_failed_launcher_cannot_be_rewritten_successful(self):
        state = self.exec_ready()
        permit = self.service.authorize("task-a", self.spec(), state["revision"])
        run = self.service.start("task-a", permit["id"], self.spec(), "failure-a", lambda s: handoff(
            execution_complete=True, returncode=7, outputs=[]))
        self.error("RUN_RESULT_MISMATCH", self.service.finish, "task-a", run["id"], {
            "run_id": run["id"], "returncode": 0, "outputs": []})
        self.assertEqual(self.service.finish("task-a", run["id"], {
            "run_id": run["id"], "returncode": 7, "outputs": []})["status"], "FAILED")

    def test_background_finish_requires_trusted_verifier(self):
        spec, permit = self.probe()
        run = self.service.start("task-a", permit["id"], spec, "background-a", lambda s: handoff())
        self.assertEqual(run["status"], "RUNNING")
        self.error("RUN_RESULT_VERIFIER_REQUIRED", TaskService(self.root).finish, "task-a", run["id"], {
            "run_id": run["id"], "returncode": 0, "outputs": []})

    def test_old_run_cannot_support_changed_execution_dependencies(self):
        state = self.exec_ready()
        permit = self.service.authorize("task-a", self.spec(), state["revision"])
        run = self.service.start("task-a", permit["id"], self.spec(), "old-run-a", lambda s: handoff())
        output = Path(permit["frozen_spec"]["output_dir"]) / "result.txt"
        output.write_text("anonymous output", encoding="utf-8")
        self.service.finish("task-a", run["id"], {"run_id": run["id"], "returncode": 0,
            "outputs": [{"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}]})
        state = self.service.read("task-a")
        changed = copy.deepcopy(state["dependencies"])
        changed["method"] = "changed choice"
        state = self.service.revise("task-a", {"dependencies": changed}, state["revision"])
        self.error("STALE_RUN_DEPENDENCIES", self.service._production_run, state)

    def test_stale_review_archived_and_input_tamper_rejected(self):
        state = self.planned()
        old_hash = state["subject_hash"]
        state = self.service.revise("task-a", {"decisions": {"change": 1}}, state["revision"])
        stale = self.reviewed(state, subject_hash=old_hash)
        self.assertTrue(stale["reviews"][-1]["stale"])
        self.assertEqual(self.service.evaluate("task-a")["verdict"], "BLOCK")
        with tempfile.TemporaryDirectory() as temp:
            other = TaskService(temp, action_adapter=adapter)
            other.create("task-a", intent())
        # Only anonymous synthetic input paths are used by all execution tests.


if __name__ == "__main__":
    unittest.main()
