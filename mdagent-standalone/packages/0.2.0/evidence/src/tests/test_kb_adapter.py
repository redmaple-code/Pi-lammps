# -*- coding: utf-8 -*-
"""UKB compatibility layer: legacy shapes, id mapping, machine observations, fail-closed switching."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(SRC, "evolution"))
sys.path.insert(0, os.path.join(SRC, "mcp"))

PACKS = [os.path.join(SRC, "ukb", "data", "seed"), os.path.join(SRC, "data", "domain", "mdagent_legacy")]


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdkb-")
        os.environ["MDSIM_DATA_ROOT"] = self.tmp
        os.environ["MDAGENT_KB"] = "ukb"
        os.environ["MDAGENT_UKB_ROOT"] = os.path.join(self.tmp, "ukb")
        from evolution.kb import adapter
        self.adapter = adapter
        adapter.reset_cache()
        root = adapter.ukb_root()
        api = adapter._ukb_module()
        kb = api.UKB.init(root, packs=PACKS, actor="test")
        kb.close()
        kb = adapter.kb()
        self.admin_token = kb.add_admin("boss", "test")
        self.system_token = kb.add_admin(adapter.SYSTEM_ADMIN, "test")
        with open(os.path.join(root, "system_auto.token"), "w", encoding="utf-8") as f:
            f.write(self.system_token + "\n")
        self.kb = kb

    def tearDown(self):
        self.adapter.reset_cache()
        os.environ.pop("MDAGENT_KB", None)
        os.environ.pop("MDAGENT_UKB_ROOT", None)
        os.environ.pop("MDSIM_DATA_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _land(self, payload):
        r = self.kb.propose(payload, "import", "import:test")
        b = self.kb.prereview_bundle(r["task_id"], 0)
        op = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve", "note": "test",
              "checklist": [{"item": c, "ok": True} for c in b["instructions"]["checklist"]]}
        self.kb.prereview_submit(r["task_id"], "llm:reviewer", [b["page_token"]], op)
        v = self.kb.review_view(r["task_id"])
        self.kb.decide(r["task_id"], "boss", self.admin_token, "approve", "test", v["bound_hash"], "cli")
        return r["entry_id"]

    def _legacy(self, type_id, record, statement, extra=None, status="active"):
        fields = {"statement": statement, "raw_json": json.dumps(record, ensure_ascii=False)}
        fields.update(extra or {})
        return {"mode": "new", "type_id": type_id, "title": record["id"],
                "fields": fields, "tags": [{"facet": "topic", "tag": "legacy"}],
                "conditions": [{"key": "legacy_id", "value": record["id"]},
                               {"key": "legacy_pack", "value": "fixture"}]
                + ([{"key": "legacy_kind", "value": record["kind"]}] if record.get("kind") else []),
                "origin": {"kind": "document", "ref": "legacy:fixture/" + record["id"],
                           "summary_of_evidence": "fixture"}}

    # ---- switch ----
    def test_unknown_mode_is_refused(self):
        os.environ["MDAGENT_KB"] = "sometimes"
        with self.assertRaises(RuntimeError):
            self.adapter.enabled()
        os.environ["MDAGENT_KB"] = "ukb"

    def test_mode_precedence_and_corruption(self):
        from pathlib import Path
        mode_file = Path(self.tmp) / "kb_mode.json"
        os.environ.pop("MDAGENT_KB")
        self.assertFalse(self.adapter.enabled())
        mode_file.write_text('{"mode":"ukb"}', encoding="utf-8")
        self.assertTrue(self.adapter.enabled())
        mode_file.write_text('not-json', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.adapter.enabled()
        os.environ["MDAGENT_KB"] = "legacy"
        self.assertFalse(self.adapter.enabled())
        os.environ.pop("MDAGENT_KB")
        os.environ.pop("MDSIM_DATA_ROOT")
        # Without a per-process store override the repository configuration is authoritative.
        os.environ.pop("MDAGENT_UKB_ROOT")
        self.assertEqual(self.adapter.ukb_root(), str((Path(SRC) / "data" / "ukb").resolve()))
        os.environ["MDAGENT_UKB_ROOT"] = os.path.join(self.tmp, "ukb")
        self.assertEqual(self.adapter.ukb_root(), os.path.join(self.tmp, "ukb"))
        os.environ["MDSIM_DATA_ROOT"] = self.tmp
        self.assertEqual(self.adapter.ukb_root(), os.path.join(self.tmp, "ukb"))
        os.environ["MDAGENT_KB"] = "ukb"

    def test_missing_store_fails_closed(self):
        os.environ["MDAGENT_UKB_ROOT"] = os.path.join(self.tmp, "nope")
        self.adapter.reset_cache()
        with self.assertRaises(RuntimeError):
            self.adapter.kb()

    # ---- reads ----
    def test_anchor_roundtrip(self):
        record = {"id": "anchor-x", "kind": "physics-anchor", "quantity": "dt", "unit": "ps",
                  "range": {"recommended_max": 0.001}, "status": "active", "exemptible": False, "refs": ["p:1"]}
        self._land(self._legacy("legacy_anchor", record, "dt", {"quantity": "dt", "unit": "ps"}))
        anchors = self.adapter.load_anchors()
        self.assertEqual(list(anchors), ["anchor-x"])
        self.assertEqual(anchors["anchor-x"]["range"], {"recommended_max": 0.001})
        self.assertEqual(anchors["anchor-x"]["status"], "active")
        idx = self.adapter.exp_status_index()
        self.assertEqual(idx["anchor-x"]["kind"], "physics-anchor")
        self.assertEqual(idx["anchor-x"]["severity"], "hard")

    def test_dimension_and_surface_shapes(self):
        self._land(self._legacy("legacy_dimension",
                                {"id": "dim-1", "kind": "methodology-dimension", "family": "elastic",
                                 "question": "方法是否适用", "check": {"type": "text_matches"}, "status": "active"},
                                "方法是否适用", {"family": "elastic"}))
        self._land(self._legacy("legacy_surface",
                                {"id": "sur-1", "phase": "design", "question": "组分？", "na_allowed": False,
                                 "status": "active"}, "组分？", {"phase": "design"}))
        dims = self.adapter.load_dimensions()
        self.assertEqual([e["id"] for e in dims["elastic"]], ["dim-1"])
        items = self.adapter.load_items()
        self.assertEqual(items["sur-1"]["phase"], "design")
        self.assertFalse(items["sur-1"]["na_allowed"])

    def test_methodology_constraints_available(self):
        self._land(self._legacy("legacy_methodology",
                                {"id": "m-1", "kind": "methodology", "statement": "不得同时控压与变形",
                                 "constraints": {"violate_if": []}, "severity_hint": "hard", "status": "active"},
                                "不得同时控压与变形"))
        entries = self.adapter.load_methodology_entries()
        self.assertEqual([e["id"] for e in entries], ["m-1"])
        self.assertEqual(entries[0]["constraints"], {"violate_if": []})
        self.assertEqual(entries[0]["_status"], "active")

    def test_pack_catalog_hidden_from_gate_index(self):
        self._land(self._legacy("legacy_entry",
                                {"id": "pack-cat", "kind": "pack-catalog", "statement": "整包目录"},
                                "整包目录", {"kind": "pack-catalog"}))
        self.assertNotIn("pack-cat", self.adapter.exp_status_index())
        self.assertIn("pack-cat", [e["id"] for e in self.adapter.load_knowledge()["entries"]])

    # ---- writes ----
    def test_record_atom_lands_and_retracts(self):
        atom = self.adapter.record_atom("repeat-question", "同一个问题被重复提问，需要记录以免重复回答。", ["s:1"])
        self.assertIsNotNone(atom)
        visible = [a["id"] for a in self.adapter.load_atoms()]
        self.assertIn(atom["id"], visible)
        self.assertEqual(self.adapter.current_value("repeat-question")["id"], atom["id"])
        self.assertEqual([a["kind"] for a in self.adapter.find_scenario_semantic("repeat question")][:1],
                         ["repeat-question"])
        self.assertTrue(self.adapter.retract_atom(atom["id"], "wrong"))
        self.assertNotIn(atom["id"], [a["id"] for a in self.adapter.load_atoms()])

    def test_short_or_noisy_atoms_rejected(self):
        self.assertIsNone(self.adapter.record_atom("k", "short"))
        self.assertIsNone(self.adapter.record_atom("friction-tool-error", "turn 3 session x " + "z" * 20))

    def test_auto_land_can_be_switched_off(self):
        os.environ["MDAGENT_KB_AUTO_OBS"] = "0"
        try:
            atom = self.adapter.record_atom("observation", "只是一条观察事实，暂不自动放行。", [])
            self.assertNotIn(atom["id"], [a["id"] for a in self.adapter.load_atoms()])
        finally:
            os.environ.pop("MDAGENT_KB_AUTO_OBS", None)

    # ---- retrieval ----
    def test_retrieval_records_and_search(self):
        self._land(self._legacy("legacy_anchor",
                                {"id": "anchor-dt", "kind": "physics-anchor", "quantity": "时间步长 dt",
                                 "unit": "ps", "status": "active", "refs": ["p:1"]},
                                "时间步长 dt", {"quantity": "时间步长 dt"}))
        knowledge = self.adapter.load_knowledge()
        self.assertEqual([e["id"] for e in knowledge["entries"]], ["anchor-dt"])
        entry = knowledge["entries"][0]
        self.assertEqual(entry["channel"], "ukb")
        self.assertEqual(entry["lifecycle"], "active")
        self.assertIsNotNone(entry["approval"])
        from evolution.evidence_v3.planning import validate_intent
        intent = validate_intent({"action": "consult", "goals": [{"id": "g1", "text": "时间步长"}],
                                  "conditions": {}, "unknowns": [], "source_refs": ["request:1"],
                                  "queries": ["dt timestep"]})
        result = self.adapter.search(intent, self.tmp, limit=5)
        self.assertEqual([e["id"] for e in result["candidates"] if not e["id"].startswith("corpus:")],
                         ["anchor-dt"])
        self.assertTrue(result["candidates"][0]["matches"])

    def test_publish_knowledge_goes_through_review(self):
        from evolution.evidence_v3 import policy, knowledge
        from evolution.evidence_v3.contracts import V3Error
        proposal = {"id": "exp-x", "class": knowledge.CLASS_NAME, "tier": "human", "op": "publish",
                    "kind": "check_knowledge", "status": "active", "owner_rule": "E3",
                    "content": {"question_or_claim": "某条可核查的证据知识"}, "conditions": {},
                    "source_refs": ["s1"], "sources": [{"id": "s1", "source_kind": "publication",
                    "source_locator": "doi:1", "provenance_refs": []}],
                    "verification_method": {"kind": "semantic"}}
        proposal["review"] = {"actor_kind": "human", "actor_id": "boss", "event_id": "old-event",
                              "status": "approved", "revoked": False, "reason": "old review",
                              "content_hash": policy.promotion_content_hash(proposal)}
        with self.assertRaises(V3Error):
            self.adapter.publish_knowledge(dict(proposal, review=dict(proposal["review"], content_hash="bad")),
                                           "boss", self.tmp)
        out = knowledge.publish(proposal, "boss", self.tmp)
        self.assertEqual(out["status"], "pending_review")
        self.assertTrue(out["event_id"].startswith("ukb:"))
        self.assertEqual(self.adapter.load_knowledge()["entries"], [])
        self.assertEqual(self.adapter.search({"action": "consult", "goals": [{"id": "g1", "text": "证据知识"}],
                         "conditions": {}, "unknowns": [], "source_refs": ["request:1"]}, self.tmp)["candidates"], [])
        task = out["task_id"]
        bundle = self.kb.prereview_bundle(task, 0)
        opinion = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve",
                   "note": "test", "checklist": [{"item": c, "ok": True}
                   for c in bundle["instructions"]["checklist"]]}
        self.kb.prereview_submit(task, "llm:reviewer", [bundle["page_token"]], opinion)
        view = self.kb.review_view(task)
        self.kb.decide(task, "boss", self.admin_token, "approve", "verified in test", view["bound_hash"], "cli")
        loaded = self.adapter.load_knowledge()["entries"]
        self.assertEqual([e["id"] for e in loaded], ["exp-x"])
        self.assertEqual(loaded[0]["approval"]["actor_id"], "boss")
        self.assertEqual(loaded[0]["source_refs"], ["doi:1"])
        self.assertFalse(loaded[0]["candidate_only"])
        self.assertIn("exp-x", self.adapter.knowledge_snapshot(self.tmp)["entries"])
        self.assertEqual([e["id"] for e in self.adapter.search(
            {"action": "consult", "goals": [{"id": "g1", "text": "证据知识"}], "conditions": {},
             "unknowns": [], "source_refs": ["request:1"]}, self.tmp)["candidates"]], ["exp-x"])

    def test_verified_evidence_projects_to_ukb_candidate_only(self):
        import hashlib
        from evolution.evidence_v3.service import TaskService
        from evolution.evidence_v3.contracts import V3Error
        source = os.path.join(self.tmp, "verified.txt")
        with open(source, "w", encoding="utf-8") as stream:
            stream.write("verified source material")
        with open(source, "rb") as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        service = TaskService(self.tmp)
        state = service.create("task-evidence", {"action": "consult", "goals": [{"id": "g1", "text": "evidence"}],
            "conditions": {}, "unknowns": [], "source_refs": ["request:1"]})
        record = {"id": "ev-1", "task_id": "task-evidence", "lifecycle": "active",
                  "source_kind": "publication", "claim_id": "g1", "claim": "verified evidence claim",
                  "conditions": {}, "provenance_refs": ["run:run-1"], "source_sha256": digest,
                  "verification": {"status": "verified", "method": "content_review", "source_locator": source}}
        service.record_evidence("task-evidence", record, state["revision"])
        out = self.adapter.submit_evidence_record(record, "task-evidence", self.tmp)
        self.assertEqual(out["status"], "pending_review")
        self.assertEqual(self.adapter.load_knowledge()["entries"], [])
        tampered = dict(record, claim="tampered")
        with self.assertRaises(V3Error):
            self.adapter.submit_evidence_record(tampered, "task-evidence", self.tmp)
        import evidence_api
        self.assertIn("submit_evidence", evidence_api.TOOL["inputSchema"]["properties"]["action"]["enum"])
        replay = evidence_api.dispatch({"action": "submit_evidence", "task_id": "task-evidence",
                                        "record": record}, self.tmp)
        self.assertEqual(replay["entry_id"], out["entry_id"])
        self.assertEqual(self.kb.conn.execute("SELECT count(*) FROM entry WHERE type_id='evidence_claim'").fetchone()[0], 1)
        self.assertTrue(evidence_api.dispatch({"action": "submit_evidence", "task_id": "task-evidence",
                                               "record": tampered}, self.tmp)["fail_closed"])
        task = out["task_id"]
        bundle = self.kb.prereview_bundle(task, 0)
        opinion = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve",
                   "note": "synthetic review", "checklist": [{"item": c, "ok": True}
                   for c in bundle["instructions"]["checklist"]]}
        self.kb.prereview_submit(task, "llm:fixture", [bundle["page_token"]], opinion)
        view = self.kb.review_view(task)
        self.kb.decide(task, "boss", self.admin_token, "approve", "synthetic approval", view["bound_hash"], "cli")
        loaded = self.adapter.load_knowledge()["entries"]
        self.assertEqual(loaded[0]["id"], "evidence:task-evidence:ev-1")
        self.assertEqual(loaded[0]["record"]["claim"], record["claim"])
        before = self.adapter.knowledge_snapshot(self.tmp)
        dependent = service.create("task-dependent", {"action": "consult", "goals": [{"id": "g1", "text": "evidence"}],
            "conditions": {}, "unknowns": [], "source_refs": ["request:2"]})
        with open(source, "w", encoding="utf-8") as stream:
            stream.write("source changed")
        with self.assertRaises(V3Error):
            self.adapter.submit_evidence_record(record, "task-evidence", self.tmp)
        self.assertNotEqual(before["hash"], self.adapter.knowledge_snapshot(self.tmp)["hash"])
        self.assertEqual(self.adapter.load_knowledge()["entries"], [])
        self.assertNotIn("evidence:task-evidence:ev-1",
                         [entry["id"] for entry in self.adapter.search(dependent["intent"], self.tmp)["candidates"]])
        with self.assertRaises(V3Error) as stale:
            service._generation_ok(dependent, service.store.generation())
        self.assertEqual(stale.exception.code, "REVALIDATION_REQUIRED")

    def test_reinforcement_invalidates_snapshot_without_new_revision(self):
        entry_id = self._land(self._legacy("legacy_entry", {"id": "snapshot-entry", "statement": "检索证据"}, "检索证据"))
        before = self.adapter.knowledge_snapshot(self.tmp)
        generation = self.adapter.load_knowledge()["generation"]
        r = self.kb.propose({"mode": "reinforce", "target_entry_id": entry_id,
             "sources": [{"source_type": "legacy_ref", "title": "new source", "locator": "source:reinforced"}],
             "note": "additional evidence"}, "import", "import:test")
        bundle = self.kb.prereview_bundle(r["task_id"], 0)
        opinion = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve",
                   "note": "synthetic review", "checklist": [{"item": c, "ok": True}
                   for c in bundle["instructions"]["checklist"]]}
        tokens = [self.kb.prereview_bundle(r["task_id"], p)["page_token"] for p in range(bundle["pages"])]
        self.kb.prereview_submit(r["task_id"], "llm:fixture", tokens, opinion)
        view = self.kb.review_view(r["task_id"])
        self.kb.decide(r["task_id"], "boss", self.admin_token, "approve", "synthetic approval", view["bound_hash"], "cli")
        after = self.adapter.knowledge_snapshot(self.tmp)
        self.assertEqual(before["entries"]["snapshot-entry"]["version"], after["entries"]["snapshot-entry"]["version"])
        self.assertNotEqual(before["hash"], after["hash"])
        self.assertNotEqual(generation, self.adapter.load_knowledge()["generation"])
        self.assertIn("source:reinforced", after["entries"]["snapshot-entry"]["source_refs"])
        self.assertIn("source:reinforced", self.adapter.load_knowledge()["entries"][0]["source_refs"])

    def test_approved_ukb_change_requires_task_revalidation(self):
        from evolution.evidence_v3.service import TaskService
        from evolution.evidence_v3.contracts import V3Error
        service = TaskService(self.tmp)
        intent = {"action": "consult", "goals": [{"id": "g1", "text": "证据知识"}],
                  "conditions": {}, "unknowns": [], "source_refs": ["request:1"]}
        state = service.create("task-ukb-refresh", intent)
        self.assertEqual(state["knowledge_snapshot"]["entries"], {})
        self._land(self._legacy("legacy_entry", {"id": "exp-new", "kind": "check_knowledge",
                                 "question_or_claim": "新增证据知识"}, "新增证据知识"))
        with self.assertRaises(V3Error) as error:
            service._generation_ok(state, service.store.generation())
        self.assertEqual(error.exception.code, "REVALIDATION_REQUIRED")
        refreshed = service.refresh_knowledge("task-ukb-refresh", state["revision"])
        self.assertTrue(refreshed["needs_revalidation"])
        self.assertEqual(refreshed["revision"], state["revision"] + 1)
        self.assertIn("exp-new", refreshed["knowledge_snapshot"]["entries"])
        self.assertFalse(refreshed["knowledge_refreshes"][-1]["reused"])

    def _execution_fixture(self):
        import test_evidence_v3_service as fixtures
        from unittest.mock import patch
        case = fixtures.TaskServiceTests("test_validation_is_independent_and_does_not_clear_production")
        with patch.dict(os.environ, {"MDAGENT_UKB_ROOT": self.adapter.ukb_root()}):
            case.setUp()
        self.addCleanup(case.tearDown)
        self.addCleanup(self.adapter.reset_cache)
        api = self.adapter._ukb_module()
        store_root = self.adapter.ukb_root(case.root)
        if not os.path.exists(os.path.join(store_root, "ukb.sqlite")):
            temporary = api.UKB.init(store_root, packs=PACKS, actor="test")
            temporary.close()
        case.state = case.service.refresh_knowledge("task-a", case.state["revision"])
        # Fixture builders expect the initial revision; re-create with the UKB root selected.
        with patch.dict(os.environ, {"MDAGENT_UKB_ROOT": self.adapter.ukb_root(case.root)}):
            case.service = fixtures.TaskService(case.root, action_adapter=fixtures.adapter,
                human_verifier=case.service.human_verifier)
            case.service.store.task_dir("task-a").rename(case.root / "initial-task")
            case.state = case.service.create("task-a", fixtures.intent())
            spec, permit = case.probe()
        return case, spec, permit

    def test_start_releases_ukb_reader_before_launcher(self):
        from unittest.mock import patch
        import test_evidence_v3_service as fixtures
        case, spec, permit = self._execution_fixture()
        root = self.adapter.ukb_root(case.root)
        with patch.dict(os.environ, {"MDAGENT_UKB_ROOT": root}):
            def launch(frozen):
                writer = self.adapter._ukb_module().UKB(root)
                previous, previous_token = self.kb, self.admin_token
                try:
                    writer.conn.execute("PRAGMA busy_timeout=500")
                    self.kb = writer
                    try:
                        self.admin_token = writer.add_admin("boss", "test")
                    except Exception:
                        self.admin_token = writer.reset_token("boss", "test")
                    self._land(self._legacy("legacy_entry", {"id": "during-launch"}, "approved observation"))
                finally:
                    self.kb, self.admin_token = previous, previous_token
                    writer.close()
                return fixtures.handoff()
            run = case.service.start("task-a", permit["id"], spec, "start-reader", launch)
        self.assertEqual(run["status"], "RUNNING")
        self.assertTrue(run["needs_revalidation"])
        self.assertTrue(case.service.store.load("task-a")["needs_revalidation"])

    def test_approved_change_before_permit_consumption_blocks_launch(self):
        from unittest.mock import patch
        import test_evidence_v3_service as fixtures
        case, spec, permit = self._execution_fixture()
        root = self.adapter.ukb_root(case.root)
        called = []
        with patch.dict(os.environ, {"MDAGENT_UKB_ROOT": root}):
            def changed_adapter(spec, context):
                previous = self.kb
                self.kb = self.adapter.kb()
                try:
                    self._land(self._legacy("legacy_entry", {"id": "before-consume"}, "new approved knowledge"))
                finally:
                    self.kb = previous
                return fixtures.adapter(spec, context)
            case.service.action_adapter = changed_adapter
            with self.assertRaises(fixtures.V3Error) as stale:
                case.service.start("task-a", permit["id"], spec, "start-stale", lambda spec: called.append(spec))
            self.assertEqual(stale.exception.code, "REVALIDATION_REQUIRED")
        self.assertEqual(called, [])
        self.assertEqual(case.service.store.load("task-a")["runs"], [])

    def test_finish_allows_approval_and_stale_run_stays_unusable_after_refresh(self):
        from unittest.mock import patch
        import test_evidence_v3_service as fixtures
        from evidence_v3 import policy
        case, spec, permit = self._execution_fixture()
        root = self.adapter.ukb_root(case.root)
        with patch.dict(os.environ, {"MDAGENT_UKB_ROOT": root}):
            run = case.service.start("task-a", permit["id"], spec, "start-finish", lambda spec: fixtures.handoff())
            def verify(server_run, result):
                writer = self.adapter._ukb_module().UKB(root)
                previous, previous_token = self.kb, self.admin_token
                try:
                    writer.conn.execute("PRAGMA busy_timeout=500")
                    self.kb = writer
                    try:
                        self.admin_token = writer.add_admin("boss", "test")
                    except Exception:
                        self.admin_token = writer.reset_token("boss", "test")
                    self._land(self._legacy("legacy_entry", {"id": "during-finish"}, "approved knowledge"))
                finally:
                    self.kb, self.admin_token = previous, previous_token
                    writer.close()
                return {"run_id": server_run["id"], "execution_complete": True, "returncode": 0}
            case.service.result_verifier = verify
            finished = case.service.finish("task-a", run["id"],
                {"run_id": run["id"], "returncode": 0, "outputs": []})
            self.assertEqual(finished["status"], "FINISHED")
            self.assertTrue(finished["needs_revalidation"])
            current = case.service.read("task-a")
            refreshed = case.service.refresh_knowledge("task-a", current["revision"])
            with self.assertRaises(fixtures.V3Error) as stale:
                case.service.record_measurement("task-a", run["id"], "item-a", {}, "stale-measure", refreshed["revision"])
            self.assertEqual(stale.exception.code, "STALE_RUN_KNOWLEDGE")
            state = case.service.read("task-a")
            state["runs"][0]["kind"] = "production"
            with self.assertRaises(fixtures.V3Error) as stale:
                case.service._production_run(state)
            self.assertEqual(stale.exception.code, "STALE_RUN_KNOWLEDGE")
            with self.assertRaises(fixtures.V3Error):
                policy._run_manifest(dict(case.service._context(state), run_id=run["id"]), {})
            state["runs"][0].pop("needs_revalidation")
            with self.assertRaises(fixtures.V3Error) as stale:
                case.service._production_run(state)
            self.assertEqual(stale.exception.code, "STALE_RUN_KNOWLEDGE")

    def test_wal_gate_fails_closed(self):
        self.kb.conn.execute("PRAGMA journal_mode=WAL")
        with self.assertRaisesRegex(RuntimeError, "WAL"):
            self.adapter.knowledge_snapshot(self.tmp)
        self.kb.conn.execute("PRAGMA journal_mode=DELETE")

    def test_ukb_snapshot_never_reads_legacy_files(self):
        from pathlib import Path
        from evolution.evidence_v3.knowledge import snapshot
        legacy = Path(self.tmp) / "memory" / "review_experience"
        legacy.mkdir(parents=True)
        (legacy / "bad.json").write_text("invalid", encoding="utf-8")
        self.assertEqual(snapshot(self.tmp)["entries"], {})


if __name__ == "__main__":
    unittest.main()
