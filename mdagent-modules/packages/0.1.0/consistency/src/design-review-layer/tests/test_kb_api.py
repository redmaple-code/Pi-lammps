"""Knowledge-base interface tests: parsing, writes, review chain and inspection.

All write tests run on a temporary copy of the production store; the production
data root and token file are never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_layer import kb_api, ukb_store  # noqa: E402

_CANDIDATES = (ROOT.parent / "data" / "ukb" / "ukb.sqlite",
               ROOT.parent / "MDagent-evidence-v3" / "data" / "ukb" / "ukb.sqlite")
PROD = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[-1])


class ParseTests(unittest.TestCase):
    def test_old_situation_keeps_open(self):
        parsed = ukb_store._parse_common_issue({
            "title": "常见问题 CI-008：实验方案由 AI 反推/缺原始记录",
            "ref": "e1@1", "type": "lesson",
            "fields": {"situation": "匹配词：反推、循环论证；类别：provenance；处置：open",
                       "risk": "risk", "fix": "fix"},
        })
        self.assertEqual(parsed["disposition"], "open")
        self.assertEqual(parsed["category"], "provenance")
        self.assertEqual(parsed["match_terms"], ["反推", "循环论证"])
        self.assertIsNone(parsed["evidence_level"])
        self.assertIn("evidence_level", parsed["metadata_missing"])

    def test_unknown_disposition_does_not_hide_point(self):
        parsed = ukb_store._parse_common_issue({
            "title": "常见问题 CI-X：x", "ref": "e@1", "type": "lesson",
            "fields": {"situation": "匹配词：x；类别：y；处置：whatever", "risk": "", "fix": ""},
        })
        self.assertEqual(parsed["disposition"], "open")

    def test_new_situation_round_trips_every_field(self):
        seed = {"id": "CI-008", "name": "n", "category": "provenance", "match_terms": ["反推", "E-06"],
                "disposition": "open", "evidence_level": "E1", "status": "must_answer",
                "explanation": "r", "required_declaration": "f"}
        text = ukb_store.situation_for(seed)
        parsed = ukb_store._parse_common_issue({
            "title": "常见问题 CI-008：n", "ref": "e@1", "type": "lesson",
            "fields": {"situation": text, "risk": "r", "fix": "f"},
        })
        self.assertEqual(parsed["disposition"], "open")
        self.assertEqual(parsed["category"], "provenance")
        self.assertEqual(parsed["evidence_level"], "E1")
        self.assertEqual(parsed["status"], "must_answer")
        self.assertTrue(parsed["metadata_complete"])


@unittest.skipUnless(PROD.exists(), "production UKB store is not available")
class KnowledgeBaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kb_api_test_")
        cls.root = Path(cls.tmp.name) / "ukb"
        cls.root.mkdir()
        shutil.copy2(PROD, cls.root / "ukb.sqlite")
        cls.token_file = Path(cls.tmp.name) / "admin.json"
        cls.old_env = {k: os.environ.get(k) for k in
                       ("REVIEW_UKB_ROOT", "REVIEW_UKB_ADMIN_FILE", "MDAGENT_KB", "MDAGENT_UKB_ROOT")}
        os.environ["REVIEW_UKB_ROOT"] = str(cls.root)
        os.environ["REVIEW_UKB_ADMIN_FILE"] = str(cls.token_file)
        ukb_store.ensure_admin_token(force=True)
        ukb_store.ensure_system_admin()
        cls.upgrade = ukb_store.upgrade_common_issue_metadata()

    @classmethod
    def tearDownClass(cls):
        ukb_store.close_connections()
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def setUp(self):
        self.kb = kb_api.KnowledgeBase(root=self.root)

    def tearDown(self):
        self.kb.close()

    def test_upgrade_completes_every_lesson(self):
        self.assertTrue({r["status"] for r in self.upgrade} <= {"upgraded", "already-complete"})
        self.assertFalse([r for r in self.upgrade if r["status"] == "error"])
        ci = ukb_store.load_common_issues()
        self.assertEqual(len(ci["entries"]), 12)
        self.assertTrue(all(e["metadata_complete"] for e in ci["entries"]))
        ci008 = next(e for e in ci["entries"] if e["id"] == "CI-008")
        self.assertEqual((ci008["disposition"], ci008["evidence_level"], ci008["status"]),
                         ("open", "E1", "must_answer"))

    def test_inspection_surfaces(self):
        self.assertEqual(self.kb.integrity()["integrity"], "ok")
        # A fresh handoff store is seeded with baseline 9 + common 12 + discovery 24
        # entries. The original threshold (>100) assumed the author's production
        # store; the portable assertion is that every seed section is present.
        self.assertGreaterEqual(self.kb.stats()["total"], 45)
        self.assertIn("entry", self.kb.schema())
        self.assertTrue(self.kb.verify_audit()["ok"])
        self.assertTrue(self.kb.audit_tail(3))
        self.assertTrue(self.kb.types())
        self.assertTrue(self.kb.condition_keys())

    def test_catalog_read_and_history(self):
        row = self.kb.find_entries("CI-001", type_id="lesson")[0]
        entry = self.kb.entry(row["entry_id"])
        self.assertEqual(entry["type"], "lesson")
        self.assertIn("risk", entry["fields"])
        self.assertTrue(entry["tags"])
        self.assertTrue(self.kb.revisions(entry["entry_id"]))

    def test_revise_creates_new_revision_and_keeps_old(self):
        row = self.kb.find_entries("CI-010", type_id="lesson")[0]
        entry = self.kb.entry(row["entry_id"])
        old_ref = entry["ref"]
        out = self.kb.revise(old_ref, fields={"risk": entry["fields"]["risk"] + "（修订）"},
                             note="test revision", approve=True)
        self.assertEqual(out["decision"]["state"], "approved")
        new = self.kb.entry(entry["entry_id"])
        self.assertEqual(new["rev_no"], entry["rev_no"] + 1)
        self.assertTrue(new["fields"]["risk"].endswith("（修订）"))
        old = self.kb.entry(old_ref)
        self.assertEqual(old["rev_no"], entry["rev_no"])
        self.assertFalse(old["fields"]["risk"].endswith("（修订）"))

    def test_status_transition_and_visibility(self):
        row = self.kb.find_entries("CI-011", type_id="lesson")[0]
        entry = self.kb.entry(row["entry_id"])
        self.assertIn("deprecated", self.kb.allowed_transitions(entry["ref"]))
        out = self.kb.change_status(entry["ref"], "deprecated", "test", approve=True)
        self.assertEqual(out["decision"]["state"], "approved")
        with self.assertRaises(kb_api.KBError):
            self.kb.entry(entry["entry_id"], profile="agent")
        self.assertEqual(self.kb.entry(entry["entry_id"], profile="agent_wide")["status"], "deprecated")
        self.kb.change_status(entry["ref"], "active", "restore", approve=True)
        self.assertEqual(self.kb.entry(entry["entry_id"], profile="agent")["status"], "active")

    def test_tags_add_and_required_facet_guard(self):
        row = self.kb.find_entries("CI-012", type_id="lesson")[0]
        entry = self.kb.entry(row["entry_id"])
        self.kb.add_tags(entry["ref"], [{"facet": "stage", "tag": "planning"}], "test", approve=True)
        tags = {t["tag"] for t in self.kb.entry(entry["entry_id"])["tags"]}
        self.assertIn("planning", tags)
        with self.assertRaises(kb_api.KBError) as ctx:
            self.kb.drop_tags(entry["ref"], ["topic/general"], "remove required topic", approve=True)
        self.assertEqual(ctx.exception.code, "TAG_REQUIRED")

    def test_new_entry_stays_candidate_until_prereview_and_approval(self):
        payload = {
            "mode": "new", "type_id": "rule", "title": "接口测试规则：只做存取验证",
            "fields": {"statement": "接口测试规则只验证存取链路。", "severity": "soft",
                       "rationale": "测试", "check": "测试"},
            "tags": [{"facet": "topic", "tag": "general"}, {"facet": "stage", "tag": "planning"}],
            "sources": [{"source_type": "knowledge_entry", "title": "test",
                         "locator": "design-review-layer/tests/test_kb_api.py"}],
            "origin": {"kind": "manual", "ref": "test:kb-api", "summary_of_evidence": "interface test"},
        }
        proposed = self.kb.propose(payload)
        candidate = self.kb.entry("%s@%d" % (proposed["entry_id"], proposed["rev_no"]), profile="reviewer")
        self.assertEqual(candidate["status"], "candidate")
        with self.assertRaises(kb_api.KBError):
            self.kb.entry("%s@%d" % (proposed["entry_id"], proposed["rev_no"]), profile="agent")
        self.kb.mechanical_prereview(proposed["task_id"])
        decided = self.kb.decide(proposed["task_id"], "approve", "interface test")
        self.assertEqual(decided["state"], "approved")
        self.assertEqual(self.kb.entry(proposed["entry_id"], profile="agent")["status"], "active")

    def test_stale_bound_hash_is_rejected(self):
        payload = {
            "mode": "new", "type_id": "rule", "title": "接口测试规则：stale hash",
            "fields": {"statement": "stale hash test", "severity": "soft", "rationale": "t", "check": "t"},
            "tags": [{"facet": "topic", "tag": "general"}],
            "sources": [{"source_type": "knowledge_entry", "title": "t", "locator": "t"}],
            "origin": {"kind": "manual", "ref": "test:stale", "summary_of_evidence": "t"},
        }
        result = self.kb.propose(payload)
        self.kb.mechanical_prereview(result["task_id"])
        with self.assertRaises(kb_api.KBError) as ctx:
            self.kb._call(self.kb.kb.decide, result["task_id"], self.kb.admin, self.kb.token,
                          "approve", "x", "0" * 64, "webui")
        self.assertEqual(ctx.exception.code, "STALE_REVIEW")

    def test_seed_functions_are_idempotent(self):
        before = self.kb.stats()["total"]
        self.assertEqual(ukb_store.seed_baseline(), 0)
        self.assertEqual(ukb_store.seed_common_issues(), 0)
        self.assertEqual(self.kb.stats()["total"], before)

    def test_system_admin_token_authenticates(self):
        token = (self.root / "system_auto.token").read_text(encoding="utf-8").strip()
        self.assertTrue(token)
        who = self.kb.kb.auth("mdagent-system", token)
        self.assertEqual(who["reviewer_id"], "mdagent-system")


if __name__ == "__main__":
    unittest.main()
