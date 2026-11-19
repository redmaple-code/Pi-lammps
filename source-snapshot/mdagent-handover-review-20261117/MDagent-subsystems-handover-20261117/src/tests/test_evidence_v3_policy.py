"""Anonymous positive controls and counterexamples for evidence policy."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evolution"))
from evidence_v3 import policy
from evidence_v3.contracts import V3Error, canonical_hash


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = {"task_id": "task-a", "revision": 1,
                        "subject_hash": canonical_hash({"a": 1}),
                        "plan_hash": canonical_hash({"b": 2}), "run_id": "run-a",
                        "findings": [], "authorizations": []}
        self.item = {"id": "item-a", "claim_id": "claim-a", "claim_kind": "design",
                     "conditions": {}, "applicability": "applicable"}
        self.records = [{"id": "record-a", "source_kind": "publication", "claim_id": "claim-a",
                         "verification": {"status": "verified", "method": "content_review",
                                          "source_locator": "source-a"},
                         "conditions": {}, "lifecycle": "active", "provenance_refs": []}]

    def binding(self):
        return {key: self.context[key] for key in
                ("task_id", "revision", "subject_hash", "plan_hash")}

    def reviewed(self, verdict="PASS", **updates):
        conclusion = {"item_id": self.item["id"], "verdict": verdict, "reason": "reason-a",
                      "supported_evidence_refs": [row["id"] for row in self.records],
                      "applicability": "applicable"}
        conclusion.update(updates)
        self.context["review"] = dict(self.binding(), status="success", method="semantic",
                                     content_hash=policy.review_content_hash(self.item, self.records, self.context),
                                     item_ids=[self.item["id"]],
                                     evidence_refs=[row["id"] for row in self.records],
                                     conclusions=[conclusion])

    def evaluate(self, rule, verdict):
        before = copy.deepcopy((self.item, self.records, self.context))
        result = policy.evaluate(rule, self.item, self.records, self.context)
        self.assertEqual(result["verdict"], verdict, result)
        self.assertEqual((self.item, self.records, self.context), before)
        self.assertEqual(set(result), {"rule", "item_id", "subject_hash", "plan_hash", "verdict",
                                       "reasons", "evidence_refs", "limitations"})
        return result

    def authorization(self, finding_id=None):
        authorization = {"id": "authorization-a", "scope": self.item["id"],
                         "subject_hash": self.context["subject_hash"], "finding_id": finding_id,
                         "reason": "reason-a", "decision": "accept_assumption", "revoked": False}
        self.context["authorizations"] = [authorization]
        return authorization

    def proof(self, left=1, right=1):
        return dict(self.binding(), predicate="eq", operands=[{"literal": left}, {"literal": right}],
                    premises=[{"predicate": "eq", "operands": [{"literal": "a"}, {"literal": "a"}]}])

    def finding(self, kind="judgment", **updates):
        value = {"id": "finding-a", "type": kind, "item_id": self.item["id"], "severity": "hard"}
        value.update(updates)
        return value

    def resolution(self, finding, action="correct", **updates):
        resolution = dict(self.binding(), id="resolution-a", finding_id=finding["id"],
                          finding_hash=canonical_hash({key: value for key, value in finding.items()
                                                      if key not in {"resolution", "status"}}),
                          action=action, reason="reason-a", supersedes=finding["id"])
        resolution.update(updates)
        resolution["review"] = dict(self.binding(), status="success", method="semantic", decision=action,
                                    content_hash=canonical_hash({"finding_hash": resolution["finding_hash"],
                                                                 "resolution": copy.deepcopy(resolution)}))
        return resolution

    def measurement(self, value=3):
        input_path = self.root / "input.json"
        output_path = self.root / "output.json"
        input_path.write_text('{"a":1}', encoding="utf-8")
        input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
        row = {"task_id": self.context["task_id"], "run_id": self.context["run_id"],
               "input_hash": input_hash, "claim_id": "claim-a", "metric": "metric-a", "unit": "unit-a",
               "value": value, "window": [0, 4], "tolerance": 0.01}
        output_path.write_text(json.dumps(row), encoding="utf-8")
        expected = dict(row, output_hash=hashlib.sha256(output_path.read_bytes()).hexdigest())
        self.item["measurement"] = expected
        self.records = [dict(expected, id="record-a", source_kind="measurement", conditions={},
                             provenance_refs=[], lifecycle="active", verification={"status": "verified",
                                 "method": "recompute", "source_locator": str(output_path),
                                 "input_locator": str(input_path)})]
        inputs = [{"path": str(input_path), "sha256": input_hash}]
        outputs = [{"path": str(output_path), "sha256": expected["output_hash"]}]
        self.context["runs"] = [{"id": self.context["run_id"], "task_id": self.context["task_id"],
                                 "status": "FINISHED", "receipt_id": "receipt-a", "inputs": inputs,
                                 "input_hash": canonical_hash(inputs), "outputs": outputs,
                                 "output_hash": canonical_hash(outputs)}]
        return input_path, output_path

    def independent_check(self, proof):
        path = self.root / "check-source.json"
        value = proof["operands"][0].get("literal", 7)
        path.write_text(json.dumps({"a": value}), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        proof = copy.deepcopy(proof)
        proof["operands"][0] = {"path": str(path), "sha256": digest, "pointer": "/a"}
        artifact = dict(self.binding(), id="proof-a", item_id=self.item["id"], input_hash=digest,
                        verification={"status": "verified", "method": "deterministic", "source_locator": str(path)},
                        proof=proof)
        self.context["proofs"] = [artifact]
        self.item["checks"] = [{"id": "check-a", "proof_ref": "proof-a"}]
        return artifact

    def test_e1_support_and_no_source_ranking(self):
        for kind in ("human", "publication", "measurement", "case", "experience", "estimate"):
            with self.subTest(kind=kind):
                self.records[0]["source_kind"] = kind
                self.reviewed()
                result = self.evaluate("E1", "PASS")
                self.assertEqual(result["evidence_refs"], ["record-a"])

    def test_e1_claim_support_and_material_verification_required(self):
        self.reviewed()
        self.records[0]["claim_id"] = "claim-b"
        self.reviewed()
        self.evaluate("E1", "BLOCK")
        self.records[0]["claim_id"] = "claim-a"
        for status in ("parsed", "retrieved", "unknown"):
            with self.subTest(status=status):
                self.records[0]["verification"]["status"] = status
                self.reviewed()
                self.evaluate("E1", "BLOCK")

    def test_legal_design_assumption_is_not_pass_or_fact(self):
        self.records = []
        self.item["allow_assumption"] = True
        self.authorization()
        result = self.evaluate("E1", "ACCEPTED_ASSUMPTION")
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(result["limitations"][0]["authorization_id"], "authorization-a")
        self.item["claim_kind"] = "result"
        self.evaluate("E1", "BLOCK")

    def test_assumption_authorization_exact_scope_and_revocation(self):
        self.records = []
        self.item["allow_assumption"] = True
        for key, value in (("scope", "task-a"), ("scope", ["item-a", "item-b"]),
                           ("subject_hash", "0" * 64), ("revoked", True),
                           ("reason", ""), ("finding_id", "finding-b")):
            with self.subTest(key=key, value=value):
                authorization = self.authorization()
                authorization[key] = value
                self.evaluate("E1", "BLOCK")

    def test_review_success_and_exact_content_binding(self):
        self.reviewed()
        self.evaluate("E3", "PASS")
        for key, value in (("status", "error"), ("status", "degraded"),
                           ("method", "parsed"), ("subject_hash", "0" * 64),
                           ("plan_hash", "0" * 64), ("revision", 2),
                           ("content_hash", "0" * 64), ("item_ids", []), ("evidence_refs", [])):
            with self.subTest(key=key, value=value):
                self.reviewed()
                self.context["review"][key] = value
                self.evaluate("E3", "BLOCK")

    def test_stale_review_after_every_substantive_input(self):
        for target in ("obligation", "evidence", "findings", "run_id"):
            with self.subTest(target=target):
                saved = copy.deepcopy((self.item, self.records, self.context))
                self.reviewed()
                if target == "obligation":
                    self.item["conditions"]["a"] = 2
                elif target == "evidence":
                    self.records[0]["conditions"]["a"] = 2
                elif target == "findings":
                    self.context["findings"] = [self.finding("advisory")]
                elif target == "authorizations":
                    self.authorization()
                else:
                    self.context["run_id"] = "run-b"
                self.evaluate("E3", "BLOCK")
                self.item, self.records, self.context = saved

    def test_not_applicable_requires_bound_semantic_rationale(self):
        self.reviewed("NOT_APPLICABLE", applicability="inapplicable")
        self.evaluate("E1", "BLOCK")
        self.item["applicability"] = "inapplicable"
        self.reviewed("NOT_APPLICABLE", applicability="inapplicable")
        self.evaluate("E1", "NOT_APPLICABLE")
        self.context["review"]["conclusions"][0]["reason"] = ""
        self.evaluate("E1", "BLOCK")

    def test_e2_real_source_pass_and_tolerance(self):
        self.measurement()
        self.evaluate("E2", "PASS")
        self.item["measurement"]["value"] += 0.005
        self.records[0]["value"] = self.item["measurement"]["value"]
        self.evaluate("E2", "PASS")
        self.item["measurement"]["value"] = 99999
        self.records[0]["value"] = 99999
        self.evaluate("E2", "BLOCK")

    def test_e2_record_binding_each_field(self):
        for key, value in (("claim_id", "claim-b"), ("task_id", "task-b"), ("run_id", "run-b"),
                           ("input_hash", "0" * 64), ("output_hash", "0" * 64),
                           ("metric", "metric-b"), ("unit", "unit-b"), ("value", 99999),
                           ("window", [1, 3]), ("tolerance", 5)):
            with self.subTest(key=key):
                self.measurement()
                self.records[0][key] = value
                self.evaluate("E2", "BLOCK")

    def test_e2_source_field_binding_not_just_asserted_record(self):
        for key, value in (("claim_id", "claim-b"), ("task_id", "task-b"), ("run_id", "run-b"),
                           ("input_hash", "0" * 64), ("metric", "metric-b"), ("unit", "unit-b"),
                           ("value", 99999), ("window", [1, 3]), ("tolerance", 4)):
            with self.subTest(key=key):
                _, output = self.measurement()
                source = json.loads(output.read_text(encoding="utf-8"))
                source[key] = value
                output.write_text(json.dumps(source), encoding="utf-8")
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                self.records[0]["output_hash"] = digest
                self.item["measurement"]["output_hash"] = digest
                self.evaluate("E2", "BLOCK")

    def test_e2_changed_missing_input_output_and_placeholder(self):
        for part in ("input-change", "output-change", "input-missing", "output-missing", "placeholder"):
            with self.subTest(part=part):
                input_path, output_path = self.measurement()
                if part == "input-change":
                    input_path.write_text("{}", encoding="utf-8")
                elif part == "output-change":
                    output_path.write_text("{}", encoding="utf-8")
                elif part == "input-missing":
                    input_path.unlink()
                elif part == "output-missing":
                    output_path.unlink()
                else:
                    self.records[0]["verification"]["source_locator"] = "source:placeholder"
                self.evaluate("E2", "BLOCK")

    def test_e2_no_other_source_or_authority_can_supply_current_measurement(self):
        self.measurement()
        for kind in ("human", "publication", "case", "experience", "estimate"):
            with self.subTest(kind=kind):
                self.records[0]["source_kind"] = kind
                self.authorization()
                self.evaluate("E2", "BLOCK")

    def test_nonfinite_and_boolean_numbers_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf"), True):
            with self.subTest(value=value):
                self.measurement()
                self.item["measurement"]["value"] = value
                self.records[0]["value"] = value
                self.evaluate("E2", "BLOCK")
        for tolerance in (-1, float("inf"), True):
            self.measurement()
            self.item["measurement"]["tolerance"] = tolerance
            self.records[0]["tolerance"] = tolerance
            self.evaluate("E2", "BLOCK")

    def test_e2_source_nonfinite_duplicate_claim_and_duplicate_json_key(self):
        for mode in ("nonfinite", "duplicate-claim", "duplicate-key"):
            with self.subTest(mode=mode):
                _, output = self.measurement()
                source = json.loads(output.read_text(encoding="utf-8"))
                if mode == "nonfinite":
                    source["value"] = float("nan")
                    payload = json.dumps(source)
                elif mode == "duplicate-claim":
                    payload = json.dumps({"measurements": [source, source]})
                else:
                    payload = json.dumps(source)[:-1] + ',"value":3}'
                output.write_text(payload, encoding="utf-8")
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                self.records[0]["output_hash"] = digest
                self.item["measurement"]["output_hash"] = digest
                self.evaluate("E2", "BLOCK")

    def test_e4_deterministic_check_recomputes_not_pass_boolean(self):
        artifact = self.independent_check(self.proof())
        self.item["checks"][0]["passed"] = False
        self.evaluate("E4", "PASS")
        artifact["proof"]["operands"][1]["literal"] = 2
        self.item["checks"][0]["passed"] = True
        self.evaluate("E4", "BLOCK")
        artifact["subject_hash"] = "0" * 64
        self.evaluate("E4", "BLOCK")

    def test_e4_hash_bound_json_pointer_and_premises(self):
        path = self.root / "a.json"
        path.write_text('{"a":[7]}', encoding="utf-8")
        proof = self.proof(7, 7)
        proof["operands"][0] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "pointer": "/a/0"}
        self.item["checks"] = [{"id": "check-a", "proof_ref": "proof-a"}]
        self.context["proofs"] = [dict(self.binding(), id="proof-a", item_id="item-a", proof=proof,
                                      input_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                                      verification={"status": "verified", "method": "deterministic",
                                                    "source_locator": str(path)})]
        self.evaluate("E4", "PASS")
        path.write_text('{"a":[8]}', encoding="utf-8")
        self.evaluate("E4", "BLOCK")
        proof["operands"][0] = {"literal": 7}
        proof["premises"] = []
        self.evaluate("E4", "BLOCK")

    def test_e4_semantic_alternative_and_unsupported_checker(self):
        self.reviewed()
        self.evaluate("E4", "PASS")
        self.independent_check(dict(self.proof(), predicate="unknown"))
        self.reviewed()
        result = self.evaluate("E4", "BLOCK")
        self.assertEqual(result["reasons"][0]["code"], "unsupported-predicate")

    def test_ai_hard_not_deterministic_proof(self):
        finding = self.finding("deterministic", proof={"passed": False})
        normalized = policy.classify_finding(finding)
        self.assertEqual(normalized["type"], "judgment")
        self.assertEqual(normalized["reported_type"], "deterministic")
        self.context["findings"] = [finding]
        self.reviewed()
        self.evaluate("E3", "BLOCK")
        finding["resolution"] = self.resolution(finding, "accept",
                                                authorization=self.authorization(finding["id"]))
        self.reviewed()
        self.evaluate("E3", "PASS")

    def test_dispute_retains_block_and_original(self):
        finding = self.finding()
        resolution = self.resolution(finding, "dispute")
        result = policy.resolve_finding(finding, resolution, self.context)
        self.assertEqual(result["status"], "disputed")
        self.assertEqual(result["resolution"]["reason"], "reason-a")
        self.assertNotIn("resolution", finding)
        finding["resolution"] = resolution
        self.context["findings"] = [finding]
        self.reviewed()
        self.evaluate("E3", "BLOCK")

    def test_successful_correction_preserves_supersedes_and_unblocks(self):
        finding = self.finding()
        finding["resolution"] = self.resolution(finding)
        self.context["findings"] = [finding]
        self.reviewed()
        result = self.evaluate("E3", "PASS")
        self.assertEqual(result["limitations"][0]["status"], "corrected")
        self.assertEqual(finding["resolution"]["supersedes"], finding["id"])
        finding["resolution"]["review"]["status"] = "error"
        self.reviewed()
        self.evaluate("E3", "BLOCK")

    def test_correction_content_and_finding_fingerprint_bound(self):
        finding = self.finding()
        for target in ("reason", "finding_hash", "review_hash", "supersedes"):
            with self.subTest(target=target):
                resolution = self.resolution(finding)
                if target == "review_hash":
                    resolution["review"]["content_hash"] = "0" * 64
                else:
                    resolution[target] = "changed"
                with self.assertRaises(V3Error):
                    policy.resolve_finding(finding, resolution, self.context)

    def test_valid_contradiction_cannot_be_accepted_corrected_or_na(self):
        finding = self.finding("deterministic", proof=self.proof(1, 2))
        self.assertEqual(policy.classify_finding(finding)["type"], "deterministic")
        for action in ("accept", "correct", "not_applicable"):
            with self.subTest(action=action):
                authorization = self.authorization(finding["id"])
                resolution = self.resolution(finding, action, authorization=authorization,
                                             replacement_proof=self.proof())
                with self.assertRaises(V3Error):
                    policy.resolve_finding(finding, resolution, self.context)
        self.context["findings"] = [finding]
        self.reviewed()
        self.evaluate("E3", "BLOCK")

    def test_contradiction_correction_requires_new_subject_and_valid_replacement(self):
        finding = self.finding("deterministic", proof=self.proof(1, 2))
        self.context["subject_hash"] = canonical_hash({"a": 2})
        self.context["revision"] = 2
        resolution = self.resolution(finding, replacement_proof=self.proof(),
                                     supersedes_proof_hash=canonical_hash(finding["proof"]))
        result = policy.resolve_finding(finding, resolution, self.context)
        self.assertEqual(result["status"], "corrected")
        self.assertEqual(result["proof"], finding["proof"])
        finding["resolution"] = resolution
        self.context["findings"] = [finding]
        self.reviewed()
        self.evaluate("E3", "PASS")
        resolution["replacement_proof"]["operands"][0]["literal"] = 9
        self.reviewed()
        self.evaluate("E3", "BLOCK")

    def test_infra_cannot_be_accepted_or_recovered_with_boolean(self):
        finding = self.finding("infra")
        for action, recovery in (("accept", {}), ("correct", {"status": "success"}),
                                 ("correct", {"status": "success", "proof": self.proof(1, 2)})):
            with self.subTest(action=action, recovery=recovery):
                resolution = self.resolution(finding, action, recovery=recovery,
                                             authorization=self.authorization(finding["id"]))
                with self.assertRaises(V3Error):
                    policy.resolve_finding(finding, resolution, self.context)
        path = self.root / "recovery.json"
        path.write_text('{"status":"success"}', encoding="utf-8")
        proof = self.proof("success", "success")
        proof["operands"][0] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "pointer": "/status"}
        recovery = {"status": "success", "proof": proof}
        resolution = self.resolution(finding, recovery=recovery)
        self.assertEqual(policy.resolve_finding(finding, resolution, self.context)["status"], "corrected")

    def test_evidence_gap_acceptance_only_for_allowed_design(self):
        for claim_kind, allowed, succeeds in (("result", True, False), ("design", False, False),
                                              ("design", True, True)):
            finding = self.finding("evidence_gap", claim_kind=claim_kind, allow_assumption=allowed)
            resolution = self.resolution(finding, "accept", authorization=self.authorization(finding["id"]))
            if succeeds:
                self.assertEqual(policy.resolve_finding(finding, resolution, self.context)["status"],
                                 "accepted_assumption")
            else:
                with self.assertRaises(V3Error):
                    policy.resolve_finding(finding, resolution, self.context)

    def test_submitted_finding_status_is_not_resolution(self):
        self.context["findings"] = [self.finding(status="corrected")]
        self.reviewed()
        self.evaluate("E3", "BLOCK")

    def test_e5_individual_acceptance_and_rollback_never_washes_failure(self):
        self.item["acceptance"] = [{"id": "acceptance-a", "target": "target-a"},
                                    {"id": "acceptance-b", "target": "target-b"}]
        results = [{"id": "acceptance-a", "passed": True, "reason": "reason-a"},
                   {"id": "acceptance-b", "passed": True, "reason": "reason-b"}]
        self.reviewed(acceptance_results=results, limitations=["limit-a"])
        self.evaluate("E5", "PASS")
        results[1]["passed"] = False
        self.item["rollback_target"] = "target-c"
        self.reviewed(acceptance_results=results, limitations=[])
        self.evaluate("E5", "BLOCK")
        results.pop()
        self.reviewed(acceptance_results=results, limitations=[])
        self.evaluate("E5", "BLOCK")
        self.item["acceptance"] = []
        self.reviewed(acceptance_results=[], limitations=[])
        self.evaluate("E5", "BLOCK")

    def test_e2_rejects_fake_missing_or_unfinished_run_and_wrong_manifest(self):
        for mode in ("missing", "unfinished", "task", "source", "hash", "inputs"):
            with self.subTest(mode=mode):
                self.measurement()
                run = self.context["runs"][0]
                if mode == "missing":
                    self.context["runs"] = []
                elif mode == "unfinished":
                    run["status"] = "RUNNING"
                elif mode == "task":
                    run["task_id"] = "task-b"
                elif mode == "source":
                    run["outputs"][0]["path"] = str(self.root / "other.json")
                    run["output_hash"] = canonical_hash(run["outputs"])
                elif mode == "hash":
                    run["output_hash"] = "0" * 64
                else:
                    run["inputs"][0]["sha256"] = "0" * 64
                    run["input_hash"] = canonical_hash(run["inputs"])
                self.evaluate("E2", "BLOCK")

    def test_e2_predeclared_plan_does_not_need_future_output_or_value(self):
        self.measurement()
        for key in ("value", "input_hash", "output_hash"):
            self.item["measurement"].pop(key)
        before = copy.deepcopy(self.item)
        self.evaluate("E2", "PASS")
        self.assertEqual(self.item, before)
        self.records[0]["value"] = 99999
        self.evaluate("E2", "BLOCK")

    def test_e4_rejects_inline_proof_and_uses_real_plan_subject_hashes(self):
        from evidence_v3.planning import hash_plan
        self.independent_check(self.proof())
        plan = {"items": [copy.deepcopy(self.item)]}
        self.context["plan_hash"] = hash_plan(plan)
        self.context["subject_hash"] = canonical_hash({"intent": {}, "plan": plan})
        self.context["proofs"][0].update(self.binding())
        self.evaluate("E4", "PASS")
        self.item["checks"][0]["proof"] = self.proof()
        self.evaluate("E4", "BLOCK")

    def test_blocked_original_ai_review_survives_correction_or_acceptance(self):
        for action in ("correct", "accept"):
            with self.subTest(action=action):
                self.context["authorizations"] = []
                finding = self.finding()
                self.context["findings"] = [finding]
                self.reviewed("BLOCK", finding_ids=[finding["id"]], blocking_basis="findings_only")
                original_review = copy.deepcopy(self.context["review"])
                authorization = self.authorization(finding["id"]) if action == "accept" else None
                finding["resolution"] = self.resolution(finding, action, authorization=authorization)
                result = self.evaluate("E3", "PASS")
                self.assertEqual(self.context["review"], original_review)
                self.assertEqual(original_review["conclusions"][0]["verdict"], "BLOCK")
                self.assertTrue(result["limitations"])
                self.context["review"]["conclusions"][0]["finding_ids"] = ["missing-a"]
                self.evaluate("E3", "BLOCK")

    def test_real_service_preserves_block_report_and_passes_legal_correction(self):
        from evidence_v3 import planning, retrieval, service
        def synthetic_human_verifier(task_id, request, context):
            actor_id = "synthetic-owner"
            request_hash = canonical_hash(request)
            source_id = "synthetic-event-" + request_hash
            return {"actor_id": actor_id, "source_refs": [source_id],
                    "authorization_source": {"kind": "trusted_human_event", "source_id": source_id,
                                             "actor_id": actor_id, "request_hash": request_hash,
                                             "verified": True}}
        task_service = service.TaskService(self.root / "data", human_verifier=synthetic_human_verifier)
        intent = {"action": "design", "goals": [{"id": "goal-a", "text": "goal-a"}],
                  "conditions": {}, "unknowns": [], "source_refs": ["source-a"]}
        initial = task_service.create("task-a", intent)
        bundle = retrieval.search(intent, str(self.root / "data"))
        item = {"id": "item-a", "owner_rule": "E3", "claim": "claim-a", "question": "question-a",
                "rationale": "reason-a", "target_refs": ["goal-a"], "origin_refs": ["source-a"],
                "due_stage": "PLAN", "applicability": {"status": "applicable", "reason": "reason-a",
                                                        "source_refs": ["source-a"]}}
        other_item = dict(item, id="item-b", owner_rule="E1",
                          applicability={"status": "inapplicable", "reason": "reason-b", "source_refs": ["source-a"]})
        items = [item, other_item]
        discovery = {"id": "discovery-a", "status": "success", "source_refs": ["source-a"],
                     "intent_hash": canonical_hash(intent), "retrieval_hash": bundle["bundle_hash"],
                     "items_hash": canonical_hash(items), "goal_coverage": {"goal-a": ["item-a", "item-b"]},
                     "counterexample_review": {"status": "success", "source_refs": ["source-a"],
                                               "reviewed_item_ids": ["item-a", "item-b"], "query_refs": [],
                                               "limitations": ["limit-a"]}, "remaining_unknowns": []}
        plan = planning.prepare_plan(intent, bundle, {"items": items, "discovery_review": discovery})
        state = task_service.set_plan("task-a", plan, initial["revision"])
        self.item, self.records = state["plan"]["items"][0], []
        finding = self.finding()
        self.context = task_service._context(state)
        self.context["run_id"] = None
        self.context["findings"] = [finding]
        self.reviewed("BLOCK", finding_ids=[finding["id"]], blocking_basis="findings_only")
        review = self.context["review"]
        review["item_content_hashes"] = {row["id"]: policy.review_content_hash(row, [], self.context)
                                          for row in state["plan"]["items"]}
        review["item_ids"] = ["item-a", "item-b"]
        review["conclusions"].append({"item_id": "item-b", "verdict": "NOT_APPLICABLE", "reason": "reason-b",
                                       "applicability": "inapplicable", "supported_evidence_refs": []})
        review.update(id="review-a", kind="plan", scope="plan", findings=[finding],
                      covered_items=["item-a", "item-b"], source_refs=["source-a"])
        task_service.record_review("task-a", review, state["revision"])
        task_service.record_authorization("task-a", {"id": "confirm-a", "scope": "plan", "decision": "confirm",
            "actor_kind": "human", "actor_id": "actor-a", "source_refs": ["source-a"], "reason": "reason-a",
            "revoked": False, "subject_hash": state["subject_hash"]}, state["revision"])
        self.assertEqual(task_service.evaluate("task-a")["verdict"], "BLOCK")
        state = task_service.read("task-a")
        finding = state["findings"][0]
        self.context = task_service._context(state)
        resolution = self.resolution(finding)
        task_service.resolve("task-a", finding["id"], resolution, state["revision"])
        result = task_service.evaluate("task-a")
        self.assertEqual(result["verdict"], "PASS", result)
        state = task_service.read("task-a")
        self.assertEqual(state["reviews"][0]["conclusions"][0]["verdict"], "BLOCK")
        self.assertEqual(state["findings"][0]["resolution"]["supersedes"], finding["id"])

    def test_batch_review_hashes_and_conclusion_aliases(self):
        self.reviewed()
        review = self.context["review"]
        review["item_content_hashes"] = {self.item["id"]: review.pop("content_hash")}
        conclusion = review["conclusions"][0]
        conclusion["reasons"] = [conclusion.pop("reason")]
        conclusion["evidence_refs"] = conclusion.pop("supported_evidence_refs")
        self.evaluate("E1", "PASS")
        review["item_content_hashes"][self.item["id"]] = "0" * 64
        self.evaluate("E1", "BLOCK")

    def test_bound_checker_source_failure_stays_infra_not_waivable_judgment(self):
        path = self.root / "source.json"
        path.write_text('{"a":1}', encoding="utf-8")
        proof = self.proof(1, 2)
        proof["operands"][0] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "pointer": "/a"}
        finding = self.finding("deterministic", proof=proof)
        self.assertEqual(policy.classify_finding(finding)["type"], "deterministic")
        path.unlink()
        self.assertEqual(policy.classify_finding(finding)["type"], "infra")
        resolution = self.resolution(finding, "accept", authorization=self.authorization(finding["id"]))
        with self.assertRaises(V3Error):
            policy.resolve_finding(finding, resolution, self.context)

    def test_review_hash_stable_across_service_finding_normalization(self):
        self.context["findings"] = [self.finding("advisory")]
        self.reviewed()
        digest = self.context["review"]["content_hash"]
        self.context["findings"] = [policy.classify_finding(self.context["findings"][0])]
        self.assertEqual(policy.review_content_hash(self.item, self.records, self.context), digest)
        self.evaluate("E3", "PASS")

    def test_failed_review_blocks_otherwise_valid_deterministic_path(self):
        self.item["checks"] = [{"id": "check-a", "proof": self.proof()}]
        self.context["review"] = {"status": "error"}
        self.evaluate("E4", "BLOCK")

    def test_review_adapter_and_service_context_feed_policy_without_translation(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
        import evidence_review
        from evidence_v3 import planning, service
        self.item.update(owner_rule="E5", target_refs=["goal-a"],
                         applicability={"status": "applicable", "rationale": "reason-a"})
        snapshot = dict(self.context, schema_version=3, intent={"goals": [{"id": "goal-a", "text": "goal-a"}]},
                        plan={"items": [copy.deepcopy(self.item)]}, evidence=self.records,
                        dependencies={}, runs=[], decisions={"selected_run_id": self.context["run_id"]})
        snapshot.pop("review", None)
        snapshot["plan_hash"] = planning.hash_plan(snapshot["plan"])
        self.context["plan_hash"] = snapshot["plan_hash"]

        def transport(request):
            citation = {"path": "/intent/goals/0/text", "quote": "goal-a"}
            return {"subject_hash": request["subject_hash"], "plan_hash": request["plan_hash"],
                    "context_hash": request["context_hash"],
                    "item_reviews": [{"item_id": "item-a", "assessment": "supported", "rationale": "reason-a",
                                      "citations": [citation]}],
                    "goal_coverage": [{"goal_id": "goal-a", "item_ids": ["item-a"], "rationale": "reason-a",
                                       "citations": [citation]}],
                    "counterexample_review": {"rationale": "reason-a", "citations": [citation]},
                    "findings": [], "remaining_unknowns": [], "limitations": ["limit-a"],
                    "conclusions": [{"item_id": "item-a", "verdict": "PASS", "reason": "reason-a",
                                     "supported_evidence_refs": ["record-a"], "applicability": "applicable",
                                     "citations": [citation],
                                     "acceptance_results": [{"id": "goal-a", "passed": True, "reason": "reason-a"}]}]}

        artifact = evidence_review.run_review(snapshot, transport=transport)
        self.assertEqual(artifact.get("status"), "success", artifact)
        # Use the real service's context builder; only external model transport
        # is injected. No verdict, content hash or conclusion is manufactured.
        task_service = object.__new__(service.TaskService)
        context = task_service._context(snapshot, artifact)
        context["run_id"] = snapshot["run_id"]
        result = policy.evaluate("E5", self.item, self.records, context)
        self.assertEqual(result["verdict"], "PASS", result)
        self.item["target_refs"] = ["goal-b"]
        self.assertEqual(policy.evaluate("E5", self.item, self.records, context)["verdict"], "BLOCK")

    def test_malformed_and_unknown_inputs_fail_closed(self):
        for args in (("E0", self.item, self.records, self.context),
                     ("E1", None, [], {}), ("E2", self.item, None, self.context),
                     ("E3", self.item, self.records, None)):
            result = policy.evaluate(*args)
            self.assertEqual(result["verdict"], "BLOCK")
            self.assertTrue(result["reasons"])
        self.records.append(copy.deepcopy(self.records[0]))
        self.reviewed()
        self.evaluate("E1", "BLOCK")


class PromotionTests(unittest.TestCase):
    def proposal(self, kind="check_knowledge"):
        proposal = {"id": "proposal-a", "kind": kind, "content": {"claim": "claim-a"},
                    "conditions": {"scope": "scope-a"}, "source_refs": ["projection-a", "projection-b"],
                    "sources": [{"id": "source-a", "source_locator": "origin-a", "provenance_refs": []},
                                {"id": "projection-a", "provenance_refs": ["source-a"]},
                                {"id": "projection-b", "provenance_refs": ["source-a"]}]}
        self.approve(proposal)
        return proposal

    def approve(self, proposal):
        digest = policy.promotion_content_hash(proposal)
        proposal["review"] = {"actor_kind": "human", "actor_id": "actor-a", "event_id": "event-a",
                               "status": "approved", "revoked": False, "content_hash": digest,
                               "reason": "reason-a"}
        proposal["confirmations"] = [{"id": "confirmation-a", "actor_kind": "human", "actor_id": "actor-a",
                                       "event_id": "event-b", "decision": "confirm", "revoked": False,
                                       "content_hash": digest},
                                      {"id": "confirmation-b", "actor_kind": "human", "actor_id": "actor-a",
                                       "event_id": "event-c", "decision": "confirm", "revoked": False,
                                       "content_hash": digest}]

    def test_real_approval_and_source_deduplication(self):
        proposal = self.proposal()
        before = copy.deepcopy(proposal)
        result = policy.validate_promotion(proposal)
        self.assertTrue(result["ok"])
        self.assertEqual(result["independent_sources"], ["origin-a"])
        self.assertEqual(proposal, before)

    def test_human_review_required_and_content_bound(self):
        for target in ("missing", "ai", "revoked", "stale", "scope-change", "source-change"):
            with self.subTest(target=target):
                proposal = self.proposal()
                if target == "missing":
                    proposal.pop("review")
                elif target == "ai":
                    proposal["review"]["actor_kind"] = "ai"
                elif target == "revoked":
                    proposal["review"]["revoked"] = True
                elif target == "stale":
                    proposal["review"]["content_hash"] = "0" * 64
                elif target == "scope-change":
                    proposal["conditions"]["scope"] = "scope-b"
                else:
                    proposal["sources"][0]["source_locator"] = "origin-b"
                with self.assertRaises(V3Error):
                    policy.validate_promotion(proposal)

    def test_two_distinct_human_confirmation_events(self):
        proposal = self.proposal("expert_policy")
        self.assertTrue(policy.validate_promotion(proposal)["ok"])
        for mode in ("one", "same-event", "same-id", "stale", "ai", "revoked"):
            with self.subTest(mode=mode):
                proposal = self.proposal("expert_policy")
                second = proposal["confirmations"][1]
                if mode == "one":
                    proposal["confirmations"].pop()
                elif mode == "same-event":
                    second["event_id"] = proposal["confirmations"][0]["event_id"]
                elif mode == "same-id":
                    second["id"] = proposal["confirmations"][0]["id"]
                elif mode == "stale":
                    second["content_hash"] = "0" * 64
                elif mode == "ai":
                    second["actor_kind"] = "ai"
                else:
                    second["revoked"] = True
                with self.assertRaises(V3Error):
                    policy.validate_promotion(proposal)

    def test_circular_missing_and_self_provenance_rejected_even_when_approved(self):
        for mode in ("cycle", "self", "missing", "no-origin", "duplicate-id"):
            with self.subTest(mode=mode):
                proposal = self.proposal()
                if mode == "cycle":
                    proposal["sources"][0]["provenance_refs"] = ["projection-a"]
                elif mode == "self":
                    proposal["sources"].append({"id": proposal["id"], "provenance_refs": [],
                                                 "source_locator": "origin-a"})
                elif mode == "missing":
                    proposal["sources"][1]["provenance_refs"] = ["missing-a"]
                elif mode == "no-origin":
                    proposal["sources"][0].pop("source_locator")
                else:
                    proposal["sources"].append(copy.deepcopy(proposal["sources"][0]))
                self.approve(proposal)
                with self.assertRaises(V3Error):
                    policy.validate_promotion(proposal)


if __name__ == "__main__":
    unittest.main()
