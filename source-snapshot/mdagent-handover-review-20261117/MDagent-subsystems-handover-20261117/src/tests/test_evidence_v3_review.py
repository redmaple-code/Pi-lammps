"""Anonymous contract tests; no network, credentials or scientific rule seeds."""
import copy
import json
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "evolution")]
import evidence_review as review
from evidence_v3 import planning, policy, retrieval
from evidence_v3.contracts import canonical_hash


def intent():
    return {"action": "design", "goals": [{"id": "g1", "text": "Compare anonymous outcomes"}],
            "conditions": {}, "unknowns": [], "source_refs": ["request:1"]}


def bundle(value):
    with tempfile.TemporaryDirectory() as directory:
        # Isolate the external corpus filesystem boundary while exercising the
        # real retrieval/planning contracts; do not read workstation corpora.
        with patch.object(retrieval, "__file__", str(Path(directory) / "evolution" / "evidence_v3" / "retrieval.py")):
            return retrieval.search(value, directory, knowledge_dirs=[])


def proposal(value, source_bundle, count=1):
    items = [{"id": "q" + str(n), "question": "Does this support the goal?", "claim": "Anonymous claim",
              "target_refs": ["g1"], "owner_rule": "E3", "due_stage": "PLAN", "origin_refs": ["request:1"],
              "rationale": "Task-derived question", "applicability": {"status": "applicable", "reason": "Goal binding",
                                                                         "source_refs": ["request:1"]}}
             for n in range(count)]
    query_ids = [x["id"] for x in source_bundle["queries"] if x["purpose"] == "counterexample"]
    return {"items": items, "discovery_review": {"id": "disc1", "status": "success", "source_refs": ["request:1"],
             "intent_hash": canonical_hash(value), "retrieval_hash": source_bundle["bundle_hash"],
             "items_hash": canonical_hash(items), "goal_coverage": {"g1": [x["id"] for x in items]},
             "counterexample_review": {"status": "success", "source_refs": ["request:1"],
                "reviewed_item_ids": [x["id"] for x in items], "query_refs": query_ids,
                "limitations": ["Only task material reviewed"]}, "remaining_unknowns": []}}


def snapshot(count=1):
    value = intent()
    source_bundle = bundle(value)
    plan = planning.prepare_plan(value, source_bundle, proposal(value, source_bundle, count))
    return {"schema_version": 3, "task_id": "t1", "revision": 1, "subject_hash": canonical_hash("subject"),
            "intent": value, "plan": plan, "evidence": [], "findings": [], "authorizations": [], "decisions": {}}


def good_response(request):
    citation = {"path": "/intent/goals/0/text", "quote": request["context"]["intent"]["goals"][0]["text"]}
    return {"subject_hash": request["subject_hash"], "plan_hash": request["plan_hash"],
            "context_hash": request["context_hash"],
            "item_reviews": [{"item_id": identity, "assessment": "supported", "rationale": "Reviewed goal binding",
                              "citations": [citation]} for identity in request["item_ids"]],
            "goal_coverage": [{"goal_id": "g1", "item_ids": request["item_ids"], "rationale": "Scope reviewed", "citations": [citation]}],
            "counterexample_review": {"rationale": "Alternative explanations considered", "citations": [citation]},
            "findings": [], "reviewed_inputs": [x["path"] for x in request["input_manifest"]],
            "input_reviews": [{"path": row["path"], "rationale": "Actual input examined",
                "citations": [{"path": "/inputs/" + str(n) + "/content", "quote": row["content"]}]}
                for n, row in enumerate(request["context"].get("inputs", []))],
            "conclusions": [{"item_id": identity, "verdict": "PASS", "reason": "Anonymous semantic conclusion",
                             "applicability": "applicable", "supported_evidence_refs": [], "citations": [citation]}
                            for identity in request["item_ids"]],
            "remaining_unknowns": [], "limitations": ["Limited to supplied task scope"]}


class ReviewTests(unittest.TestCase):
    def test_full_dynamic_plan_beyond_old_batch_limit(self):
        task = snapshot(31)
        result = review.run_review(task, good_response)
        self.assertEqual(result.get("status"), "success", result)
        self.assertEqual(len(result["covered_items"]), 31)
        self.assertEqual(result["metrics"]["item_count"], 31)
        self.assertTrue(result["inputs_unbound"])
        self.assertEqual(result["plan_hash"], task["plan"]["plan_hash"])

    def test_omitted_item_cannot_be_success(self):
        def incomplete(request):
            result = good_response(request)
            result["item_reviews"].pop()
            return result
        self.assertTrue(review.run_review(snapshot(25), incomplete)["fail_closed"])

    def test_stale_and_hallucinated_citations_and_proof(self):
        for mutate in (
            lambda x: x.update(subject_hash="old"),
            lambda x: x["item_reviews"][0]["citations"][0].update(path="/missing"),
            lambda x: x["item_reviews"][0]["citations"][0].update(quote="invented quote"),
            lambda x: x["conclusions"][0].update(supported_evidence_refs=["invented"]),
            lambda x: x["findings"].append({"id": "f1", "type": "deterministic", "item_id": "q0", "detail": "AI says so"}),
        ):
            def transport(request):
                result = good_response(request)
                mutate(result)
                return result
            self.assertEqual(review.run_review(snapshot(), transport)["status"], "infra_error")

    def test_input_review_requires_full_manifest_and_all_paths(self):
        task = snapshot()
        task["inputs"] = [{"path": "object.txt", "content": "anonymous content"},
                          {"path": "dependency.txt", "content": "complete dependency"}]
        task["input_dependencies_complete"] = True
        result = review.run_review(task, good_response, "input")
        self.assertEqual(result.get("status"), "success", result)
        self.assertEqual(len(result["input_manifest"]), 2)
        for mutation in ("partial", "bad_hash", "closure", "no_content"):
            broken = copy.deepcopy(task)
            if mutation == "partial":
                broken["inputs"][0]["truncated"] = True
            elif mutation == "bad_hash":
                broken["inputs"][0]["sha256"] = "wrong"
            elif mutation == "closure":
                broken["input_dependencies_complete"] = False
            else:
                broken["inputs"][0].pop("content")
            self.assertTrue(review.run_review(broken, good_response, "input")["fail_closed"])
        def missing_path(request):
            result = good_response(request)
            result["reviewed_inputs"].pop()
            return result
        self.assertTrue(review.run_review(task, missing_path, "input")["fail_closed"])

    def test_missing_credentials_uses_real_transport_boundary_without_network(self):
        import model_research
        with patch.object(model_research, "_providers", return_value=[]), patch("urllib.request.urlopen") as network:
            result = review.run_review(snapshot())
        self.assertEqual(result["status"], "infra_error")
        network.assert_not_called()

    def test_exception_does_not_disclose_transport_secrets(self):
        def transport(request):
            raise RuntimeError("credential-value-must-not-escape")
        result = review.run_review(snapshot(), transport)
        self.assertNotIn("credential-value", str(result))
        self.assertTrue(result["fail_closed"])

    def test_real_policy_consumes_review(self):
        task = snapshot()
        artifact = review.run_review(task, good_response)
        context = {k: task[k] for k in ("task_id", "revision", "subject_hash", "findings", "authorizations")}
        context.update(plan_hash=task["plan"]["plan_hash"], review=artifact)
        result = policy.evaluate("E3", task["plan"]["items"][0], [], context)
        self.assertEqual(result["verdict"], "PASS", result)
        context["subject_hash"] = canonical_hash("changed")
        self.assertEqual(policy.evaluate("E3", task["plan"]["items"][0], [], context)["verdict"], "BLOCK")

    def test_intent_is_model_extraction_with_real_citations(self):
        request = {"request": "Compare anonymous outcomes"}
        def transport(prompt):
            self.assertEqual(prompt["operation"], "recognize_intent")
            value = intent()
            refs = [{"path": "/request", "quote": request["request"]}]
            value["source_refs"] = refs
            value["goals"][0]["source_refs"] = refs
            return {"context_hash": prompt["context_hash"], "intent": value}
        result = review.recognize_intent(request, transport)
        self.assertEqual(result.get("action"), "design", result)
        planning.validate_intent(result)
        self.assertTrue(result["source_refs"][0].startswith("request:"))

    def test_proposal_real_planner_and_hashes(self):
        value = intent()
        source_bundle = bundle(value)
        def transport(prompt):
            draft = proposal(value, source_bundle, 27)
            discovery = draft["discovery_review"]
            for field in ("status", "intent_hash", "retrieval_hash", "items_hash", "id"):
                discovery.pop(field)
            discovery["goal_coverage_rationale"] = "Each goal mapped to derived obligations"
            discovery["counterexample_review"]["rationale"] = "Failure paths considered"
            citation = {"path": "/intent/goals/0/text", "quote": value["goals"][0]["text"]}
            discovery["counterexample_review"]["citations"] = [citation]
            return {"context_hash": prompt["context_hash"], "proposal": draft, "citations": [citation]}
        result = review.propose_checks(value, source_bundle, transport)
        self.assertNotIn("error", result, result)
        plan = planning.prepare_plan(value, source_bundle, result)
        self.assertEqual(len(plan["items"]), 27)
        self.assertEqual(result["discovery_review"]["items_hash"], canonical_hash(result["items"]))
        self.assertEqual(result["retrieval_bundle"], source_bundle)

    def test_confirmation_comes_from_current_human_record(self):
        task = snapshot()
        self.assertFalse(review.run_review(task, good_response)["confirmed"])
        task["confirmation"] = {"id": "confirm1", "scope": "plan", "actor_kind": "human", "actor_id": "owner1",
            "decision": "confirm", "subject_hash": task["subject_hash"], "reason": "Approved exact plan",
            "source_refs": ["user:event1"], "revoked": False}
        result = review.run_review(task, good_response)
        self.assertTrue(result["confirmed"])
        task["confirmation"]["subject_hash"] = canonical_hash("old")
        self.assertFalse(review.run_review(task, good_response)["confirmed"])

    def test_validation_does_not_require_parent_plan_or_production_clearance(self):
        import hashlib
        from evidence_v3.execution import normalize_spec
        with tempfile.TemporaryDirectory() as directory:
            content = "anonymous probe input"
            digest = hashlib.sha256(content.encode()).hexdigest()
            spec = normalize_spec({"kind": "validation", "command": ["probe", "object.txt"],
                "inputs": [{"path": "object.txt", "sha256": digest}], "cwd": directory,
                "limits": {"timeout": 10, "stop": "bounded question"}, "question_id": "q1", "review_id": "probe-review1"})
            task = {"schema_version": 3, "task_id": "t1", "revision": 1, "subject_hash": canonical_hash("subject"),
                    "intent": intent(), "validation_spec": spec, "inputs": [{"path": "object.txt", "content": content, "sha256": digest}],
                    "input_dependencies_complete": True,
                    "authorizations": [{"id": "auth1", "scope": "validation", "decision": "allow", "actor_kind": "human",
                        "actor_id": "owner1", "source_refs": ["user:event1"], "reason": "Bounded probe approved",
                        "subject_hash": canonical_hash("subject"), "spec_hash": canonical_hash(spec), "revoked": False}]}
            def transport(request):
                citation = {"path": "/validation_spec/question_id", "quote": "q1"}
                return {"context_hash": request["context_hash"], "subject_hash": request["subject_hash"],
                    "spec_hash": request["spec_hash"], "question_review": {"rationale": "Question is independent", "citations": [citation]},
                    "resource_review": {"rationale": "Limits reviewed", "citations": [citation]},
                    "conclusion": {"verdict": "PASS", "reason": "Limited probe viable", "citations": [citation]},
                    "reviewed_inputs": ["object.txt"],
                    "input_reviews": [{"path": "object.txt", "rationale": "Probe input inspected",
                        "citations": [{"path": "/inputs/0/content", "quote": content}]}],
                    "findings": [], "limitations": ["Production remains unreviewed"]}
            result = review.run_review(task, transport, "validation")
            self.assertEqual(result.get("status"), "success", result)
            self.assertEqual(result["id"], spec["review_id"])
            self.assertEqual(result["authorization_refs"], ["auth1"])
            self.assertEqual(result["spec_hash"], canonical_hash(spec))
            for mutation in (lambda x: x["authorizations"][0].update(revoked=True),
                             lambda x: x["authorizations"][0].update(actor_kind="AI"),
                             lambda x: x["authorizations"][0].update(spec_hash="old"),
                             lambda x: x["validation_spec"].update(review_id="different"),
                             lambda x: x.update(input_dependencies_complete=False)):
                bad = copy.deepcopy(task)
                mutation(bad)
                self.assertTrue(review.run_review(bad, transport, "validation")["fail_closed"])

    def test_service_records_and_policy_consumes_exact_review(self):
        from evidence_v3.service import TaskService
        value = intent()
        source_bundle = bundle(value)
        with tempfile.TemporaryDirectory() as directory:
            service = TaskService(directory)
            task = service.create("t1", value)
            draft = proposal(value, source_bundle)
            draft["retrieval_bundle"] = source_bundle
            task = service.set_plan("t1", draft, task["revision"])
            artifact = review.run_review(task, good_response)
            self.assertEqual(artifact.get("status"), "success", artifact)
            task = service.record_review("t1", artifact, task["revision"])
            result = policy.evaluate("E3", task["plan"]["items"][0], task["evidence"], service._context(task, task["reviews"][-1]))
            self.assertEqual(result["verdict"], "PASS", result)

    def test_resolution_review_consumed_by_policy_and_service(self):
        from evidence_v3.service import TaskService
        task = snapshot()
        finding = policy.classify_finding({"id": "f1", "item_id": "q0", "type": "judgment", "detail": "Anonymous concern"})
        task["findings"] = [finding]
        def transport(request):
            return {"context_hash": request["context_hash"], "content_hash": request["content_hash"],
                    "decision": "correct", "reason": "Counterevidence corrects interpretation",
                    "citations": [{"path": "/finding/detail", "quote": "Anonymous concern"}],
                    "limitations": ["Correction limited to current task"]}
        result = review.review_resolution(task, "f1", {"id": "resolution1", "action": "correct", "reason": "Reconsider interpretation"}, transport)
        self.assertNotIn("error", result, result)
        context = {k: task[k] for k in ("task_id", "revision", "subject_hash", "findings", "authorizations")}
        context["plan_hash"] = task["plan"]["plan_hash"]
        self.assertEqual(policy.resolve_finding(finding, result, context)["status"], "corrected")
        def synthetic_human_verifier(task_id, request, context):
            actor_id = "synthetic-owner"
            request_hash = canonical_hash(request)
            source_id = "synthetic-event-" + request_hash
            return {"actor_id": actor_id, "source_refs": [source_id],
                    "authorization_source": {"kind": "trusted_human_event", "source_id": source_id,
                                             "actor_id": actor_id, "request_hash": request_hash,
                                             "verified": True}}
        with tempfile.TemporaryDirectory() as directory:
            service = TaskService(directory, human_verifier=synthetic_human_verifier)
            value = intent()
            source_bundle = bundle(value)
            state = service.create("t1", value)
            draft = proposal(value, source_bundle, 2)
            draft["items"][1].update(owner_rule="E1", allow_assumption=True, claim_kind="design", claim_id="claim1")
            draft["discovery_review"]["items_hash"] = canonical_hash(draft["items"])
            draft["retrieval_bundle"] = source_bundle
            state = service.set_plan("t1", draft, state["revision"])
            state = service.record_authorization("t1", {"id": "assumption1", "scope": "q1", "decision": "accept_assumption",
                "actor_kind": "human", "actor_id": "owner1", "source_refs": ["user:event0"], "reason": "Explicit design uncertainty",
                "subject_hash": state["subject_hash"], "revoked": False}, state["revision"])
            state = service.record_authorization("t1", {"id": "confirm1", "scope": "plan", "decision": "confirm",
                "actor_kind": "human", "actor_id": "owner1", "source_refs": ["user:event1"], "reason": "Exact plan confirmed",
                "subject_hash": state["subject_hash"], "revoked": False}, state["revision"])
            def objection(request):
                response = good_response(request)
                response["findings"] = [dict(finding, citations=[{"path": "/intent/goals/0/text", "quote": value["goals"][0]["text"]}])]
                response["item_reviews"][0]["assessment"] = "opposed"
                response["conclusions"][0].update(verdict="BLOCK", finding_ids=["f1"], blocking_basis="findings_only")
                return response
            artifact = review.run_review(state, objection)
            state = service.record_review("t1", artifact, state["revision"])
            blocked = policy.evaluate("E3", state["plan"]["items"][0], state["evidence"], service._context(state, state["reviews"][-1]))
            self.assertEqual(blocked["verdict"], "BLOCK")
            resolution = review.review_resolution(state, "f1", {"id": "resolution2", "action": "correct", "reason": "Review counterargument"}, transport)
            self.assertNotIn("error", resolution, resolution)
            state = service.resolve("t1", "f1", resolution, state["revision"])
            self.assertEqual(state["findings"][0]["status"], "corrected")
            self.assertEqual(state["reviews"][-1]["conclusions"][0]["verdict"], "BLOCK")
            resolved = policy.evaluate("E3", state["plan"]["items"][0], state["evidence"], service._context(state, state["reviews"][-1]))
            self.assertEqual(resolved["verdict"], "PASS", resolved)
            receipt = service.evaluate("t1")
            self.assertEqual(receipt["verdict"], "PASS", receipt)

    def test_resolution_cannot_grant_accept_or_repair_facility_by_assertion(self):
        task = snapshot()
        task["findings"] = [policy.classify_finding({"id": "f1", "type": "infra", "detail": "Missing source"})]
        calls = []
        def transport(request):
            calls.append(request)
            return {"context_hash": request["context_hash"], "content_hash": request["content_hash"],
                    "decision": "correct", "reason": "Model agreement is insufficient",
                    "citations": [{"path": "/finding/detail", "quote": "Missing source"}], "limitations": []}
        result = review.review_resolution(task, "f1", {"id": "resolution1", "action": "correct", "reason": "Trust model"}, transport)
        self.assertTrue(result["fail_closed"])
        result = review.review_resolution(task, "f1", {"id": "resolution1", "action": "accept", "reason": "Trust model"}, transport)
        self.assertTrue(result["fail_closed"])
        self.assertEqual(len(calls), 1)

    def test_parse_errors_and_nonfinite_rejected(self):
        for response in ('{"x":1,"x":2}', '{"x":NaN}', 'not json', []):
            self.assertTrue(review.run_review(snapshot(), lambda _: response)["fail_closed"])


def segment_citations(segment):
    def leaves(value, path):
        if isinstance(value, dict):
            return [citation for key, child in value.items()
                    for citation in leaves(child, path + "/" + review._escape(key))]
        if isinstance(value, list):
            return [citation for index, child in enumerate(value)
                    for citation in leaves(child, path + "/" + str(index))]
        text = value if isinstance(value, str) else json.dumps(value)
        return [{"path": path, "quote": text[:32]}] if text.strip() else []
    if segment["encoding"] == "json":
        return leaves(json.loads(segment["text"]), segment["path"])[:1]
    return [{"path": segment["path"], "quote": segment["text"][:32]}] if segment["text"].strip() else []


class BatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.task = snapshot()
        self.task["inputs"] = [{"path": "anonymous.txt", "content": "片段甲乙" * 8000}]
        self.task["input_dependencies_complete"] = True
        self.request = review.prepare_review(self.task, "input")
        self.budget = {"max_bytes": 60000, "max_tokens": 60000, "max_batches": 100,
                       "max_calls": 100, "max_attempts": 1, "deadline_seconds": 30}
        self.calls = []

    def transport(self, prompt):
        self.calls.append(prompt)
        self.assertLessEqual(len(review._serialized(prompt)), self.budget["max_bytes"])
        if prompt["phase"] == "fragments":
            rows = []
            for part in prompt["fragments"]:
                entries = [{"segment_id": segment["id"], "summary": "Source interpreted",
                            "rationale": "Claims and limits considered", "citations": segment_citations(segment)}
                           for segment in part["segments"]]
                rows.append({"fragment_id": part["id"], "summary": "Assigned sources considered",
                             "rationale": "Relations checked", "segment_reviews": entries,
                             "citations": [c for entry in entries for c in entry["citations"]]})
            return {"request_hash": prompt["request_hash"], "fragment_reviews": rows,
                    "batch_summary": {"summary": "All assigned sources reviewed", "rationale": "Dependencies considered"}}
        if prompt["phase"] == "global_pair":
            return {"request_hash": prompt["request_hash"],
                    "reviewed_fragment_ids": [part["id"] for part in prompt["fragments"]],
                    "verdict": "PASS", "rationale": "Independent consistency comparison",
                    "citations": [citation for part in prompt["fragments"] for segment in part["segments"]
                                  for citation in segment_citations(segment)], "findings": []}
        self.assertEqual(prompt["phase"], "synthesis")
        request = dict(self.request, item_ids=prompt["item_ids"], goal_ids=prompt["goal_ids"], input_manifest=[])
        response = good_response(request)
        citation = next(c for row in prompt["local_reviews"] for entry in row["segment_reviews"]
                        for c in entry["citations"] if c["path"].startswith("/intent/goals/"))
        for row in response["item_reviews"] + response["goal_coverage"] + response["conclusions"]:
            row["citations"] = [copy.deepcopy(citation)]
        response["counterexample_review"]["citations"] = [copy.deepcopy(citation)]
        response.update(request_hash=prompt["request_hash"], input_reviews=[], reviewed_inputs=[])
        response["goal_coverage"] = [row for row in response["goal_coverage"] if row["goal_id"] in prompt["goal_ids"]]
        findings = [row for row in prompt["global_findings"] if row["item_id"] in prompt["item_ids"]]
        response["findings"] = copy.deepcopy(findings)
        if any(row["type"] != "advisory" for row in findings):
            response["item_reviews"][0]["assessment"] = "opposed"
            response["conclusions"][0]["verdict"] = "BLOCK"
        return response

    def run_batch(self, transport=None, **kwargs):
        return review.run_review(self.task, transport or self.transport, "input", budget=self.budget, **kwargs)

    def test_full_multibyte_coverage_synthesis_input_and_policy(self):
        result = self.run_batch()
        self.assertEqual(result.get("status"), "success", result)
        parts = result["batch_review"]["fragments"]
        self.assertGreater(len(parts), 1)
        self.assertEqual(result["batch_review"]["global_pair_count"], len(parts) * (len(parts) - 1) // 2)
        segments = [segment for part in parts for segment in part["segments"]
                    if segment["path"] == "/inputs/0/content"]
        self.assertGreater(len(segments), 1)
        self.assertEqual(sum(s["end_byte"] - s["start_byte"] for s in segments),
                         len(self.task["inputs"][0]["content"].encode("utf-8")))
        self.assertEqual(len(result["input_reviews"][0]["citations"]), len(segments))
        self.assertNotIn('"text":', json.dumps(parts))
        context = {key: self.task[key] for key in ("task_id", "revision", "subject_hash", "findings", "authorizations")}
        context.update(plan_hash=self.task["plan"]["plan_hash"], review=result)
        self.assertEqual(policy.evaluate("E3", self.task["plan"]["items"][0], [], context)["verdict"], "PASS")

    def test_missing_segment_and_unseen_synthesis_quote_fail_closed(self):
        for mutation in ("missing_segment", "unseen_quote"):
            with self.subTest(mutation=mutation):
                self.calls = []
                def transport(prompt):
                    response = self.transport(prompt)
                    if mutation == "missing_segment" and prompt["phase"] == "fragments":
                        response["fragment_reviews"][0]["segment_reviews"].pop()
                    if mutation == "unseen_quote" and prompt["phase"] == "synthesis":
                        response["item_reviews"][0]["citations"] = [{"path": "/inputs/0/content",
                            "quote": self.task["inputs"][0]["content"][:100]}]
                    return response
                result = self.run_batch(transport)
                self.assertTrue(result["fail_closed"], result)
                phases = {prompt["phase"] for prompt in self.calls}
                self.assertIn("fragments", phases)
                if mutation == "unseen_quote":
                    self.assertIn("synthesis", phases)
                else:
                    self.assertNotIn("global_pair", phases)

    def test_exact_coverage_rejects_gaps_overlap_payload_and_extra_pointer(self):
        context = {"source": "甲乙丙丁"}
        parts = [review._fragment([review._segment("/source", context["source"], 0, 6),
                                   review._segment("/source", context["source"], 6, 12)])]
        review._verify_fragment_coverage(context, parts)
        for mutation in ("gap", "overlap", "payload", "extra", "duplicate"):
            broken = copy.deepcopy(parts)
            segments = broken[0]["segments"]
            if mutation == "gap":
                segments.pop(0)
            elif mutation == "overlap":
                segments[1] = review._segment("/source", context["source"], 3, 12)
            elif mutation == "payload":
                segments[0]["text"] = "伪造"
                segments[0]["sha256"] = review.hashlib.sha256(segments[0]["text"].encode()).hexdigest()
            elif mutation == "extra":
                segments.append(review._segment("/other", "extra"))
            else:
                segments.append(copy.deepcopy(segments[0]))
            broken[0] = review._fragment(segments)
            with self.subTest(mutation=mutation), self.assertRaises(review.ReviewError):
                review._verify_fragment_coverage(context, broken)

    def test_structured_leaf_escaping_and_empty_units(self):
        context = {"rows": [{"text": 'quoted "value"\nnext'}], "empty": [], "blank": ""}
        parts = [review._fragment([review._segment(path, value) for path, value in review._leaves(context)])]
        prompt = review._batch_request({}, "fragments", parts)
        response = self.transport(prompt)
        review._verify_fragment_coverage(context, parts)
        review._review_parts(response, prompt, context)

    def test_structured_record_sized_by_actual_pair_budget(self):
        value = {"rows": [{"record": "x" * 5000}]}
        settings = review._budget(dict(self.budget, max_bytes=30000, max_tokens=30000))
        header = review._batch_header(self.request)
        parts = review._split_context(value, header, settings)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["segments"][0]["encoding"], "json")
        review._verify_fragment_coverage(value, parts)

    def conflict_transport(self, prompt):
        response = self.transport(prompt)
        if prompt["phase"] == "global_pair":
            parts = prompt["fragments"]
            left = [s for s in parts[0]["segments"] if s["path"] == "/inputs/0/content"]
            right = [s for s in parts[1]["segments"] if s["path"] == "/inputs/0/content"]
            if left and right:
                response.update(verdict="BLOCK", findings=[{"id": "conflict-" + prompt["request_hash"],
                    "item_id": "q0", "type": "judgment", "detail": "Distant source relation unresolved",
                    "citations": segment_citations(left[0]) + segment_citations(right[0])}])
        return response

    def test_independent_distant_conflicts_survive_synthesis(self):
        result = self.run_batch(self.conflict_transport)
        self.assertEqual(result.get("status"), "success", result)
        self.assertTrue(result["batch_review"]["global_findings"])
        self.assertEqual(result["conclusions"][0]["verdict"], "BLOCK")
        self.assertTrue(result["findings"])

    def test_global_issue_cannot_be_omitted_or_passed(self):
        for mutation in ("omit", "pass"):
            self.calls = []
            def transport(prompt):
                response = self.conflict_transport(prompt)
                if prompt["phase"] == "synthesis":
                    if mutation == "omit":
                        response["findings"] = []
                    else:
                        response["item_reviews"][0]["assessment"] = "supported"
                        response["conclusions"][0]["verdict"] = "PASS"
                return response
            with self.subTest(mutation=mutation):
                result = self.run_batch(transport)
                self.assertTrue(result["fail_closed"], result)
                self.assertIn("synthesis", {prompt["phase"] for prompt in self.calls})

    def test_cache_revalidation_corruption_and_identity(self):
        from evidence_v3.contracts import atomic_json, read_json
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_batch(artifact_root=directory, reviewer_id="offline-v1")
            self.assertEqual(first.get("status"), "success", first)
            second = self.run_batch(lambda _: self.fail("valid cache should avoid transport"),
                                    artifact_root=directory, reviewer_id="offline-v1")
            self.assertEqual(second.get("status"), "success", second)
            self.assertEqual(second["metrics"]["transport_calls"], 0)
            self.assertEqual(second["metrics"]["cache_hits"], first["metrics"]["batch_count"])
            cache = next(path for path in Path(directory).rglob("*.json")
                         if "response" in read_json(path))
            doc = read_json(cache)
            doc["response"]["request_hash"] = "stale"
            doc["response_hash"] = canonical_hash(doc["response"])
            atomic_json(cache, doc)
            repaired = self.run_batch(artifact_root=directory, reviewer_id="offline-v1")
            self.assertEqual(repaired.get("status"), "success", repaired)
            self.assertEqual(repaired["metrics"]["transport_calls"], 1)
            changed = self.run_batch(artifact_root=directory, reviewer_id="offline-v2")
            self.assertEqual(changed.get("status"), "success", changed)
            self.assertEqual(changed["metrics"]["cache_hits"], 0)

    def test_retry_call_and_batch_limits(self):
        for limit, value in (("max_attempts", 2), ("max_calls", 1), ("max_batches", 1)):
            self.calls = []
            self.budget[limit] = value
            def transport(prompt):
                response = self.transport(prompt)
                return {} if limit == "max_attempts" else response
            result = self.run_batch(transport)
            with self.subTest(limit=limit):
                self.assertTrue(result["fail_closed"], result)
                self.assertEqual(result["metrics"]["transport_calls"], value)
                self.assertEqual(len(self.calls), value)
            self.budget[limit] = {"max_attempts": 1, "max_calls": 100, "max_batches": 100}[limit]

    def test_cache_hit_allowed_after_fresh_call_limit(self):
        settings = review._budget(dict(self.budget, max_calls=1))
        prompt = review._batch_request({}, "fragments", [review._fragment([review._segment("/x", "value")])])
        with tempfile.TemporaryDirectory() as directory:
            session = review._BatchSession(self.request, settings, directory, "offline-v1")
            session.execute(prompt, lambda _: {"request_hash": prompt["request_hash"]},
                            lambda response: review._bound_response(response, prompt))
            session.execute(prompt, lambda _: self.fail("cache hit used transport"),
                            lambda response: review._bound_response(response, prompt))
            self.assertEqual(session.metrics["cache_hits"], 1)
            self.assertEqual(session.metrics["transport_calls"], 1)

    def test_deadline_covers_preparation_validation_and_hung_transport(self):
        clock = [0.0]
        original = review.prepare_review
        def prepare(*args):
            request = original(*args)
            clock[0] = 31.0
            return request
        with patch.object(review, "prepare_review", side_effect=prepare), \
                patch.object(review.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(self.run_batch()["fail_closed"])
        self.assertFalse(self.calls)
        settings = review._budget(dict(self.budget, deadline_seconds=1))
        prompt = review._batch_request({}, "test", [])
        clock[0] = 0.0
        with patch.object(review.time, "monotonic", side_effect=lambda: clock[0]):
            session = review._BatchSession(self.request, settings, None, None)
            def validate(_):
                clock[0] = 2.0
            with self.assertRaises(review.ReviewError):
                session.execute(prompt, lambda _: {}, validate)
        event = threading.Event()
        try:
            started = time.monotonic()
            with self.assertRaises(review.ReviewError):
                review._call({}, lambda _: event.wait(2), timeout_seconds=0.02)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            event.set()

    def test_oversized_header_and_synthesis_and_artifact_failure(self):
        self.budget["max_bytes"] = 1000
        self.assertTrue(self.run_batch()["fail_closed"])
        self.assertFalse(self.calls)
        self.budget["max_bytes"] = 60000
        def large_summary(prompt):
            response = self.transport(prompt)
            if prompt["phase"] == "fragments":
                response["fragment_reviews"][0]["summary"] = "x" * 80000
            return response
        self.assertTrue(self.run_batch(large_summary)["fail_closed"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("evidence_v3.contracts.atomic_json", side_effect=OSError("write failed")):
            result = self.run_batch(artifact_root=directory, reviewer_id="offline-v1")
        self.assertTrue(result["fail_closed"])
        self.assertTrue(result["artifact_write_failed"])


if __name__ == "__main__":
    unittest.main()
