# -*- coding: utf-8 -*-
"""evidence_grade 单测：六级分级、有效等级修正、门槛、冲突检测、追问指纹。"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SRC, "evolution"))
sys.path.insert(0, os.path.join(SRC, "mcp"))

import evidence_grade as eg  # noqa: E402


class TestSourceGrade(unittest.TestCase):
    def test_six_families(self):
        cases = {
            "user:u-1": "A",
            "paper:10.1234/x": "B",
            "std:gb-1234": "B",
            "$density": "C",
            "LEDGER:r1:success": "C",
            "factcheck:fc-1": "C",
            "tool:sim_exec:20261019T000000": "C",
            "case:LIT-01": "D",
            "atom:a-1": "E",
            "exp:anchor-md-001": "E",
            "随便一句估算": "F",
        }
        for ref, want in cases.items():
            self.assertEqual(eg.source_grade(ref), want, ref)

    def test_threshold_shape(self):
        self.assertEqual(eg.THRESHOLDS["high"]["design"]["min"], 4)  # A 或 B
        self.assertEqual(eg.THRESHOLDS["medium"]["design"]["min"], 2)
        self.assertEqual(eg.THRESHOLDS["low"]["design"]["min"], 1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx = {"case_id": "C1", "state": {"risk_level": "medium"},
                    "facts": {"a": 1.0}, "ledger": [], "atoms": [],
                    "exp_index": {}, "cases": {}, "confirmations": [],
                    "factchecks": {}, "toolcalls": []}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestResolve(Base):
    def test_human_confirmation_verified(self):
        self.ctx["confirmations"] = [{"id": "u-1", "value": 3.6,
                                      "scope": "C1", "reason": "用户指定"}]
        g = eg.resolve("user:u-1", self.ctx)
        self.assertTrue(g["verified"])
        self.assertTrue(g["countable"])
        self.assertEqual(g["grade"], "A")

    def test_human_confirmation_absent(self):
        g = eg.resolve("user:u-missing", self.ctx)
        self.assertFalse(g["verified"])
        self.assertFalse(g["countable"])

    def test_human_scope_mismatch_not_applicable(self):
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "OTHER"}]
        g = eg.resolve("user:u-1", self.ctx)
        self.assertTrue(g["verified"])
        self.assertFalse(g["applicable"])
        self.assertFalse(g["countable"])

    def test_paper_malformed(self):
        self.assertFalse(eg.resolve("paper:x", self.ctx)["verified"])

    def test_paper_family_mismatch(self):
        self.ctx["family"] = "liquid-md"
        g = eg.resolve("paper:10.1/x", self.ctx,
                       {"ref": "paper:10.1/x", "meta": {"family": "elastic-constants"}})
        self.assertTrue(g["verified"])
        self.assertFalse(g["applicable"])

    def test_fact_present_and_absent(self):
        self.assertTrue(eg.resolve("$a", self.ctx)["countable"])
        self.assertFalse(eg.resolve("$b", self.ctx)["verified"])

    def test_toolcall_is_soft_only(self):
        self.ctx["toolcalls"] = [{"name": "sim_exec", "ts": "T1"}]
        g = eg.resolve("tool:sim_exec:T1", self.ctx)
        self.assertTrue(g["verified"])
        self.assertFalse(g["countable"])  # 只证执行，不支撑设计依据

    def test_case_applicability(self):
        self.ctx["cases"] = {"LIT-01": {"case_id": "LIT-01", "family": "elastic-constants",
                                        "result_summary": "C11=167GPa"}}
        self.ctx["family"] = "elastic-constants"
        self.assertTrue(eg.resolve("case:LIT-01", self.ctx)["countable"])
        self.ctx["family"] = "liquid-md"
        g = eg.resolve("case:LIT-01", self.ctx)
        self.assertFalse(g["applicable"])
        self.assertFalse(g["countable"])

    def test_atom_retracted(self):
        self.ctx["atoms"] = [{"id": "a-1", "retracted": True}]
        self.assertFalse(eg.resolve("atom:a-1", self.ctx)["verified"])

    def test_exp_canary_soft_only(self):
        self.ctx["exp_index"] = {"x": {"status": "canary"}}
        g = eg.resolve("exp:x", self.ctx)
        self.assertTrue(g["verified"])
        self.assertFalse(g["countable"])

    def test_estimate_never_countable(self):
        self.assertFalse(eg.resolve("经验上大约 3.6", self.ctx)["countable"])


class TestThreshold(Base):
    def test_high_design_requires_a_or_b(self):
        graded = [eg.resolve("atom:a", self.ctx)]  # E 级
        self.ctx["atoms"] = [{"id": "a"}]
        graded = [eg.resolve("atom:a", self.ctx)]
        ok, msg = eg.meets_threshold("high", "design", graded)
        self.assertFalse(ok, msg)
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "C1"}]
        ok, msg = eg.meets_threshold("high", "design", [eg.resolve("user:u-1", self.ctx)])
        self.assertTrue(ok, msg)  # 高风险允许纯人工

    def test_medium_design_accepts_case(self):
        self.ctx["cases"] = {"C0": {"case_id": "C0", "result_summary": "x"}}
        ok, _ = eg.meets_threshold("medium", "design", [eg.resolve("case:C0", self.ctx)])
        self.assertTrue(ok)

    def test_result_requires_factcheck(self):
        self.ctx["facts"] = {"a": 1.0}
        ok, msg = eg.meets_threshold("high", "result", [eg.resolve("$a", self.ctx)])
        self.assertFalse(ok, msg)
        self.assertIn("factcheck", msg)


class TestConflicts(Base):
    def _slot(self, value, ref, reason=""):
        return {"value": value, "basis": {"kind": "user", "ref": ref,
                                          "value": value, "reason": reason,
                                          "scope": "C1"}}

    def test_value_conflict_detected(self):
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "C1"}]
        slot = self._slot(3.6, "user:u-1")
        graded = [{"ref": "paper:10.1/x", "grade": "B", "verified": True,
                   "value": 3.0, "countable": True, "applicable": True,
                   "fresh": True, "note": "", "source": "B", "soft_only": False}]
        cs = eg.detect_conflicts("D1", slot, graded, self.ctx)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["type"], "value")

    def test_no_conflict_within_tolerance(self):
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "C1"}]
        slot = self._slot(3.615, "user:u-1")
        graded = [{"ref": "paper:10.1/x", "grade": "B", "verified": True,
                   "value": 3.618, "countable": True, "applicable": True,
                   "fresh": True, "note": "", "source": "B", "soft_only": False}]
        self.assertEqual(eg.detect_conflicts("D1", slot, graded, self.ctx), [])

    def test_fingerprint_stable_and_distinct(self):
        slot = self._slot(3.6, "user:u-1")
        cs = [{"type": "value", "ref": "paper:x", "summary": "s"}]
        f1 = eg.fingerprint("D1", slot, cs)
        f2 = eg.fingerprint("D1", dict(slot), list(cs))
        self.assertEqual(f1, f2)
        cs2 = [{"type": "value", "ref": "paper:y", "summary": "s"}]
        self.assertNotEqual(f1, eg.fingerprint("D1", slot, cs2))

    def test_needs_reason(self):
        self.assertTrue(eg.needs_reason(self._slot(1, "user:u-1")))
        self.assertFalse(eg.needs_reason(self._slot(1, "user:u-1", "用户要求")))

    def test_second_confirmation(self):
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "C1", "confirmed_twice": True}]
        self.assertTrue(eg.is_second_confirmed(self._slot(1, "user:u-1"), self.ctx))
        # 另一条记录显式 confirms 同一条输入 → 二次确认
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "C1"},
                                     {"id": "u-2", "confirms": "u-1"}]
        slot2 = {"value": 1, "basis": {"kind": "user", "ref": "user:u-1"}}
        self.assertTrue(eg.is_second_confirmed(slot2, self.ctx))
        # 两次不同输入的确认不算二次确认
        self.ctx["confirmations"] = [{"id": "u-1", "scope": "C1"},
                                     {"id": "u-2", "scope": "C1"}]
        self.assertFalse(eg.is_second_confirmed(slot2, self.ctx))


if __name__ == "__main__":
    unittest.main()
