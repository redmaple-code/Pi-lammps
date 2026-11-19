"""Actual API/MCP lifecycle; only model transport and external bridge are fakes.

Anonymous input/output bytes are produced in temporary storage. The real task
service, policy, planner, file locks, frozen inputs and receipts are exercised.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "evolution")]
import evidence_api
import md_mcp
from evidence_v3.contracts import atomic_json, canonical_hash, file_hash, read_json
from evidence_v3.execution import normalize_spec


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.input = self.work / "object.json"
        self.input.write_text('{"object":"anonymous-a","value":7}', encoding="utf-8")
        self.script = self.work / "runner.in"
        self.script.write_text("read_data object.json\nwrite_data measurement.json\n", encoding="utf-8")
        self.inputs = [{"path": self.input.name, "sha256": file_hash(self.input)},
                       {"path": self.script.name, "sha256": file_hash(self.script)}]
        self.solver = self.root / "registered solver.exe"
        self.solver.write_bytes(b"anonymous registered executable")
        registration = {"profile_id": "local-lammps-static-v1", "executable": str(self.solver),
                        "command": [str(self.solver), "-in", self.script.name, "-log", "{output:run.log}"],
                        "kinds": ["production", "validation"], "entrypoints": [self.script.name]}
        self.config = {"evidence_execution_registrations": [registration]}
        self.adapter = evidence_api.configured_action_adapter(self.config, bridge_exec=self.bridge_exec)
        self.adapter.worker_launcher = self.fake_worker_launcher
        self.handoff_jobs = []
        self.operations = []
        self.executions = []
        self.task_id = "lifecycle-a"
        self.environment = patch.dict(os.environ, {"MDSIM_DATA_ROOT": str(self.root / "data")})
        self.environment.start()
        self.mode = patch.object(md_mcp, "MODE", "local")
        self.mode.start()
        self.cfg = patch.object(md_mcp, "CFG", self.config)
        self.cfg.start()
        self.bridge = patch.object(md_mcp, "_bridge_exec", side_effect=self.bridge_exec)
        self.bridge.start()
        self.evidence_adapter = patch.object(md_mcp, "_evidence_adapter", return_value=self.adapter)
        self.evidence_adapter.start()

    def tearDown(self):
        self.evidence_adapter.stop()
        self.bridge.stop()
        self.cfg.stop()
        self.mode.stop()
        self.environment.stop()
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o666)
        self.temporary.cleanup()

    def spec(self, kind="production"):
        value = {"kind": kind, "command": [str(self.solver), "-in", self.script.name,
                                              "-log", "{output:run.log}"], "cwd": str(self.work),
                 "limits": {"timeout_seconds": 2}, "environment": {}, "inputs": copy.deepcopy(self.inputs)}
        if kind == "validation":
            value.update(question_id="probe-question", review_id="probe-review")
        else:
            value["authorization_id"] = "production-authorization"
        return normalize_spec(value)

    def call(self, action, **kwargs):
        args = dict(action=action, task_id=self.task_id, **kwargs)
        response = evidence_api.dispatch(args, str(self.root / "data"), self.adapter, self.transport,
                                         human_verifier=self.human_verifier,
                                         review_budget={"max_bytes": 750000, "max_tokens": 750000,
                                             "max_attempts": 2, "max_batches": 64, "max_calls": 96,
                                             "deadline_seconds": 180})
        self.assertNotIn("error", response, (action, response))
        return response

    def human_verifier(self, task_id, request, context):
        actor_id = "test-owner"
        return {"actor_id": actor_id, "source_refs": ["test-event:" + request["id"]],
                "authorization_source": {"kind": "trusted_human_event", "source_id": "event:" + request["id"],
                    "actor_id": actor_id, "request_hash": canonical_hash(request), "verified": True}}

    def status(self):
        return self.call("status")

    def transport(self, request):
        """Model responses cite actual material; the policy decides every gate."""
        operation = request["operation"]
        self.operations.append(operation)
        if operation == "recognize_intent":
            citation = {"path": "/request", "quote": request["context"]["request"]}
            return {"context_hash": request["context_hash"], "intent": {
                "action": "production", "goals": [{"id": "goal-a", "text": "Report the anonymous object value", "source_refs": [citation]}],
                "conditions": {}, "unknowns": [], "source_refs": [citation]}}
        if operation == "propose_checks":
            citation = {"path": "/intent/goals/0/text", "quote": request["context"]["intent"]["goals"][0]["text"]}
            items = []
            for identity, owner, due, question in (
                ("design", "E1", "PLAN", "Does the supplied object support this task's design scope?"),
                ("interpretation", "E3", "PLAN", "Is the proposed interpretation limited to the requested object?"),
                ("input-check", "E4", "BUILD", "Does the complete input describe the requested object?"),
                ("result-interpretation", "E3", "ANALYZE", "Does the actual result support the interpretation?"),
                ("measurement", "E2", "ANALYZE", "What value came from the selected run output?"),
                ("acceptance", "E5", "ANALYZE", "Does the delivered value answer the stated goal?")):
                row = {"id": identity, "question": question, "claim": "claim-" + identity,
                       "target_refs": ["goal-a"], "owner_rule": owner, "due_stage": due,
                       "origin_refs": request["context"]["intent"]["source_refs"], "rationale": "Derived from requested delivery",
                       "applicability": {"status": "applicable", "reason": "Current task scope", "source_refs": ["request:scope"]}}
                if due == "ANALYZE":
                    row["claim_id"] = "claim-measurement"
                    row["claim"] = "The selected run reports the requested anonymous object's value from its frozen input"
                if owner == "E2":
                    # Value, run ID and output hash cannot exist before execution.
                    row["measurement"] = {"claim_id": row["claim_id"], "metric": "object-value", "unit": "count",
                                          "window": [0, 1], "tolerance": 0}
                items.append(row)
            return {"context_hash": request["context_hash"], "citations": [citation], "proposal": {
                "items": items, "discovery_review": {"goal_coverage": {"goal-a": [row["id"] for row in items]},
                    "goal_coverage_rationale": "Interpretation, exact inputs, run measurement and delivery each have an owner",
                    "counterexample_review": {"rationale": "Checked wrong-input and wrong-run alternative explanations",
                        "reviewed_item_ids": [row["id"] for row in items],
                        "query_refs": [q["id"] for q in request["context"]["retrieval"]["queries"] if q["purpose"] == "counterexample"],
                        "limitations": ["Only local task material available"], "citations": [citation]}, "remaining_unknowns": []}}}
        context = request["context"]
        input_reviews = [{"path": row["path"], "rationale": "Read complete anonymous input and its explicit object value",
                          "citations": [{"path": "/inputs/" + str(i) + "/content", "quote": row["content"]}]}
                         for i, row in enumerate(context.get("inputs", []))]
        if operation == "review_validation":
            citation = {"path": "/validation_spec/question_id", "quote": "probe-question"}
            return {"context_hash": request["context_hash"], "subject_hash": request["subject_hash"], "spec_hash": request["spec_hash"],
                    "question_review": {"rationale": "Independent object-value probe", "citations": [citation]},
                    "resource_review": {"rationale": "Bounded to one exact input and configured timeout", "citations": [citation]},
                    "conclusion": {"verdict": "PASS", "reason": "Probe own inputs and bounds are reviewed", "citations": [citation]},
                    "reviewed_inputs": [row["path"] for row in context["inputs"]], "input_reviews": input_reviews,
                    "findings": [], "limitations": ["Does not review or permit production"]}
        self.assertIn(operation, ("review_plan", "review_input"))
        citation = {"path": "/intent/goals/0/text", "quote": context["intent"]["goals"][0]["text"]}
        evidence = context.get("evidence", [])
        conclusions = []
        for row in context["plan"]["items"]:
            claim_id = row.get("claim_id", row["claim"])
            selected_run_id = context.get("decisions", {}).get("selected_run_id")
            support = [e["id"] for e in evidence if e["claim_id"] == claim_id
                       and (row["due_stage"] != "ANALYZE" or e.get("run_id") == selected_run_id)]
            conclusion = {"item_id": row["id"], "verdict": "PASS", "reason": "Reviewed declared protocol within current scope",
                          "supported_evidence_refs": support, "applicability": "applicable", "citations": [citation]}
            if row["due_stage"] == "ANALYZE" and support:
                index = next(i for i, e in enumerate(evidence) if e["id"] == support[0])
                measurement = evidence[index]
                self.assertEqual(measurement["run_id"], selected_run_id)
                self.assertEqual(measurement["claim_id"], claim_id)
                self.assertEqual(read_json(measurement["verification"]["source_locator"])["value"], measurement["value"])
                conclusion["citations"] = [{"path": "/evidence/" + str(index) + "/run_id", "quote": selected_run_id},
                                          {"path": "/evidence/" + str(index) + "/claim_id", "quote": claim_id},
                                          {"path": "/evidence/" + str(index) + "/value", "quote": json.dumps(measurement["value"])}]
                conclusion["reason"] = "The selected run's verified object-value measurement supports this exact result claim"
            if row["owner_rule"] == "E1":
                source_index = next(i for i, e in enumerate(evidence) if e["id"] == "design-material")
                conclusion["citations"] = [{"path": "/evidence/" + str(source_index) + "/content", "quote": evidence[source_index]["content"]}]
                conclusion["reason"] = "The supplied source explicitly identifies one anonymous object and its value; design is limited to reading that object"
            if row["owner_rule"] == "E5":
                measurements = [e for e in evidence if e["source_kind"] == "measurement"
                                and e["claim_id"] == claim_id and e.get("run_id") == selected_run_id]
                delivered = bool(measurements) and measurements[0]["value"] == json.loads(self.input.read_text())["value"]
                conclusion["acceptance_results"] = [{"id": "goal-a", "passed": delivered,
                    "reason": "Delivered selected-run value matches object" if delivered else "Delivery is pending actual run evidence"}]
            conclusions.append(conclusion)
        return {"context_hash": request["context_hash"], "subject_hash": request["subject_hash"], "plan_hash": request["plan_hash"],
                "item_reviews": [{"item_id": row["id"], "assessment": "supported", "rationale": "Question and method fit the stated goal", "citations": [citation]}
                                 for row in context["plan"]["items"]],
                "goal_coverage": [{"goal_id": "goal-a", "item_ids": request["item_ids"], "rationale": "Complete task-specific mapping", "citations": [citation]}],
                "counterexample_review": {"rationale": "Wrong input, run and missing delivery paths reviewed", "citations": [citation]},
                "conclusions": conclusions, "reviewed_inputs": [row["path"] for row in context.get("inputs", [])],
                "input_reviews": input_reviews, "findings": [], "remaining_unknowns": [], "limitations": ["Anonymous single-object example"]}

    def initialize(self):
        value = self.call("recognize", request="Report the anonymous object value")
        snapshot = self.call("retrieve", intent=value, knowledge_dirs=[], limit=2)
        draft = self.call("propose", intent=value, bundle=snapshot)
        self.call("init", intent=value, request_id="create-a")
        state = self.call("plan", bundle=snapshot, proposal=draft, expected_revision=1)
        self.assertEqual({i["owner_rule"] for i in state["plan"]["items"]}, {"E1", "E2", "E3", "E4", "E5"})
        state = self.call("revise", patch={"dependencies": {"cwd": str(self.work), "inputs": self.inputs}}, expected_revision=state["revision"])
        source = {"id": "design-material", "claim_id": "claim-design", "source_kind": "human", "lifecycle": "active",
                  "conditions": {}, "provenance_refs": value["source_refs"],
                  "verification": {"status": "verified", "method": "content_review", "source_locator": str(self.input)},
                  "content": self.input.read_text(encoding="utf-8"), "source_sha256": file_hash(self.input)}
        return self.call("evidence", record=source, expected_revision=state["revision"])

    def human(self, scope, identity, decision="allow", spec=None):
        state = self.status()
        authorization = {"id": identity, "scope": scope, "decision": decision, "actor_kind": "human", "actor_id": "owner-a",
                         "reason": "Approved this exact scope", "subject_hash": state["subject_hash"],
                         "source_refs": ["user-event:" + identity], "revoked": False}
        if spec is not None:
            authorization["spec_hash"] = canonical_hash(spec)
        return self.call("authorization", authorization=authorization, expected_revision=state["revision"])

    def review(self, kind="plan", spec=None):
        args = {"kind": kind, "expected_revision": self.status()["revision"]}
        if spec is not None:
            args["spec"] = spec
        return self.call("review", **args)

    def ready(self):
        self.initialize()
        self.human("plan", "confirm-plan", "confirm")
        self.review()
        for next_stage in ("BUILD", "CONFIG", "EXEC"):
            if self.status()["current_stage"] == "CONFIG":
                self.review("input")
            receipt = self.call("evaluate")
            self.assertEqual(receipt["verdict"], "PASS", receipt)
            self.call("advance", to_stage=next_stage, receipt_id=receipt["id"], expected_revision=self.status()["revision"])
        receipt = self.call("evaluate")
        self.assertEqual(receipt["verdict"], "PASS", receipt)

    def bridge_exec(self, args):
        """Only this external boundary creates synthetic execution output."""
        self.executions.append(copy.deepcopy(args))
        if str(self.solver) in args["cmd"] and "-h" in args["cmd"]:
            return {"audit_id": "version-probe", "whitelisted": True, "returncode": 0,
                    "timeout_enforced": True, "timeout_seconds": args["timeout_seconds"],
                    "stdout": "LAMMPS", "stderr": ""}
        state = self.status()
        run = state["runs"][-1]
        permit = next(p for p in state["permits"] if p["id"] == run["permit_id"])
        self.assertEqual(Path(args["cwd"]), Path(permit["frozen_spec"]["output_dir"]))
        self.assertNotEqual(Path(args["cwd"]), self.work)
        self.assertEqual(args["timeout_seconds"], 2)
        self.assertEqual(args["background"], False)
        self.assertNotIn("environment", args)
        value = json.loads((Path(args["cwd"]) / self.input.name).read_text())["value"]
        run_log = Path(args["cwd"]) / "run.log"
        run_log.write_text("LAMMPS anonymous fake bridge run", encoding="utf-8")
        output = Path(args["cwd"]) / "measurement.json"
        output.write_text(json.dumps({"task_id": self.task_id, "run_id": run["id"],
            "input_hash": file_hash(Path(args["cwd"]) / self.input.name), "claim_id": "claim-measurement",
            "metric": "object-value", "unit": "count", "window": [0, 1], "tolerance": 0,
            "value": value}), encoding="utf-8")
        return {"audit_id": "audit-" + run["id"], "whitelisted": True, "returncode": 0,
                "timeout_enforced": True, "timeout_seconds": args["timeout_seconds"],
                "stdout": "", "stderr": ""}

    def fake_worker_launcher(self, job_path):
        self.handoff_jobs.append(Path(job_path))
        return {"started": True}

    def complete_fake_handoff(self, run):
        job_path = self.handoff_jobs[-1]
        job = read_json(job_path)
        frozen = job["frozen_spec"]
        output_dir = Path(frozen["output_dir"])
        registration = self.adapter._registration(frozen)
        closure = self.adapter._closure(registration, frozen)
        for entry in closure["inputs"]:
            source = Path(frozen["cwd"]) / entry["path"]
            destination = output_dir / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        measurement = output_dir / "measurement.json"
        object_input = output_dir / self.input.name
        atomic_json(measurement, {"task_id": self.task_id, "run_id": run["id"],
            "input_hash": file_hash(object_input), "claim_id": "claim-measurement",
            "metric": "object-value", "unit": "count", "window": [0, 1], "tolerance": 0,
            "value": json.loads(object_input.read_text(encoding="utf-8"))["value"]})
        (output_dir / "run.log").write_text("fake registered run", encoding="utf-8")
        outputs = [{"path": str((output_dir / name).resolve()),
                    "sha256": file_hash(output_dir / name)} for name in closure["outputs"]]
        journal_path = job_path.with_name(job_path.name.removesuffix(".job.json") + ".json")
        journal = read_json(journal_path)
        self.assertEqual(journal["launch_id"], run["launch_id"])
        self.assertEqual(journal["registration_hash"], canonical_hash(registration))
        atomic_json(journal_path, {**journal, "state": "complete", "result": {
            "started": True, "execution_complete": True, "returncode": 0,
            "launch_id": run["launch_id"], "outputs": outputs,
            "registration_hash": canonical_hash(registration)}})
        self.executions.append({"audit_id": "audit-" + run["id"], "kind": run["kind"],
                                "timeout_seconds": frozen["limits"]["timeout_seconds"],
                                "background": False})

    def execute(self, kind="production"):
        spec = self.spec(kind)
        self.human(kind, "production-authorization" if kind == "production" else "probe-authorization", spec=spec)
        if kind == "validation":
            self.review("validation", spec)
        else:
            # Authorizations are review inputs; refresh the exact current reviews.
            self.review("plan")
            self.review("input")
            for stage in ("PLAN", "BUILD", "CONFIG", "EXEC"):
                receipt = self.call("evaluate", stage=stage)
                self.assertEqual(receipt["verdict"], "PASS", receipt)
        permit = self.call("authorize", spec=spec, expected_revision=self.status()["revision"])
        request = {"task_id": self.task_id, "permit_id": permit["id"], "request_id": "start-" + kind, "spec": spec}
        run = md_mcp.sim_exec(request)
        self.assertNotIn("error", run, run)
        self.assertEqual(run["status"], "RUNNING", run)
        self.complete_fake_handoff(run)
        run = md_mcp.sim_exec({"action": "reconcile", "task_id": self.task_id,
                               "run_id": run["id"],
                               "receipt": {"launch_id": run["launch_id"]}})
        self.assertNotIn("error", run, run)
        self.assertEqual(run["status"], "FINISHED", run)
        self.assertEqual(md_mcp.sim_exec(request)["id"], run["id"])
        self.assertEqual(len(self.executions), 3 if kind == "production" else 4)
        output = Path(permit["frozen_spec"]["output_dir"]) / "measurement.json"
        self.assertIn(str(output), {row["path"] for row in run["outputs"]})
        return permit, run, output

    def test_api_to_mcp_production_finish(self):
        self.ready()
        permit, run, output = self.execute()
        self.assertEqual(run["kind"], "production")
        self.assertEqual(json.loads(output.read_text())["run_id"], run["id"])
        self.assertNotEqual(permit["frozen_spec"]["cwd"], str(self.work))
        for operation in ("recognize_intent", "propose_checks", "review_plan", "review_input"):
            self.assertIn(operation, self.operations)

    def test_complete_selected_run_with_real_e2_e3_e5_results(self):
        self.ready()
        permit, run, output = self.execute()
        self.assertEqual(len(run["inputs"]), 2)
        input_source = next(Path(row["path"]) for row in run["inputs"] if Path(row["path"]).name == self.input.name)
        self.assertTrue(input_source.is_absolute())
        actual = read_json(output)
        self.assertEqual(actual["run_id"], run["id"])
        record = {"id": "measurement-record", "source_kind": "measurement", "lifecycle": "active",
                  "claim_id": "claim-measurement", "task_id": self.task_id, "run_id": run["id"],
                  "input_hash": file_hash(input_source), "output_hash": file_hash(output),
                  "metric": actual["metric"], "unit": actual["unit"], "value": actual["value"],
                  "window": actual["window"], "tolerance": actual["tolerance"], "conditions": {},
                  "provenance_refs": [run["receipt_id"]],
                  "verification": {"status": "verified", "method": "recompute",
                                   "input_locator": str(input_source), "source_locator": str(output)}}
        before = self.status()
        state = self.call("evidence", record=record, expected_revision=before["revision"])
        self.assertGreater(state["revision"], before["revision"])
        self.assertEqual(state["active_receipts"], {})
        state = self.call("revise", patch={"decisions": {"selected_run_id": run["id"]}}, expected_revision=state["revision"])
        self.human("plan", "confirm-results", "confirm")
        self.review("plan")
        self.review("input")
        for stage in ("PLAN", "BUILD", "CONFIG", "EXEC"):
            receipt = self.call("evaluate", stage=stage)
            self.assertEqual(receipt["verdict"], "PASS", receipt)
        state = self.call("advance", to_stage="ANALYZE", expected_revision=self.status()["revision"])
        original_output = output.read_bytes()
        changed_output = dict(actual, value=actual["value"] + 1)
        output.write_text(json.dumps(changed_output), encoding="utf-8")
        rejected = self.call("evaluate")
        self.assertEqual(rejected["verdict"], "BLOCK", rejected)
        measurement = next(row for row in rejected["rule_results"] if row["item_id"] == "measurement")
        self.assertEqual(measurement["verdict"], "BLOCK", measurement)
        output.write_bytes(original_output)
        receipt = self.call("evaluate")
        self.assertEqual(receipt["verdict"], "PASS", receipt)
        by_item = {row["item_id"]: row for row in receipt["rule_results"]}
        self.assertEqual(by_item["measurement"]["evidence_refs"], [record["id"]])
        for identity, owner in (("measurement", "E2"), ("result-interpretation", "E3"), ("acceptance", "E5")):
            obligation = next(row for row in state["plan"]["items"] if row["id"] == identity)
            self.assertEqual(obligation["claim_id"], record["claim_id"])
            self.assertEqual(by_item[identity]["rule"], owner)
            self.assertEqual(by_item[identity]["verdict"], "PASS", by_item[identity])
            self.assertEqual(by_item[identity]["evidence_refs"], [record["id"]])
        state = self.call("complete", expected_revision=state["revision"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["current_stage"], "ANALYZE")
        self.assertEqual(len(state["runs"]), 1)
        self.assertEqual(len(self.executions), 3)
        self.assertEqual(state["plan"]["plan_hash"], before["plan"]["plan_hash"])
        self.assertEqual(file_hash(Path(permit["frozen_spec"]["cwd"]) / self.input.name), self.inputs[0]["sha256"])

    def test_plan_validation_does_not_clear_production(self):
        self.initialize()
        blocked_before = self.call("evaluate")
        self.assertEqual(blocked_before["verdict"], "BLOCK")
        _permit, run, _output = self.execute("validation")
        self.assertEqual(run["kind"], "validation")
        self.assertEqual(self.status()["current_stage"], "PLAN")
        self.assertEqual(self.call("evaluate")["verdict"], "BLOCK")
        failure = evidence_api.dispatch({"action": "authorize", "task_id": self.task_id, "spec": self.spec(),
                                        "expected_revision": self.status()["revision"]}, str(self.root / "data"), self.adapter, self.transport,
                                        human_verifier=self.human_verifier)
        self.assertEqual(failure.get("error"), "EXEC_STAGE_REQUIRED")
        self.assertEqual(len(self.executions), 4)


if __name__ == "__main__":
    unittest.main()
