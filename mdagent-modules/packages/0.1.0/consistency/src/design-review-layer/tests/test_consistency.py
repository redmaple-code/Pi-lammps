"""Regression tests for the consistency engine + summary (no policy rules)."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_layer.consistency import build_consistency
from review_layer.models import ReviewReport
from review_layer.review import summarize


def base_intent():
    return {
        "case_id": "t",
        "objective": {"simulation_goal": "measure a0 via NPT"},
        "system": {"molecule": "Al", "size": {"atoms_target": 108}},
        "constraints": {"temperature_range": [300, 300]},
        "ensemble_sequence": [{"stage": "production", "ensemble": "nvt", "T_K": 300}],
        "key_params": {"timestep_fs": 1.0, "total_steps": 1000, "units": "metal",
                        "pair_style": "eam/alloy", "forcefield": {"preferred": "Al.eam"}},
        "experiment_design": {"experiment_type": "drawing"},
    }


def infile(npt=True, dt="0.001", steps=1000, extra=""):
    lines = ["units metal", "pair_style eam/alloy", "pair_coeff * * Al.eam", "timestep " + dt]
    if npt:
        lines.append("fix 1 all npt temp 300 300 0.1 iso 0 0 1.0")
    else:
        lines.append("fix 1 all nvt temp 300 300 0.1")
    lines.append("run %d" % steps)
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def verdicts(items):
    return [i.verdict for i in items]


class ConsistencyTests(unittest.TestCase):
    def test_npt_goal_sequence_nvt_infile_npt_is_plan_conflict_not_invalid(self):
        items = build_consistency(base_intent(), infile(npt=True))
        self.assertIn("plan_conflict", verdicts(items))
        self.assertNotIn("invalid", verdicts(items))

    def test_npt_goal_infile_without_npt_is_invalid(self):
        items = build_consistency(base_intent(), infile(npt=False))
        self.assertIn("invalid", verdicts(items))

    def test_timestep_mismatch_is_invalid(self):
        items = build_consistency(base_intent(), infile(npt=True, dt="0.002"))
        bad = [i for i in items if i.element == "时间步"][0]
        self.assertEqual(bad.verdict, "invalid")

    def test_steps_mismatch_is_invalid(self):
        items = build_consistency(base_intent(), infile(npt=True, steps=2000))
        bad = [i for i in items if i.element == "步数"][0]
        self.assertEqual(bad.verdict, "invalid")

    def test_fix_indent_in_drawing_is_invalid(self):
        items = build_consistency(base_intent(), infile(npt=True, extra="fix 2 die indent 10.0 0 0 0 0 8.0"))
        bad = [i for i in items if i.element == "命令用途"]
        self.assertTrue(any(i.verdict == "invalid" and "indent" in i.detail for i in bad))

    def test_fix_move_on_all_atoms_is_invalid(self):
        items = build_consistency(base_intent(), infile(npt=True, extra="fix 3 all move linear 0.1 0 0 units box"))
        bad = [i for i in items if i.element == "命令用途"]
        self.assertTrue(any(i.verdict == "invalid" and "全体原子" in i.detail for i in bad))

    def test_clean_case_all_ok_or_conflict_only(self):
        intent = base_intent()
        intent["objective"]["simulation_goal"] = "measure a0 via NVT"
        items = build_consistency(intent, infile(npt=False))
        self.assertTrue(all(i.verdict in ("ok", "plan_conflict") for i in items))


class SummaryTests(unittest.TestCase):
    def _report(self, items):
        r = ReviewReport(case_id="t", generated_at="now", artifacts=[], consistency=items,
                         findings=[], gate_status="GATED_FAIL", verdict="PENDING")
        return summarize(r)

    def test_plan_conflict_maps_to_needs_revision_and_no_findings(self):
        from review_layer.models import ConsistencyItem
        r = self._report([ConsistencyItem("c-1", "系综", "x", "y", [], "plan_conflict")])
        self.assertEqual(r.verdict, "NEEDS_REVISION")
        self.assertEqual(r.findings, [])

    def test_invalid_maps_to_fail(self):
        from review_layer.models import ConsistencyItem
        r = self._report([ConsistencyItem("c-1", "系综", "x", "y", [], "invalid")])
        self.assertEqual(r.verdict, "FAIL")
        self.assertEqual(r.findings, [])

    def test_all_ok_maps_to_pass(self):
        from review_layer.models import ConsistencyItem
        r = self._report([ConsistencyItem("c-1", "系综", "x", "y", [], "ok")])
        self.assertEqual(r.verdict, "PASS")
        self.assertEqual(r.findings, [])


if __name__ == "__main__":
    unittest.main()


class ExtendedConsistencyTests(unittest.TestCase):
    """New file-level checks: potential file, temperature, loading rate, observable."""

    def test_missing_potential_file_is_invalid(self):
        intent = base_intent()
        intent["key_params"]["forcefield"] = {"preferred": "NiPt.meam"}
        text = infile(npt=False)
        items = build_consistency(intent, text)
        bad = [i for i in items if i.element == "势函数"][0]
        self.assertEqual(bad.verdict, "invalid")
        self.assertIn("NiPt.meam", bad.detail)

    def test_temperature_mismatch_and_variable_resolution(self):
        intent = base_intent()
        intent["ensemble_sequence"] = [{"stage": "production", "ensemble": "nvt", "T_K": 500}]
        text = "units metal\npair_style eam/alloy\npair_coeff * * Al.eam\nvariable T equal 300\nfix 1 all nvt temp ${T} ${T} 0.1\nrun 1000\n"
        items = build_consistency(intent, text)
        bad = [i for i in items if i.element == "温度"][0]
        self.assertEqual(bad.verdict, "invalid")
        self.assertIn("300K", bad.detail)

    def test_loading_rate_matches_declared_erate(self):
        intent = base_intent()
        intent["cross_scale"] = {"loading": {"micro": "fix deform x erate 1×10⁻⁴ ps⁻¹ = 1×10⁸ s⁻¹"}}
        text = infile(npt=False, extra="fix 2 all deform 500 x erate 0.0001 units box remap x")
        items = build_consistency(intent, text)
        hit = [i for i in items if i.element == "加载速率"][0]
        self.assertEqual(hit.verdict, "ok")

    def test_loading_rate_mismatch_is_invalid(self):
        intent = base_intent()
        intent["cross_scale"] = {"loading": {"micro": "fix deform x erate 1×10⁻⁴ ps⁻¹ = 1×10⁸ s⁻¹"}}
        text = infile(npt=False, extra="fix 2 all deform 500 x erate 0.001 units box remap x")
        items = build_consistency(intent, text)
        hit = [i for i in items if i.element == "加载速率"][0]
        self.assertEqual(hit.verdict, "invalid")

    def test_declared_velocity_variable_not_used_in_move_is_flagged(self):
        intent = base_intent()
        intent["cross_scale"] = {"loading": {"micro": "上下 slab 相对速度 layer_velocity 推导 1×10⁷ s⁻¹"}}
        text = infile(npt=False, extra=(
            "variable layer_velocity equal 0.0005*(2*ly-2*0.2*ly)\n"
            "fix 6 top move linear -1 0 0 units box"))
        items = build_consistency(intent, text)
        hit = [i for i in items if i.element == "加载速率"][0]
        self.assertEqual(hit.verdict, "questionable")
        self.assertIn("layer_velocity", hit.detail)

    def test_observable_missing_strain_is_questionable(self):
        intent = base_intent()
        intent["objective"]["target_property"] = ["stress_strain_curve"]
        text = "units metal\npair_style eam/alloy\npair_coeff * * Al.eam\nfix 1 all nvt temp 300 300 0.1\nrun 1000\n"
        items = build_consistency(intent, text)
        hit = [i for i in items if i.element == "观测量"][0]
        self.assertEqual(hit.verdict, "questionable")
