#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""配置正确性机检(config_check + E4 接线)单测:解析/两类违规/正常配置/门禁集成。"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "evolution"))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "mcp"))

import stage_state   # noqa: E402
import stage_gate    # noqa: E402
import config_check  # noqa: E402
import surface_fixture  # noqa: E402

TENSILE_BAD = """units metal
boundary p p p
fix 1 all npt temp 300 300 0.1 iso 0.0 0.0 1.0
fix 2 all deform 1 x erate 1e-4 units box remap x
run 50000
"""
TENSILE_OK = """units metal
boundary p p p
fix 1 all npt temp 300 300 0.1 y 0.0 0.0 1.0 z 0.0 0.0 1.0
fix 2 all deform 1 x erate 1e-4 units box remap x
run 50000
"""
FRICTION_ONEWAY = """units metal
velocity mobile set 1.0 0.0 0.0 units box
fix 1 all nve
run 20000
"""
FRICTION_OSC = """units metal
fix drive mobile move wiggle 1.0 0.0 0.0 2.0 units box
fix 1 all nve
run 20000
"""


def load_entries():
    """直接从知识包取方法学约束条目(E4 的判据来源,不再经 MC 投影)。"""
    return [e for e in stage_gate.load_methodology_entries(None)
            if e.get("id") in ("methodo-001", "methodo-002")]


class TestConfigCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfgchk_")
        self.entries = load_entries()
        self.e1 = [e for e in self.entries if e["id"] == "methodo-001"][0]
        self.e2 = [e for e in self.entries if e["id"] == "methodo-002"][0]

    def _w(self, name, text):
        fp = os.path.join(self.tmp, name)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        return fp

    def test_parse_basic(self):
        fp = self._w("a.lmp", "# 注释\nunits metal  # 行内注释\nfix 1 all nvt &\n  temp 300 300\n")
        cmds = config_check.parse_input(fp)
        self.assertEqual([c["cmd"] for c in cmds], ["units", "fix"])
        self.assertIn("nvt", cmds[1]["args"])
        self.assertIn("temp 300 300", cmds[1]["args"])

    def test_npt_iso_plus_deform_violation(self):
        cmds = config_check.parse_input(self._w("t.lmp", TENSILE_BAD))
        r = config_check.eval_constraints(self.e1, cmds)
        self.assertTrue(r["triggered"])
        self.assertFalse(r["passed"])
        self.assertIn("拉不断", r["violations"][0]["note"])

    def test_directional_npt_ok(self):
        cmds = config_check.parse_input(self._w("t.lmp", TENSILE_OK))
        r = config_check.eval_constraints(self.e1, cmds)
        self.assertTrue(r["passed"])

    def test_recip_declared_but_oneway(self):
        cmds = config_check.parse_input(self._w("f.lmp", FRICTION_ONEWAY))
        r = config_check.eval_constraints(self.e2, cmds, "D2 研究目标:往复摩擦磨损")
        self.assertFalse(r["passed"])
        self.assertIn("单向", r["violations"][0]["note"])

    def test_recip_with_wiggle_ok(self):
        cmds = config_check.parse_input(self._w("f.lmp", FRICTION_OSC))
        r = config_check.eval_constraints(self.e2, cmds, "往复摩擦")
        self.assertTrue(r["passed"])

    def test_unreadable_fail_closed(self):
        r = config_check.eval_constraints(self.e1, None)
        self.assertTrue(r["cannot_verify"])
        self.assertIsNone(config_check.check_case_inputs(
            [self.e1], [os.path.join(self.tmp, "nope.lmp")], ""))


class TestGateE4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfgr16_")
        self.cid = "TASK-UT-CFG"
        stage_state.init_task(self.cid, data_root=self.tmp)
        surface_fixture.fill_all(self.tmp, self.cid)
        stage_state.lock_decisions(self.cid, {
            "D2_研究目标": "单轴拉伸", "D7_验收标准": ["应力应变曲线完整"]}, self.tmp)
        # 运行时知识包:把种子直接落到运行时目录并投影
        rt = os.path.join(self.tmp, "memory", "review_experience")
        os.makedirs(rt, exist_ok=True)
        src = os.path.join(os.path.dirname(_HERE),
                   "mcp", "review_experience", "methodology_seed.json")
        with open(src, encoding="utf-8") as f:
            pack = json.load(f)
        with open(os.path.join(rt, "m.json"), "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False)
        # workdir + 坏拉伸输入
        wd = os.path.join(self.tmp, "work", self.cid)
        os.makedirs(wd, exist_ok=True)
        with open(os.path.join(wd, "in.t.lmp"), "w", encoding="utf-8") as f:
            f.write(TENSILE_BAD)

    def _rec(self, **over):
        r = {"schema_version": 1, "case_id": self.cid, "stage": "BUILD",
             "gate": "G-BUILD", "decision": "PASS", "scope": "ut",
             "risk_level": "low", "evidence_refs": [{"ref": "$x"}],
             "acceptance_checks": [], "summary": "ut",
             "input_files": ["in.t.lmp"]}
        r.update(over)
        return r

    def test_bad_tensile_blocked_at_build(self):
        ctx = stage_gate.build_context(self.cid, data_root=self.tmp)
        ctx["facts"] = {"x": 1}
        res = stage_gate.validate_decision_record(self._rec(), ctx)
        self.assertEqual(res["result"], "BLOCK", res["reasons"])
        self.assertTrue(any(r.startswith("E4:violation:methodo-001")
                            for r in res["reasons"]), res["reasons"])
        self.assertIn("拉不断", " ".join(res["reasons"]))

    def test_missing_input_files_blocks(self):
        # 没有输入文件就无法独立复算：按缺项拒（不再静默跳过）
        ctx = stage_gate.build_context(self.cid, data_root=self.tmp)
        res = stage_gate.validate_decision_record(self._rec(input_files=[]), ctx)
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any("E4:no-input-declared" in r for r in res["reasons"]),
                        res["reasons"])

    def test_good_tensile_passes(self):
        wd = os.path.join(self.tmp, "work", self.cid)
        with open(os.path.join(wd, "in.t.lmp"), "w", encoding="utf-8") as f:
            f.write(TENSILE_OK)
        ctx = stage_gate.build_context(self.cid, data_root=self.tmp)
        ctx["facts"] = {"x": 1}
        res = stage_gate.validate_decision_record(self._rec(), ctx)
        self.assertEqual(res["result"], "GO", res["reasons"])


if __name__ == "__main__":
    unittest.main()
