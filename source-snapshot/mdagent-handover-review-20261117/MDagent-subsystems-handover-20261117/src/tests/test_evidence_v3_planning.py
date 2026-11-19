"""Anonymous structural examples; no domain checklist or runtime-store writes."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evolution"))
from evidence_v3 import planning, retrieval
from evidence_v3.contracts import V3Error, canonical_hash


def intent():
    return {"action": "design", "goals": [{"id": "goal-a", "text": "object alpha 可达性"}],
            "conditions": {"mode": "a"}, "unknowns": [], "source_refs": ["request:1"],
            "queries": [{"text": "alternative alpha", "purpose": "counterexample"}]}


def bundle(task=None, health=None):
    task = intent() if task is None else task
    result = {"intent_hash": canonical_hash(task), "status": "empty", "queries": [],
              "candidates": [], "health": [] if health is None else health,
              "manifest": [], "exclusions": [], "limitations": ["empty-library"],
              "truncation": {"limit": 20, "total_matches": 0, "omitted_ids": []}}
    result["bundle_hash"] = canonical_hash(result)
    return result


def item(identity="item-a", owner="E1", due="PLAN", goals=None):
    return {"id": identity, "question": "Is object alpha supported?", "claim": "object alpha is supported",
            "target_refs": ["goal-a"] if goals is None else goals, "owner_rule": owner, "due_stage": due,
            "origin_refs": ["request:1"], "rationale": "Answer the stated goal",
            "applicability": {"status": "applicable", "reason": "Task-derived question",
                              "source_refs": ["request:1"]}}


def proposal(task=None, snapshot=None, items=None):
    task = intent() if task is None else task
    snapshot = bundle(task) if snapshot is None else snapshot
    items = [item()] if items is None else items
    return {"items": items, "discovery_review": {
        "id": "review-a", "status": "success", "source_refs": ["review:1"],
        "intent_hash": canonical_hash(task), "retrieval_hash": snapshot["bundle_hash"],
        "items_hash": canonical_hash(items),
        "goal_coverage": {g["id"]: [i["id"] for i in items if g["id"] in i["target_refs"]] for g in task["goals"]},
        "counterexample_review": {"status": "success", "source_refs": ["review:2"],
                                   "reviewed_item_ids": [i["id"] for i in items], "query_refs": [],
                                   "limitations": ["No additional source was available"]},
        "remaining_unknowns": []}}


def make_plan(task=None, snapshot=None, items=None):
    task = intent() if task is None else task
    snapshot = bundle(task) if snapshot is None else snapshot
    return planning.prepare_plan(task, snapshot, proposal(task, snapshot, items))


def result(plan, identity="item-a", verdict="PASS", owner="E1", subject="subject-a"):
    return {"rule": owner, "item_id": identity, "plan_hash": plan["plan_hash"],
            "subject_hash": subject, "verdict": verdict, "reasons": [], "evidence_refs": [], "limitations": []}


def reviewed_revision(old, new):
    new = copy.deepcopy(new)
    new["reviewed_change"] = {"status": "success", "reason": "A reviewed change in current scope",
                              "source_refs": ["review:3"], "old_plan_hash": old["plan_hash"],
                              "new_content_hash": canonical_hash({k: v for k, v in new.items() if k != "plan_hash"})}
    new["plan_hash"] = planning.hash_plan(new)
    return new


class IntentAndPlanningTests(unittest.TestCase):
    def test_open_intent_is_valid_and_not_inferred_from_words(self):
        task = intent()
        task["action"] = "consult"
        task["goals"][0]["text"] = "production build arbitrary unfamiliar object"
        self.assertEqual(planning.validate_intent(task)["action"], "consult")
        for action in ("design", "validation", "production", "build", "analyze"):
            task["action"] = action
            self.assertEqual(planning.validate_intent(task)["action"], action)

    def test_invalid_intents_rejected(self):
        for key, bad in (("action", "unknown"), ("goals", []), ("conditions", []),
                         ("unknowns", "none"), ("source_refs", []), ("queries", [3])):
            with self.subTest(key=key):
                task = intent()
                task[key] = bad
                with self.assertRaises(V3Error):
                    planning.validate_intent(task)
        task = intent()
        task["conditions"]["x"] = float("nan")
        with self.assertRaises(V3Error):
            planning.validate_intent(task)
        task = intent()
        task["goals"].append(copy.deepcopy(task["goals"][0]))
        with self.assertRaises(V3Error):
            planning.validate_intent(task)

    def test_empty_library_accepts_task_derived_nonempty_plan(self):
        plan = make_plan()
        self.assertEqual(plan["items"][0]["origin_refs"], ["request:1"])
        self.assertEqual(plan["items"][0]["status"], "awaiting_evidence")
        self.assertTrue(planning.coverage(plan, [result(plan)], "PLAN", "subject-a")["ok"])

    def test_empty_plan_and_caller_status_cannot_pass(self):
        task, snapshot = intent(), bundle()
        with self.assertRaises(V3Error):
            planning.prepare_plan(task, snapshot, proposal(items=[]))
        entry = item()
        entry["status"] = "evidenced"
        plan = make_plan(items=[entry])
        self.assertEqual(plan["items"][0]["status"], "awaiting_evidence")
        self.assertFalse(planning.coverage(plan, [], "PLAN", "subject-a")["ok"])
        self.assertFalse(planning.coverage({}, [], "PLAN", "subject-a")["ok"])

    def test_exactly_one_owner_and_stable_id_required(self):
        for key, value in (("owner_rule", ["E1", "E2"]), ("owner_rule", "E6"),
                           ("due_stage", "LATER"), ("id", ""), ("origin_refs", []),
                           ("target_refs", ["other-goal"]), ("applicability", {"status": "applicable"})):
            with self.subTest(key=key, value=value):
                entry = item()
                entry[key] = value
                with self.assertRaises(V3Error):
                    make_plan(items=[entry])
        with self.assertRaises(V3Error):
            make_plan(items=[item(), item()])

    def test_discovery_requires_success_and_current_hashes(self):
        for key, value in (("status", "error"), ("source_refs", []), ("intent_hash", "old"),
                           ("retrieval_hash", "old"), ("items_hash", "old")):
            with self.subTest(key=key):
                spec = proposal()
                spec["discovery_review"][key] = value
                with self.assertRaises(V3Error):
                    planning.prepare_plan(intent(), bundle(), spec)
        snapshot = bundle()
        snapshot["health"].append({"source": "x", "status": "error"})
        with self.assertRaises(V3Error):
            planning.prepare_plan(intent(), snapshot, proposal(snapshot=snapshot))

    def test_goal_backcheck_and_counterexample_coverage(self):
        task = intent()
        task["goals"].append({"id": "goal-b", "text": "alternative goal"})
        snapshot = bundle(task)
        spec = proposal(task, snapshot)
        with self.assertRaises(V3Error):
            planning.prepare_plan(task, snapshot, spec)
        plan = make_plan(task, snapshot, [item(goals=["goal-a", "goal-b"])])
        self.assertEqual(set(plan["discovery_review"]["goal_coverage"]), {"goal-a", "goal-b"})
        spec = proposal()
        spec["discovery_review"]["counterexample_review"]["reviewed_item_ids"] = []
        with self.assertRaises(V3Error):
            planning.prepare_plan(intent(), bundle(), spec)
        spec = proposal()
        spec["discovery_review"]["counterexample_review"]["limitations"] = []
        with self.assertRaises(V3Error):
            planning.prepare_plan(intent(), bundle(), spec)

    def test_remaining_unknowns_stop_only_at_due_stage(self):
        spec = proposal()
        spec["discovery_review"]["remaining_unknowns"] = [{"id": "unknown-a", "text": "Unresolved input",
            "due_stage": "BUILD", "blocking": True, "source_refs": ["request:1"]}]
        plan = planning.prepare_plan(intent(), bundle(), spec)
        self.assertTrue(planning.coverage(plan, [result(plan)], "PLAN", "subject-a")["ok"])
        self.assertFalse(planning.coverage(plan, [result(plan)], "BUILD", "subject-a")["ok"])

    def test_owner_and_both_hash_bindings_required(self):
        plan = make_plan()
        for key, value in (("rule", "E2"), ("plan_hash", "old"), ("subject_hash", "other"), ("item_id", "other")):
            with self.subTest(key=key):
                record = result(plan)
                record[key] = value
                report = planning.coverage(plan, [record], "PLAN", "subject-a")
                self.assertFalse(report["ok"])
                self.assertEqual(report["items"][0]["status"], "missing-result")
        for verdict in ("ACCEPTED_ASSUMPTION", "NOT_APPLICABLE"):
            self.assertTrue(planning.coverage(plan, [result(plan, verdict=verdict)], "PLAN", "subject-a")["ok"])

    def test_conflicts_block_without_last_write_wins(self):
        plan = make_plan()
        records = [result(plan), result(plan, verdict="BLOCK")]
        for values in (records, records[::-1]):
            report = planning.coverage(plan, values, "PLAN", "subject-a")
            self.assertFalse(report["ok"])
            self.assertEqual(report["items"][0]["status"], "CONFLICT")

    def test_later_obligations_do_not_block_earlier_stage(self):
        plan = make_plan(items=[item(), item("item-b", "E5", "ANALYZE")])
        self.assertTrue(planning.coverage(plan, [result(plan)], "PLAN", "subject-a")["ok"])
        self.assertFalse(planning.coverage(plan, [result(plan)], "ANALYZE", "subject-a")["ok"])
        self.assertTrue(planning.coverage(plan, [result(plan), result(plan, "item-b", owner="E5")], "ANALYZE", "subject-a")["ok"])

    def test_twenty_fifth_obligation_not_dropped(self):
        entries = [item("item-" + str(n)) for n in range(25)]
        plan = make_plan(items=entries)
        records = [result(plan, i["id"]) for i in entries[:24]]
        report = planning.coverage(plan, records, "PLAN", "subject-a")
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["items"]), 25)
        self.assertEqual(report["remaining"][0]["id"], "item-24")
        self.assertTrue(planning.coverage(plan, records + [result(plan, "item-24")], "PLAN", "subject-a")["ok"])

    def test_required_unhealthy_source_blocks_even_with_policy_pass(self):
        for status in ("error", "unavailable", "empty", "ok"):
            with self.subTest(status=status):
                snapshot = bundle(health=[{"channel": "x", "source": "source:x", "status": status}])
                entry = item()
                entry["required_sources"] = ["source:x"]
                plan = make_plan(snapshot=snapshot, items=[entry])
                self.assertEqual(planning.coverage(plan, [result(plan)], "PLAN", "subject-a")["ok"], status in ("ok", "empty"))
        plan = make_plan(snapshot=bundle(health=[{"source": "source:x", "status": "error"}]))
        self.assertTrue(planning.coverage(plan, [result(plan)], "PLAN", "subject-a")["ok"])

    def test_structural_failure_is_closed(self):
        plan = make_plan()
        for records in (None, [dict(result(plan), verdict={})]):
            self.assertFalse(planning.coverage(plan, records, "PLAN", "subject-a")["ok"])
        self.assertFalse(planning.coverage(plan, [result(plan)], "bad-stage", "subject-a")["ok"])
        plan["items"][0]["claim"] = "Changed claim"
        self.assertFalse(planning.coverage(plan, [result(plan)], "PLAN", "subject-a")["ok"])
        with patch.object(planning, "_validate_plan", side_effect=RuntimeError("checker failure")):
            self.assertIn("coverage:internal-error", planning.coverage(plan, [], "PLAN", "subject-a")["reasons"])

    def test_revision_keeps_findings_and_removed_item_history(self):
        old = make_plan(items=[item(), item("item-b")])
        old["findings"] = [{"id": "finding-a", "item_id": "item-b", "status": "open"}]
        old["plan_hash"] = planning.hash_plan(old)
        new = make_plan()
        with self.assertRaises(V3Error):
            planning.revise_plan(old, new)
        revised = planning.revise_plan(old, reviewed_revision(old, new))
        self.assertEqual(revised["findings"], old["findings"])
        self.assertEqual(revised["change_history"][0]["removed"][0]["id"], "item-b")
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["plan_hash"], planning.hash_plan(revised))
        new["findings"] = [{"id": "finding-a", "status": "resolved"}]
        new["plan_hash"] = planning.hash_plan(new)
        with self.assertRaises(V3Error):
            planning.revise_plan(old, reviewed_revision(old, new))

    def test_revision_requires_explicit_goal_scope_change(self):
        task = intent()
        task["goals"].append({"id": "goal-b", "text": "additional output"})
        old = make_plan(task, bundle(task), [item(goals=["goal-a", "goal-b"])])
        new = reviewed_revision(old, make_plan())
        with self.assertRaises(V3Error):
            planning.revise_plan(old, new)
        new["reviewed_change"]["scope_change"] = {"reason": "Output no longer requested",
            "source_refs": ["request:2"], "excluded_goal_ids": ["goal-b"]}
        new["plan_hash"] = planning.hash_plan(new)
        revised = planning.revise_plan(old, new)
        self.assertEqual(revised["change_history"][0]["excluded_goal_ids"], ["goal-b"])


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.library = self.root / "knowledge"
        self.library.mkdir()
        self.code = self.root / "code"
        self.code.mkdir()
        # Keep bundled corpus out of anonymous examples; only redirect source location.
        self.module_path = patch.object(retrieval, "__file__", str(self.code / "evolution" / "evidence_v3" / "retrieval.py"))
        self.module_path.start()
        self.addCleanup(self.module_path.stop)

    def write(self, relative, value, lines=False):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in value) if lines else json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def pack(self, entries, name="pack-a.json", **extra):
        pack = {"pack": "pack-a", "schema_version": 1, "entries": entries, **extra}
        path = self.library / name
        path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        return path

    def entry(self, identity="knowledge-a", **extra):
        return {"id": identity, "statement": "object alpha 可达性", "status": "active",
                "refs": ["source:a"], "conditions": {"mode": "a"}, "provenance": {"source": "seed"}, **extra}

    def search(self, limit=20):
        return retrieval.search(intent(), str(self.data), [str(self.library)], limit)

    def test_multichannel_reuses_real_shapes_without_store_mutations(self):
        self.write("memory/atoms.jsonl", [{"id": "a1", "kind": "observation", "fact": "object alpha", "refs": ["event:1"]}], True)
        self.write("memory/scenarios.json", [{"signature": "observation", "count": 1, "examples": ["object alpha"], "atom_ids": ["a1"]}])
        self.write("external_corpus/source.jsonl", [{"id": "case-a", "source": "local", "url": "ref:case-a", "title": "object alpha",
            "problem": {"text": "alpha"}, "solutions": [{"text": "alternative"}], "status": "unverified"}], True)
        self.pack([self.entry()])
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        found = self.search()
        self.assertEqual({c["channel"] for c in found["candidates"]}, {"atoms", "scenarios", "review_experience", "local_corpus"})
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertTrue(all(c["candidate_only"] for c in found["candidates"]))
        self.assertEqual(len(found["counterexample_queries"]), 1)
        self.assertEqual(found["status"], "ok")

    def test_missing_empty_and_corrupt_sources_are_distinct(self):
        missing = self.search()
        self.assertTrue(any(h["status"] == "unavailable" for h in missing["health"]))
        self.write("memory/atoms.jsonl", [], True)
        empty = self.search()
        self.assertTrue(any(h["channel"] == "atoms" and h["status"] == "empty" for h in empty["health"]))
        path = self.data / "memory" / "atoms.jsonl"
        path.write_text('{"id": "a1", "fact": "alpha", "refs": []}\nnot json\n', encoding="utf-8")
        broken = self.search()
        self.assertEqual(broken["status"], "error")
        self.assertTrue(any(h["channel"] == "atoms" and h["status"] == "error" for h in broken["health"]))
        self.assertTrue(any(c["id"] == "atoms:a1" for c in broken["candidates"]))

    def test_knowledge_strict_status_sources_duplicates_and_foreign_consumer(self):
        self.pack([self.entry(), self.entry("missing-status", status=None), self.entry("bad-source", refs=[], provenance={}),
                   self.entry("retired", status="revoked")])
        loaded = retrieval.load_knowledge([str(self.library)])
        self.assertEqual([e["id"] for e in loaded["entries"]], ["knowledge-a"])
        self.assertTrue(loaded["entries"][0]["candidate_only"])
        self.assertIn("legacy-package", loaded["entries"][0]["limitations"])
        self.assertEqual(loaded["health"][0]["status"], "error")
        self.pack([self.entry()], name="pack-b.json")
        duplicate = retrieval.load_knowledge([str(self.library)])
        self.assertEqual(duplicate["entries"], [])
        self.assertTrue(any(h.get("reason") == "duplicate-entry-id" for h in duplicate["health"]))
        self.pack([self.entry("foreign")], name="foreign.json", consumer="other-consumer")
        loaded = retrieval.load_knowledge([str(self.library)])
        self.assertTrue(any(h.get("reason") == "foreign-consumer" for h in loaded["health"]))
        self.assertFalse(any(e["id"] == "foreign" for e in loaded["entries"]))

    def test_legacy_catalogue_missing_status_is_quarantined(self):
        path = self.library / "catalogue.json"
        path.write_text(json.dumps({"pack": "catalogue", "schema_version": 1, "required_fields": ["x"]}), encoding="utf-8")
        loaded = retrieval.load_knowledge([str(self.library)])
        self.assertEqual(loaded["entries"], [])
        self.assertEqual(loaded["health"][0]["status"], "error")

    def test_applicability_unknown_despite_lexical_or_exact_condition_match(self):
        self.pack([self.entry("exact"), self.entry("missing", conditions={"other": "a"}),
                   self.entry("different", conditions={"mode": "b"})])
        candidates = {e["id"]: e for e in self.search()["candidates"]}
        self.assertEqual(set(candidates), {"exact", "missing", "different"})
        self.assertTrue(all(e["applicability"]["status"] == "unknown" for e in candidates.values()))
        self.assertTrue(candidates["exact"]["applicability"]["exact_conditions"])
        self.assertEqual(candidates["missing"]["applicability"]["missing"], ["other"])
        self.assertEqual(candidates["different"]["applicability"]["different"], ["mode"])

    def test_truncation_and_same_source_dedup_are_visible(self):
        self.pack([self.entry("first"), self.entry("copy"), self.entry("third", refs=["source:c"]),
                   self.entry("fourth", refs=["source:d"])])
        found = self.search(limit=1)
        self.assertEqual(found["truncation"]["total_matches"], 3)
        self.assertEqual(len(found["truncation"]["omitted_ids"]), 2)
        self.assertTrue(any(e["reason"] == "duplicate-source" for e in found["exclusions"]))
        self.assertIn("top-k-is-not-plan-scope", found["limitations"])
        task = intent()
        expanded = retrieval.search(task, str(self.data), [str(self.library)], 1)
        plan = planning.prepare_plan(task, expanded, proposal(task, expanded))
        self.assertEqual(len(plan["items"]), 1)

    def test_chinese_substrings_and_query_provenance_are_visible(self):
        self.pack([self.entry(statement="任意对象可达性需要复审")])
        found = self.search()
        candidate = found["candidates"][0]
        self.assertTrue(any("可达" in m["tokens"] for m in candidate["matches"]))
        self.assertTrue(all(q.get("origin_ref") for q in found["queries"]))

    def test_retracted_and_superseded_atoms_are_excluded(self):
        self.write("memory/atoms.jsonl", [
            {"id": "a1", "fact": "alpha", "refs": [], "retracted": True},
            {"id": "a2", "fact": "alpha", "refs": [], "superseded_by": "a3"},
            {"id": "a3", "fact": "alpha", "refs": []}], True)
        found = self.search()
        self.assertEqual([c["id"] for c in found["candidates"]], ["atoms:a3"])
        self.assertEqual(len([e for e in found["exclusions"] if e["reason"] == "retired-record"]), 2)

    def test_invalid_json_constants_duplicate_keys_and_generation_change(self):
        path = self.pack([self.entry()])
        first = retrieval.load_knowledge([str(self.library)])
        self.pack([self.entry(statement="Changed alpha")])
        self.assertNotEqual(first["generation"], retrieval.load_knowledge([str(self.library)])["generation"])
        for raw in ('{"pack":"p","pack":"q"}', '{"pack":"p","value":NaN}', '{"pack":"p","value":1e999}'):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                loaded = retrieval.load_knowledge([str(self.library)])
                self.assertEqual(loaded["health"][0]["status"], "error")
                self.assertEqual(loaded["entries"], [])

    def test_stale_scenario_cannot_resurrect_retired_atom(self):
        self.write("memory/atoms.jsonl", [{"id": "a1", "fact": "object alpha", "refs": [], "retracted": True}], True)
        self.write("memory/scenarios.json", [{"signature": "old-a", "examples": ["object alpha"], "atom_ids": ["a1"]}])
        found = self.search()
        self.assertEqual(found["candidates"], [])
        self.assertTrue(any(e["reason"] == "retired-scenario-dependency" for e in found["exclusions"]))

    def test_v3_packages_require_version_and_explicit_human_approval(self):
        valid = self.entry(version=1, approval={"actor_kind": "human", "actor_id": "reviewer-a",
            "status": "approved", "revoked": False, "event_id": "event-a", "reason": "Scope reviewed",
            "content_hash": "a" * 64})
        self.pack([valid], schema_version=3)
        loaded = retrieval.load_knowledge([str(self.library)])
        self.assertEqual(len(loaded["entries"]), 1)
        for mutation in (lambda e: e.pop("version"), lambda e: e["approval"].update(actor_kind="AI"),
                         lambda e: e["approval"].update(revoked=True)):
            entry = copy.deepcopy(valid)
            mutation(entry)
            self.pack([entry], schema_version=3)
            broken = retrieval.load_knowledge([str(self.library)])
            self.assertEqual(broken["entries"], [])
            self.assertEqual(broken["health"][0]["status"], "error")

    def test_package_lifecycle_blocks_active_children(self):
        self.pack([self.entry()], status="revoked")
        loaded = retrieval.load_knowledge([str(self.library)])
        self.assertEqual(loaded["entries"], [])
        self.assertEqual(loaded["health"][0]["reason"], "revoked")
        self.pack([self.entry()], status="unknown-status")
        loaded = retrieval.load_knowledge([str(self.library)])
        self.assertEqual(loaded["entries"], [])
        self.assertEqual(loaded["health"][0]["status"], "error")

    def test_invalid_requests(self):
        for limit in (0, -1, True, "1"):
            with self.assertRaises(V3Error):
                self.search(limit)
        with self.assertRaises(V3Error):
            retrieval.load_knowledge("directory")


if __name__ == "__main__":
    unittest.main()
