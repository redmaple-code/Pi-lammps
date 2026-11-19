# -*- coding: utf-8 -*-
"""Generic knowledge-base client: read, inspect, write, review and token handling.

Runs on a temporary copy of the production store; production data and tokens are
never touched.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, SRC)

from evolution.kb import client  # noqa: E402

PROD = Path(SRC) / "data" / "ukb" / "ukb.sqlite"


@unittest.skipUnless(PROD.exists(), "production UKB store is not available")
class KnowledgeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kb_client_test_")
        cls.root = Path(cls.tmp.name) / "ukb"
        cls.root.mkdir()
        shutil.copy2(PROD, cls.root / "ukb.sqlite")
        cls.token_file = Path(cls.tmp.name) / "admin.json"
        cls.old_env = {k: os.environ.get(k) for k in
                       ("MDAGENT_UKB_ROOT", "MDAGENT_UKB_ADMIN_FILE", "REVIEW_UKB_ROOT", "REVIEW_UKB_ADMIN_FILE")}
        os.environ["MDAGENT_UKB_ROOT"] = str(cls.root)
        os.environ["MDAGENT_UKB_ADMIN_FILE"] = str(cls.token_file)
        cls.admin = client.ensure_admin_token(force=True)
        cls.system = client.ensure_system_admin()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def setUp(self):
        self.kb = client.KnowledgeBase(root=self.root)

    def tearDown(self):
        self.kb.close()

    def test_inspection_and_read(self):
        self.assertEqual(self.kb.integrity()["integrity"], "ok")
        self.assertGreater(self.kb.stats()["total"], 100)
        self.assertIn("entry", self.kb.schema())
        self.assertTrue(self.kb.verify_audit()["ok"])
        self.assertTrue(self.kb.audit_tail(2))
        row = self.kb.find_entries("CI-001", type_id="lesson")[0]
        entry = self.kb.entry(row["entry_id"])
        self.assertEqual(entry["type"], "lesson")
        self.assertTrue(self.kb.revisions(entry["entry_id"]))

    def test_revise_and_status(self):
        row = self.kb.find_entries("CI-010", type_id="lesson")[0]
        entry = self.kb.entry(row["entry_id"])
        out = self.kb.revise(entry["ref"], fields={"risk": entry["fields"]["risk"] + "（client）"},
                             note="client test", approve=True)
        self.assertEqual(out["decision"]["state"], "approved")
        new = self.kb.entry(entry["entry_id"])
        self.assertEqual(new["rev_no"], entry["rev_no"] + 1)
        self.kb.change_status(new["ref"], "deprecated", "client test", approve=True)
        self.assertEqual(self.kb.entry(new["entry_id"], profile="agent_wide")["status"], "deprecated")
        self.kb.change_status(new["ref"], "active", "restore", approve=True)
        self.assertEqual(self.kb.entry(new["entry_id"])["status"], "active")

    def test_new_entry_needs_prereview_and_approval(self):
        payload = {
            "mode": "new", "type_id": "rule", "title": "客户端测试规则：存取链路",
            "fields": {"statement": "客户端测试规则只验证读写链路。", "severity": "soft",
                       "rationale": "test", "check": "test"},
            "tags": [{"facet": "topic", "tag": "general"}, {"facet": "stage", "tag": "planning"}],
            "sources": [{"source_type": "knowledge_entry", "title": "test",
                         "locator": "MDagent-evidence-v3/tests/test_kb_client.py"}],
            "origin": {"kind": "manual", "ref": "test:kb-client", "summary_of_evidence": "client test"},
        }
        out = self.kb.propose(payload)
        candidate = self.kb.entry("%s@%d" % (out["entry_id"], out["rev_no"]), profile="reviewer")
        self.assertEqual(candidate["status"], "candidate")
        with self.assertRaises(client.KBError):
            self.kb.entry("%s@%d" % (out["entry_id"], out["rev_no"]), profile="agent")
        self.kb.mechanical_prereview(out["task_id"])
        self.assertEqual(self.kb.decide(out["task_id"], "approve", "client test")["state"], "approved")
        self.assertEqual(self.kb.entry(out["entry_id"], profile="agent")["status"], "active")

    def test_stale_hash_and_tag_guard(self):
        payload = {
            "mode": "new", "type_id": "rule", "title": "客户端测试规则：stale",
            "fields": {"statement": "stale hash", "severity": "soft", "rationale": "t", "check": "t"},
            "tags": [{"facet": "topic", "tag": "general"}],
            "sources": [{"source_type": "knowledge_entry", "title": "t", "locator": "t"}],
            "origin": {"kind": "manual", "ref": "test:stale", "summary_of_evidence": "t"},
        }
        out = self.kb.propose(payload)
        self.kb.mechanical_prereview(out["task_id"])
        with self.assertRaises(client.KBError) as ctx:
            self.kb._call(self.kb.kb.decide, out["task_id"], self.kb.admin, self.kb.token,
                          "approve", "x", "0" * 64, "webui")
        self.assertEqual(ctx.exception.code, "STALE_REVIEW")

    def test_tokens_are_usable(self):
        self.assertTrue(self.admin["rotated"])
        self.assertTrue(self.system["rotated"])
        self.assertFalse(client.ensure_admin_token()["rotated"])
        self.assertFalse(client.ensure_system_admin()["rotated"])
        from ukb.api import UKB
        kb = UKB(str(self.root))
        try:
            self.assertEqual(kb.auth("mdagent-system",
                                     (self.root / "system_auto.token").read_text(encoding="utf-8").strip()),
                             {"reviewer_id": "mdagent-system"})
        finally:
            kb.close()


if __name__ == "__main__":
    unittest.main()
