#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""v2 内容/知识层单测:assessment 引擎锚判级 + E2/E3 门禁接线。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "evolution"))

import stage_state   # noqa: E402
import stage_gate    # noqa: E402
import assessment    # noqa: E402
import surface_fixture  # noqa: E402
import content_check  # noqa: E402


def base_rec(**over):
    r = {"schema_version": 1, "case_id": "TASK-UT-V2", "stage": "PLAN",
         "gate": "G-PLAN", "decision": "PASS", "scope": "ut", "risk_level": "low",
         "evidence_refs": [], "semantic_surface_touched": [],
         "mandatory_check_results": {}, "acceptance_checks": [],
         "confidence": "medium", "required_next_actor": "coordinator",
         "summary": "ut"}
    r.update(over)
    return r


class TestAssessmentEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v2_assess_")
        # 种子锚即 mcp/review_experience/anchors_seed.json(engine 自读)
        cid = "TASK-UT-V2"
        stage_state.init_task(cid, risk_level="low", data_root=self.tmp)
        stage_state.lock_decisions(cid, {
            "D5_关键参数": "dt=100fs;其余同前",
            "D7_验收标准": ["偏差<0.01", "跑通就好"]}, self.tmp)

    def test_dt_critical(self):
        rec, ref = assessment.assess_plan("TASK-UT-V2", self.tmp)
        pa1 = [f for f in rec["findings"] if f["id"] == "PA1"][0]
        self.assertEqual(pa1["verdict"], "critical")     # dt*=0.046 > 0.02
        self.assertEqual(rec["proceed"], "not-recommended")
        self.assertIn("PA1", rec["critical_open"])

    def test_dt_concern_25fs(self):
        stage_state.revise_decision("TASK-UT-V2", "D5_关键参数", "ut", "ut", self.tmp)
        doc = stage_state.read_decisions("TASK-UT-V2", self.tmp)
        doc["D5_关键参数"]["value"] = "dt=25fs"
        with open(os.path.join(stage_state.task_dir("TASK-UT-V2", self.tmp),
                               "decisions.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        rec, _ = assessment.assess_plan("TASK-UT-V2", self.tmp)
        pa1 = [f for f in rec["findings"] if f["id"] == "PA1"][0]
        self.assertEqual(pa1["verdict"], "concern")      # 0.005<dt*=0.0116≤0.02
        self.assertEqual(rec["critical_open"], [])

    def test_rdf_anchor(self):
        cid = "TASK-UT-V2"
        stage_state.init_task(cid, data_root=self.tmp)  # 幂等
        facts = {"rdf_peak_A": 4.99}
        rec, _ = assessment.assess_results(cid, facts, self.tmp)
        ra2 = [f for f in rec["findings"] if f["id"] == "RA2"][0]
        self.assertEqual(ra2["verdict"], "critical")    # 4.99 > 1.15σ=3.92
        facts2 = {"rdf_peak_A": 3.719}
        rec2, _ = assessment.assess_results(cid, facts2, self.tmp)
        self.assertEqual([f for f in rec2["findings"] if f["id"] == "RA2"][0]["verdict"],
                         "sound")

    def test_acceptance_quantifiability(self):
        rec, _ = assessment.assess_plan("TASK-UT-V2", self.tmp)
        pa2 = [f for f in rec["findings"] if f["id"] == "PA2"][0]
        self.assertEqual(pa2["verdict"], "concern")     # "跑通就好"不可量化


class TestE2E3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v2_gate_")
        self.cid = "TASK-UT-GV2"
        stage_state.init_task(self.cid, data_root=self.tmp)
        surface_fixture.fill_all(self.tmp, self.cid)
        stage_state.lock_decisions(
            self.cid, {"D7_验收标准": ["x<0.1"]}, self.tmp,
            basis={"D7_验收标准": {"kind": "literature",
                                   "ref": "std:Allen-Tildesley-1989-Computer-Simulation-of-Liquids"}})

    def _mk_assessment(self, phase, critical):
        d = os.path.join(stage_state.task_dir(self.cid, self.tmp), "assessments")
        os.makedirs(d, exist_ok=True)
        name = "%s-%s.json" % (len(os.listdir(d)) + 1, phase)
        doc = {"schema_version": 1, "case_id": self.cid, "phase": phase,
               "findings": [], "critical_open": ["PA1"] if critical else [],
               "proceed": "not-recommended" if critical else "recommended"}
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return "assessment:assessments/" + name

    def test_e3_missing_blocks(self):
        # 没有评估记录 → E3 缺项即拒（不再静默跳过）
        ctx = stage_gate.build_context(self.cid, data_root=self.tmp)
        res = stage_gate.validate_decision_record(base_rec(), ctx)
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any(r.startswith("E3:assessment-missing") for r in res["reasons"]))

    def test_e3_critical_blocks(self):
        self._mk_assessment("plan", critical=True)
        res = stage_gate.validate_decision_record(
            base_rec(), stage_gate.build_context(self.cid, data_root=self.tmp))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any(r.startswith("E3:critical:PA1") for r in res["reasons"]),
                        res["reasons"])

    def test_e3_clean_passes(self):
        self._mk_assessment("plan", critical=False)
        res = stage_gate.validate_decision_record(
            base_rec(), stage_gate.build_context(self.cid, data_root=self.tmp))
        self.assertEqual(res["result"], "GO", res["reasons"])

    def test_e2_factchecks(self):
        d = os.path.join(stage_state.task_dir(self.cid, self.tmp), "factchecks")
        os.makedirs(d, exist_ok=True)
        good = {"schema_version": 1, "case_id": self.cid, "field": "a0",
                "claimed": 3.615, "measured": 3.6150, "verdict": "PASS"}
        bad = dict(good, field="ec", claimed=-3.5, measured=-2.0, verdict="FAIL")
        for n, doc in (("1-a0.json", good), ("2-ec.json", bad)):
            with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                json.dump(doc, f)
        acc = [{"item": "a0", "claimed": 3.615, "passed": True,
                "factcheck": "factcheck:factchecks/1-a0.json"},
               {"item": "ec", "claimed": -3.5, "passed": False,
                "factcheck": "factcheck:factchecks/2-ec.json"},
               {"item": "no-fc", "claimed": 1.0, "passed": True}]
        self._mk_assessment("results", critical=False)
        ctx = stage_gate.build_context(self.cid, data_root=self.tmp)
        res = stage_gate.validate_decision_record(
            base_rec(stage="ANALYZE", gate="G-ANALYZE", acceptance_checks=acc,
                     rollback_target="PLAN"), ctx)
        self.assertTrue(any("E2:claim-failed" in r and "ec" in r
                            for r in res["reasons"]), res["reasons"])
        self.assertTrue(any("E2:claim-unevidenced" in r and "no-fc" in r
                            for r in res["reasons"]), res["reasons"])
        self.assertEqual(res["result"], "BLOCK")

    def test_content_check_roundtrip(self):
        log = os.path.join(self.tmp, "log_probe.lammps")
        with open(log, "w", encoding="utf-8") as f:
            f.write("EVLIVE_A0 3.6150\nEVLIVE_EC -3.5400\n")
        rec, ref = content_check.check(
            self.cid, "a0", 3.615, {"path": log, "marker": "EVLIVE_A0"},
            tolerance=0.005, data_root=self.tmp)
        self.assertEqual(rec["verdict"], "PASS")
        self.assertEqual(rec["measured"], 3.615)
        rec2, _ = content_check.check(
            self.cid, "a0", 3.70, {"path": log, "marker": "EVLIVE_A0"},
            tolerance=0.005, data_root=self.tmp)
        self.assertEqual(rec2["verdict"], "FAIL")
        self.assertIsNone(content_check.read_factcheck(
            self.cid, "factchecks/nope.json", self.tmp))


if __name__ == "__main__":
    unittest.main()
