# -*- coding: utf-8 -*-
"""stage_gate v2.2 单测：E1-E5、闸绑定、缺项即拒、人工覆盖追问、fail-closed。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SRC, "evolution"))
sys.path.insert(0, os.path.join(SRC, "mcp"))

import stage_state  # noqa: E402
import stage_gate  # noqa: E402
import surface_fixture  # noqa: E402


def _wjson(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.dr = tempfile.mkdtemp()
        os.environ["MDSIM_DATA_ROOT"] = self.dr
        self.cid = "CASE-1"
        stage_state.init_task(self.cid, risk_level="medium", data_root=self.dr)
        self.td = stage_state.task_dir(self.cid, self.dr)

    def tearDown(self):
        os.environ.pop("MDSIM_DATA_ROOT", None)
        shutil.rmtree(self.dr, ignore_errors=True)

    def rec(self, gate, **kw):
        r = {"schema_version": 1, "case_id": self.cid, "gate": gate,
             "stage": {"G-PLAN": "PLAN", "G-BUILD": "BUILD", "G-CONFIG": "CONFIG",
                       "G-EXEC": "EXEC", "G-ANALYZE": "ANALYZE"}.get(gate, "PLAN"),
             "decision": "PASS", "summary": "t", "evidence_refs": [],
             "acceptance_checks": [], "input_files": []}
        r.update(kw)
        return r

    def gate(self, record, **kw):
        phase = {"G-PLAN": "design", "G-BUILD": "build", "G-CONFIG": "build",
                 "G-EXEC": "exec", "G-ANALYZE": "analyze"}.get(record.get("gate"))
        if phase and not kw.pop("skip_surface", False):
            surface_fixture.fill_phase(self.dr, self.cid, phase)
        return stage_gate.gate_and_log(self.cid, record, data_root=self.dr, **kw)

    def lock(self, slots, basis=None):
        stage_state.lock_decisions(self.cid, slots, data_root=self.dr, basis=basis)
        st = stage_state.read_state(self.cid, self.dr)
        st["locked_decisions"] = list(slots.keys())
        stage_state._atomic_write_json(stage_state.state_path(self.cid, self.dr), st)

    def add_assessment(self, phase, findings=None, crit=None):
        d = os.path.join(self.td, "assessments")
        os.makedirs(d, exist_ok=True)
        _wjson(os.path.join(d, "a-%s.json" % phase),
               {"schema_version": 1, "case_id": self.cid, "phase": phase,
                "findings": findings or [], "critical_open": crit or [],
                "overall": "sound", "proceed": "recommended"})

    def add_atom(self, aid, retracted=False):
        fp = os.path.join(self.dr, "memory", "atoms.jsonl")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": aid, "retracted": retracted}) + "\n")

    def add_confirmation(self, uid, scope=None, reason="", twice=False, confirms=""):
        d = os.path.join(self.td, "confirmations")
        os.makedirs(d, exist_ok=True)
        doc = {"id": uid, "scope": scope or self.cid, "reason": reason,
               "value": 3.6, "ts": "2026-10-19T00:00:00Z"}
        if twice:
            doc["confirmed_twice"] = True
        if confirms:
            doc["confirms"] = confirms
        _wjson(os.path.join(d, "%s.json" % uid), doc)


class TestGateBinding(Base):
    def test_each_gate_runs_only_bound_rules(self):
        self.assertEqual(stage_gate.GATE_RULES["G-BUILD"], ("E4", "E6"))
        self.assertEqual(stage_gate.GATE_RULES["G-CONFIG"], ("E4", "E6"))
        self.assertEqual(stage_gate.GATE_RULES["G-EXEC"], ("E2", "E6"))
        self.assertEqual(stage_gate.GATE_RULES["G-PLAN"], ("E1", "E3", "E6"))
        self.assertEqual(stage_gate.GATE_RULES["G-ANALYZE"],
                         ("E2", "E3", "E5", "E6"))

    def test_unbound_gate_skips_evidence_rules(self):
        r = self.rec("G-PLAN")
        r["gate"] = "G-CUSTOM"
        res = self.gate(r)
        self.assertEqual(res["result"], "GO")
        self.assertIn("未绑定证据规则", res["rule_trace"].get("_gate", ""))

    def test_build_gate_does_not_run_e3(self):
        wd = os.path.join(self.dr, "work", self.cid)
        os.makedirs(wd, exist_ok=True)
        with open(os.path.join(wd, "in.x"), "w", encoding="utf-8") as f:
            f.write("minimize 0 0 0 0\n")
        res = self.gate(self.rec("G-BUILD", input_files=["in.x"]))
        self.assertNotIn("E3", " ".join(res["reasons"]))


class TestE1DesignEvidence(Base):
    def test_no_design_at_plan_gate_blocks(self):
        # G-PLAN 的意义就是"批准设计"：没有设计记录就没有可批准的方案
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any("E1:no-design-decisions" in r for r in res["reasons"]))

    def test_insufficient_evidence_blocks(self):
        self.lock({"D1_方法": "NVT 平衡"})
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any(r.startswith("E1:evidence-insufficient")
                            for r in res["reasons"]))

    def test_paper_satisfies_medium_design(self):
        self.lock({"D1_方法": "NVT 平衡"},
                  basis={"D1_方法": {"kind": "literature",
                                     "ref": "paper:10.1063/1.1234"}})
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "GO", res["reasons"])

    def test_high_risk_requires_a_or_b(self):
        stage_state.init_task("CASE-H", risk_level="high", data_root=self.dr)
        self.cid = "CASE-H"
        self.td = stage_state.task_dir(self.cid, self.dr)
        self.add_atom("a-1")
        self.lock({"D1_方法": "NVT"},
                  basis={"D1_方法": {"kind": "prior_case", "ref": "atom:a-1"}})
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any("E1:evidence-insufficient" in r for r in res["reasons"]))

    def test_high_risk_human_only_allowed(self):
        stage_state.init_task("CASE-H2", risk_level="high", data_root=self.dr)
        self.cid = "CASE-H2"
        self.td = stage_state.task_dir(self.cid, self.dr)
        self.add_confirmation("u-1", reason="用户明确要求该方案")
        self.lock({"D1_方法": "NVT"},
                  basis={"D1_方法": {"kind": "user", "ref": "user:u-1",
                                     "value": 3.6, "reason": "用户明确要求该方案"}})
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "GO", res["reasons"])

    def test_override_without_reason_asks(self):
        self.add_confirmation("u-1")
        self.lock({"D1_方法": "NVT"},
                  basis={"D1_方法": {"kind": "user", "ref": "user:u-1", "value": 3.6}})
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any("E1:override-reason-missing" in r
                            for r in res["reasons"]))
        self.assertTrue(res["conflicts"], "应产出冲突包供协调者追问")

    def test_second_confirmation_bypasses_question(self):
        self.add_confirmation("u-1", twice=True)
        self.lock({"D1_方法": "NVT"},
                  basis={"D1_方法": {"kind": "user", "ref": "user:u-1", "value": 3.6}})
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "GO", res["reasons"])

    def test_estimate_requires_assessment_backing(self):
        # 估算型依据 + 没有任何评估记录 → 估算未被评估背书
        self.lock({"D1_参数": "0.9Tm"},
                  basis={"D1_参数": {"kind": "physics_estimate", "ref": "经验估算"}})
        res = self.gate(self.rec("G-PLAN"))
        self.assertTrue(any("E1:estimate-unassessed" in r for r in res["reasons"]),
                        res["reasons"])

    def test_invalid_basis_kind(self):
        self.lock({"D1_方法": "NVT"},
                  basis={"D1_方法": {"kind": "vibes", "ref": "paper:10.1/x"}})
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertTrue(any("E1:basis-invalid" in r for r in res["reasons"]))


class TestE3Physics(Base):
    def _design(self):
        self.lock({"D1_方法": "NVT 平衡"},
                  basis={"D1_方法": {"kind": "literature",
                                     "ref": "paper:10.1063/1.1234"}})

    def test_missing_assessment_blocks_plan(self):
        self._design()
        res = self.gate(self.rec("G-PLAN"))
        self.assertTrue(any("E3:assessment-missing" in r for r in res["reasons"]))

    def test_critical_blocks(self):
        self._design()
        self.add_assessment("plan", crit=["mdev-general-dt"])
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any("E3:critical" in r for r in res["reasons"]))

    def test_clean_assessment_passes(self):
        self._design()
        self.add_assessment("plan")
        res = self.gate(self.rec("G-PLAN"))
        self.assertEqual(res["result"], "GO", res["reasons"])


class TestE4Methodology(Base):
    def _write_input(self, text):
        wd = os.path.join(self.dr, "work", self.cid)
        os.makedirs(wd, exist_ok=True)
        fp = os.path.join(wd, "in.test")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        return "in.test"

    def test_violation_blocks(self):
        f = self._write_input("fix 1 all npt temp 300 300 0.1 iso 0 0 1.0\n"
                              "fix 2 all deform 1 x erate 1e-4 units box\n")
        res = self.gate(self.rec("G-BUILD", input_files=[f]))
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(any("E4:violation" in r for r in res["reasons"]),
                        res["reasons"])

    def test_clean_input_passes(self):
        f = self._write_input("fix 1 all nvt temp 300 300 0.1\n"
                              "fix 2 all deform 1 x erate 1e-4 units box\n")
        res = self.gate(self.rec("G-BUILD", input_files=[f]))
        self.assertEqual(res["result"], "GO", res["reasons"])

    def test_no_input_declared_blocks(self):
        res = self.gate(self.rec("G-BUILD"))
        self.assertTrue(any("E4:no-input-declared" in r for r in res["reasons"]))


class TestE2Computation(Base):
    def test_exec_requires_inputs(self):
        res = self.gate(self.rec("G-EXEC"))
        self.assertTrue(any("E2:input-untraceable" in r for r in res["reasons"]))

    def test_analyze_numeric_claim_needs_factcheck(self):
        res = self.gate(self.rec("G-ANALYZE", acceptance_checks=[
            {"item": "a0=3.615", "claimed": 3.615, "passed": True}]))
        self.assertTrue(any("E2:claim-unevidenced" in r for r in res["reasons"]))

    def test_analyze_factcheck_pass(self):
        d = os.path.join(self.td, "factchecks")
        os.makedirs(d, exist_ok=True)
        _wjson(os.path.join(d, "fc-1.json"),
               {"field": "a0", "claimed": 3.615, "measured": 3.615,
                "verdict": "PASS", "source": {}})
        self.add_assessment("results")
        res = self.gate(self.rec("G-ANALYZE", acceptance_checks=[
            {"item": "a0=3.615", "claimed": 3.615, "passed": True,
             "factcheck": "factcheck:fc-1"}]))
        self.assertFalse(any("E2:claim" in r for r in res["reasons"]), res["reasons"])

    def test_analyze_factcheck_fail(self):
        d = os.path.join(self.td, "factchecks")
        os.makedirs(d, exist_ok=True)
        _wjson(os.path.join(d, "fc-2.json"),
               {"field": "a0", "claimed": 3.7, "measured": 3.615,
                "verdict": "FAIL", "source": {}})
        self.add_assessment("results")
        res = self.gate(self.rec("G-ANALYZE", acceptance_checks=[
            {"item": "a0=3.7", "claimed": 3.7, "passed": True,
             "factcheck": "factcheck:fc-2"}]))
        self.assertTrue(any("E2:claim-failed" in r for r in res["reasons"]))


class TestE5Completeness(Base):
    def test_missing_acceptance_item(self):
        self.lock({"验收标准": ["a0=3.615", "Ec=-3.54"]})
        self.add_assessment("results")
        res = self.gate(self.rec("G-ANALYZE", acceptance_checks=[
            {"item": "a0=3.615", "passed": True}]))
        self.assertTrue(any("E5:acceptance-not-covered" in r for r in res["reasons"]))

    def test_failed_item_needs_rollback(self):
        self.add_assessment("results")
        res = self.gate(self.rec("G-ANALYZE", acceptance_checks=[
            {"item": "a0", "passed": False}]))
        self.assertTrue(any("E5:no-rollback-on-fail" in r for r in res["reasons"]))


class TestFailClosed(Base):
    def test_unusable_record(self):
        res = stage_gate.gate_and_log(self.cid, {"gate": "G-PLAN"}, data_root=self.dr)
        self.assertEqual(res["result"], "BLOCK")

    def test_internal_error_blocks(self):
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")
        res = stage_gate.validate_decision_record(Boom(gate="G-PLAN"), {})
        self.assertEqual(res["result"], "BLOCK")
        self.assertTrue(res["reasons"][0].startswith("E0:internal-error"))

    def test_revise_is_hold(self):
        res = self.gate(self.rec("G-PLAN", decision="REVISE"))
        self.assertEqual(res["result"], "HOLD")

    def test_blocked_is_block(self):
        res = self.gate(self.rec("G-PLAN", decision="BLOCKED"))
        self.assertEqual(res["result"], "BLOCK")

    def test_gate_log_appended(self):
        self.gate(self.rec("G-PLAN"))
        rows = stage_state.read_gate_log(self.cid, 10, self.dr)
        self.assertTrue(rows)
        self.assertIn("rule_trace", rows[-1])
        self.assertIn("evidence", rows[-1])


class TestEvidenceReport(Base):
    def test_grades_reported(self):
        self.add_atom("a-1")
        res = self.gate(self.rec("G-PLAN", evidence_refs=[
            "atom:a-1", "paper:10.1/x", "随便估算"]))
        by_ref = {e["ref"]: e for e in res["evidence"]}
        self.assertEqual(by_ref["atom:a-1"]["grade"], "E")
        self.assertEqual(by_ref["paper:10.1/x"]["grade"], "B")
        self.assertEqual(by_ref["随便估算"]["grade"], "F")
        self.assertFalse(by_ref["随便估算"]["countable"])


if __name__ == "__main__":
    unittest.main()
