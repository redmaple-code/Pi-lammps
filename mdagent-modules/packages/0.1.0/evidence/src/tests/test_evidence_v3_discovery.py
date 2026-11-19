"""Anonymous tests for bounded evidence discovery; no network or writes."""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "evolution")]

import evidence_discovery as discovery
from evidence_v3.contracts import canonical_hash


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name) / "data"
        self.data_root.mkdir()
        self.sentinel = self.data_root / "sentinel.json"
        self.sentinel.write_text("unchanged\n", encoding="utf-8")
        self.calls = []

    def _intent(self, prompt):
        request = prompt["context"]
        quote = request["request"]
        citation = {"path": "/request", "quote": quote}
        return {"context_hash": prompt["context_hash"], "intent": {
            "action": "design",
            "goals": [{"id": "goal-a", "text": "Compare anonymous outcomes",
                       "source_refs": [citation]}],
            "conditions": {}, "unknowns": [], "source_refs": [citation]}}

    def _bundle(self, intent, limit=100):
        queries = []
        for value in intent.get("queries", []):
            if isinstance(value, str):
                text, purpose, origin = value, "expanded", "intent"
            else:
                text = value["text"]
                purpose = value.get("purpose", "expanded")
                origin = value.get("origin_ref", "intent")
            if not any(row["text"] == text and row["purpose"] == purpose for row in queries):
                queries.append({"id": "query-" + str(len(queries) + 1), "text": text,
                                "purpose": purpose, "origin_ref": origin})
        if not queries:
            queries = [{"id": "query-1", "text": "Compare anonymous outcomes",
                        "purpose": "goal", "origin_ref": "goal-a"}]
        bundle = {"schema_version": 3, "intent_hash": canonical_hash(intent),
                  "queries": queries,
                  "counterexample_queries": [row["id"] for row in queries
                                              if row["purpose"] == "counterexample"],
                  "health": [{"channel": "anonymous", "source": "memory:anonymous", "status": "empty"}],
                  "manifest": [], "candidates": [],
                  "exclusions": [{"id": "anonymous-excluded", "reason": "not-applicable",
                                  "source": "memory:anonymous"}],
                  "truncation": {"limit": limit, "total_matches": 0, "omitted_ids": []},
                  "limitations": ["lexical-recall-only", "anonymous-fixture"], "status": "empty"}
        bundle["bundle_hash"] = canonical_hash(bundle)
        return bundle

    def _proposal(self, prompt, *, unknown=False):
        context = prompt["context"]
        intent = context["intent"]
        bundle = context["retrieval"]
        citation = {"path": "/intent/goals/0/text",
                    "quote": intent["goals"][0]["text"]}
        item = {"id": "check-a", "question": "Does the anonymous goal have a supported basis?",
                "claim": "The anonymous goal has a supported basis", "target_refs": ["goal-a"],
                "owner_rule": "E1", "due_stage": "PLAN",
                "origin_refs": ["request:" + intent["source_context_hash"] + "/request"],
                "rationale": "Derived from the supplied goal",
                "applicability": {"status": "applicable", "reason": "Goal binding",
                                   "source_refs": ["request:" + intent["source_context_hash"] + "/request"]}}
        review = {"goal_coverage": {"goal-a": ["check-a"]},
                  "goal_coverage_rationale": "The goal is mapped to the derived check",
                  "counterexample_review": {
                      "rationale": "An independent failure path was considered",
                      "reviewed_item_ids": ["check-a"],
                      "query_refs": [bundle["counterexample_queries"][0]],
                      "limitations": [], "citations": [citation]},
                  "remaining_unknowns": ([{"id": "unknown-a", "text": "Need an alternate explanation",
                                           "due_stage": "PLAN", "blocking": True,
                                           "source_refs": ["request:" + intent["source_context_hash"] + "/request"]}]
                                         if unknown else [])}
        proposal = {"items": [item], "discovery_review": review, "citations": [citation]}
        return {"context_hash": prompt["context_hash"], "proposal": proposal,
                "citations": [citation]}

    def _transport(self, *, unknown_rounds=0, fail_operation=None):
        state = {"proposals": 0}

        def transport(prompt):
            operation = prompt["operation"]
            self.calls.append(copy.deepcopy(prompt))
            if operation == "recognize_intent":
                if fail_operation == operation:
                    raise RuntimeError("anonymous model failure")
                return self._intent(prompt)
            if operation == "propose_checks":
                if fail_operation == operation:
                    raise RuntimeError("anonymous model failure")
                state["proposals"] += 1
                return self._proposal(prompt, unknown=state["proposals"] <= unknown_rounds)
            raise AssertionError(operation)

        return transport

    def _search(self, *, fail=False, reported_error=False):
        def search(intent, data_root, limit=20):
            self.assertEqual(str(self.data_root), data_root)
            self.calls.append({"operation": "retrieval", "intent": copy.deepcopy(intent), "limit": limit})
            if fail:
                raise RuntimeError("anonymous retrieval failure")
            bundle = self._bundle(intent, limit)
            if reported_error:
                bundle["status"] = "error"
                bundle["bundle_hash"] = canonical_hash({k: v for k, v in bundle.items()
                                                          if k != "bundle_hash"})
            return bundle
        return search

    def test_success_retains_round_bundle_citations_and_source_bindings(self):
        transport = self._transport()
        with patch.object(discovery.retrieval, "search", self._search()):
            result = discovery.discover("Compare anonymous outcomes", str(self.data_root), transport,
                                        max_rounds=3, deadline_seconds=5, max_calls=5,
                                        reviewer_id="anonymous-reviewer")
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["complete"])
        self.assertFalse(result["semantic_exhaustion_claimed"])
        self.assertEqual(len(result["rounds"]), 1)
        round_one = result["rounds"][0]
        bundle = round_one["bundle"]
        self.assertEqual(round_one["exclusions"], bundle["exclusions"])
        self.assertEqual(round_one["health"], bundle["health"])
        self.assertEqual(round_one["truncation"], bundle["truncation"])
        self.assertEqual(round_one["limitations"], bundle["limitations"])
        self.assertTrue(round_one["model_citations"])
        counter_queries = [query for query in bundle["queries"]
                           if query["purpose"] == "counterexample"]
        self.assertTrue(counter_queries)
        self.assertTrue(all(query.get("source_binding") for query in counter_queries))
        self.assertTrue(all(query["source_binding"].get("source_refs")
                            for query in counter_queries))
        proposal = result["proposal"]
        self.assertEqual(proposal["retrieval_bundle"], bundle)
        review = proposal["discovery_review"]
        self.assertEqual(review["intent_hash"], bundle["intent_hash"])
        self.assertEqual(review["retrieval_hash"], bundle["bundle_hash"])
        self.assertEqual(review["counterexample_review"]["query_refs"],
                         bundle["counterexample_queries"][:1])
        self.assertEqual(result["budget"]["calls"], 3)  # recognize, retrieve, propose
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "unchanged\n")
        self.assertIn("lexical-retrieval-is-not-semantic-exhaustion", result["limitations"])

    def test_unknowns_expand_counterexample_queries_and_stop_at_round_budget(self):
        transport = self._transport(unknown_rounds=99)
        with patch.object(discovery.retrieval, "search", self._search()):
            result = discovery.discover({"request": "Compare anonymous outcomes"}, str(self.data_root),
                                        transport, max_rounds=2, deadline_seconds=5, max_calls=7)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertFalse(result["complete"])
        self.assertEqual(result["failure"]["code"], "round-budget-exhausted")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["budget"]["rounds_used"], 2)
        self.assertLessEqual(result["budget"]["calls"], 7)
        retrieval_prompts = [call for call in self.calls if call.get("operation") == "retrieval"]
        self.assertEqual(len(retrieval_prompts), 2)
        second_queries = retrieval_prompts[1]["intent"]["queries"]
        self.assertTrue(any(q.get("purpose") == "unknown" for q in second_queries))
        self.assertTrue(any(q.get("purpose") == "counterexample" and
                            "alternate explanation" in q.get("text", "") for q in second_queries))
        self.assertTrue(any(q.get("purpose") == "counterexample" for q in second_queries))
        self.assertTrue(result["remaining_unknowns"])

    def test_model_and_retrieval_errors_are_not_success(self):
        with patch.object(discovery.retrieval, "search", self._search()):
            model_error = discovery.discover("Compare anonymous outcomes", str(self.data_root),
                                             self._transport(fail_operation="propose_checks"),
                                             max_rounds=2, deadline_seconds=5, max_calls=5)
        self.assertEqual(model_error["status"], "blocked", model_error)
        self.assertFalse(model_error["complete"])
        self.assertTrue(model_error["fail_closed"])

        with patch.object(discovery.retrieval, "search", self._search(fail=True)):
            retrieval_error = discovery.discover("Compare anonymous outcomes", str(self.data_root),
                                                 self._transport(), max_rounds=2,
                                                 deadline_seconds=5, max_calls=5)
        self.assertEqual(retrieval_error["status"], "blocked", retrieval_error)
        self.assertFalse(retrieval_error["complete"])
        self.assertTrue(retrieval_error["fail_closed"])

        with patch.object(discovery.retrieval, "search", self._search(reported_error=True)):
            reported_error = discovery.discover("Compare anonymous outcomes", str(self.data_root),
                                                self._transport(), max_rounds=2,
                                                deadline_seconds=5, max_calls=5)
        self.assertEqual(reported_error["status"], "blocked", reported_error)
        self.assertFalse(reported_error["complete"])

    def test_call_budget_stops_before_an_unbudgeted_proposal(self):
        with patch.object(discovery.retrieval, "search", self._search()):
            result = discovery.discover("Compare anonymous outcomes", str(self.data_root),
                                        self._transport(), max_rounds=4,
                                        deadline_seconds=5, max_calls=2)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertFalse(result["complete"])
        self.assertEqual(result["failure"]["code"], "call-budget-exhausted")
        self.assertEqual(result["budget"]["calls"], 2)
        self.assertLessEqual(result["budget"]["calls"], result["budget"]["max_calls"])

    def test_deadline_bounds_a_blocking_injected_transport(self):
        def slow_transport(prompt):
            time.sleep(0.2)
            if prompt["operation"] == "recognize_intent":
                return self._intent(prompt)
            raise AssertionError("proposal must not run after recognition timeout")

        started = time.monotonic()
        result = discovery.discover("Compare anonymous outcomes", str(self.data_root),
                                    slow_transport, max_rounds=2,
                                    deadline_seconds=0.03, max_calls=5)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["failure"]["code"], "deadline-exhausted")
        self.assertLess(elapsed, 0.15)
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "unchanged\n")

    def test_invalid_budget_blocks_without_transport_or_write(self):
        result = discovery.discover("Compare anonymous outcomes", str(self.data_root),
                                    self._transport(), max_rounds=0, deadline_seconds=5)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.calls)
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "unchanged\n")


if __name__ == "__main__":
    unittest.main()
