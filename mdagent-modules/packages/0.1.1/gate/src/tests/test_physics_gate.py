# -*- coding: utf-8 -*-
"""度量与结构层回归:physics_gate 只测量不判断(2026-10-04 架构)。

运行(仓库根或本目录均可):
    python -m unittest tests.test_physics_gate -v
被测对象:mcp/physics_gate.py(纯测量:解析/度量/衍生比值/结构存在性)。
判断层(AI 审阅)见 tests/test_design_review.py。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

import physics_gate  # noqa: E402


def base_intent():
    return {
        "schema_version": "2.1",
        "case_id": "t_case",
        "status": "CONFIRMED",
        "system": {"molecule": "碳氢框架", "target_elements": ["C", "H"],
                   "size": {"atoms_target": 6000, "density_target": 0.25},
                   "declared_net_charge": 0.0},
        "constraints": {"temperature_range": [300, 300],
                        "protocol_flags": {"is_quench": False, "has_heating_ramp": False}},
        "ensemble_sequence": [
            {"stage": "minimize"},
            {"stage": "pre_equil", "ensemble": "nvt", "thermostat": "langevin"},
            {"stage": "production", "ensemble": "npt"},
        ],
        "key_params": {"timestep_fs": 0.25, "total_steps": 600000, "cores": 48,
                       "forcefield": {"preferred": "ffield.reax.CHO"},
                       "units": "real", "pair_style": "reax/c", "charge_method": "qeq"},
    }


def friction_intent():
    it = base_intent()
    it.setdefault("objective", {})["simulation_goal"] = "高熵合金摩擦"
    it["experiment_design"] = {
        "experiment_type": "friction_indentation",
        "setup": {"indenter": {"shape": "sphere", "radius_A": 50.0},
                  "indentation_depth_A": 5.0, "sliding_velocity_m_s": 20.0,
                  "sliding_length_A": 80.0,
                  "sample": {"dimensions_A": [200.0, 200.0, 80.0]},
                  "box_l_A": 200.0, "pair_cutoff_A": 10.0,
                  "thermostat": {"region": "底部边界层"}},
        "design_rationale": "浅压",
    }
    return it


def write_data_file(atoms, box=20.0, style="charge"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "system.data")
    lines = ["LAMMPS data file", "", "%d atoms" % len(atoms),
             "1.0 %.1f xlo xhi" % box, "1.0 %.1f ylo yhi" % box, "1.0 %.1f zlo zhi" % box,
             "", "Masses", "", "1 12.011", "2 1.008", "",
             "Atoms # %s" % style, ""]
    for a in atoms:
        lines.append(" ".join(str(x) for x in a))
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def write_ffield(elements):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "ffield.reax")
    lines = ["! minimal test ffield", str(len(elements))]
    for e in elements:
        lines.append("%s 6 12.011 1.0 0.1 0.0 0.0 0.0" % e)
    lines += ["33", "0.0 0.0 0.0"] * 2
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


class TestEffectiveGradeRobustness(unittest.TestCase):
    """字符串段落不得炸分级判定(2026-10-23,webui 示例数据暴露 update_intent 直调路径)。"""

    def test_string_sections_survive(self):
        from physics_gate import effective_grade
        intent = {"source": "来自一次对话(自由文本)",
                  "objective": "看看弛豫稳不稳",
                  "experiment_reference": "某文献",
                  "comparability": "对照原文表1"}
        grade, reasons = effective_grade(intent)
        self.assertEqual(grade, "exploratory")
        self.assertEqual(reasons, [])

    def test_extract_facts_string_sections_survive(self):
        from physics_gate import extract_facts
        # source/系统段为自由文本时度量不得崩(2026-10-23 示例D 暴露,曾致 validate 必 REVIEW_ERROR)
        facts = extract_facts({"source": "来自一次对话(自由文本)",
                               "system": "铜, 室温",
                               "material_model": "fcc Cu",
                               "key_params": "dt=1fs"}, ctx={})
        self.assertIsInstance(facts, dict)
        self.assertIs(facts.get("continuation"), False)

    def test_structured_semantics_unchanged(self):
        from physics_gate import effective_grade
        grade, reasons = effective_grade({
            "source": {"kind": "paper"},
            "experiment_reference": {"kind": "paper", "citation": "x"},
            "comparability": {"targets": [{"property": "a0"}]},
            "objective": {"simulation_goal": "复现文献晶格常数"}})
        self.assertEqual(grade, "paper_grade")
        self.assertIn("source.kind=paper", reasons)
        self.assertTrue(any(r.startswith("experiment_reference.kind") for r in reasons))


class TestParsers(unittest.TestCase):
    """纯测量:文件解析(原样保留的行为)。"""

    def test_parse_data_charge_style(self):
        p = write_data_file([(1, 1, 1.1, 1.0, 1.0, 1.0), (2, 2, 0.2, 1.0, 1.0, 5.0)])
        d = physics_gate.parse_data_file(p)
        self.assertEqual(len(d["coords"]), 2)
        self.assertEqual(d["charges"], [1.1, 0.2])
        self.assertEqual(len(d["box"]), 3)

    def test_parse_data_missing_returns_none(self):
        self.assertIsNone(physics_gate.parse_data_file("Z:/no/such.data"))

    def test_dmin_periodic_min_image(self):
        # 两原子相邻周期像更近:盒 20,直接距离 19.9,镜像距离 0.1
        p = write_data_file([(1, 1, 0.0, 1.0, 1.0, 1.0), (2, 2, 0.0, 20.8, 1.0, 1.0)])
        d = physics_gate.parse_data_file(p)
        dmin, _ = physics_gate.compute_dmin(d)
        self.assertAlmostEqual(dmin, 0.8, places=6)

    def test_ffield_elements(self):
        p = physics_gate.parse_ffield_elements(write_ffield(["C", "H", "O"]))
        self.assertEqual(p, ["C", "H", "O"])
        self.assertIsNone(physics_gate.parse_ffield_elements("Z:/no/ffield"))


class TestExtractFacts(unittest.TestCase):
    """度量+衍生比值:只算不判——输出数值/结构,永不抛、永不产生"违规"。"""

    def test_sequence_structure_facts(self):
        f = physics_gate.extract_facts(base_intent(), {})
        self.assertTrue(f["has_minimize"])
        self.assertEqual(f["first_dyn_ensemble"], "nvt")
        self.assertTrue(f["relax_before_first_dyn"])
        self.assertFalse(f["any_nve"])
        self.assertFalse(f["continuation"])

    def test_bare_nve_structural_fact(self):
        it = base_intent()
        it["ensemble_sequence"] = [{"stage": "production", "ensemble": "nve"}]
        f = physics_gate.extract_facts(it, {})
        self.assertTrue(f["any_nve"])
        self.assertTrue(f["nve_unrelaxed"])  # 只记录结构事实,不判违规

    def test_continuation_fact(self):
        it = base_intent()
        it["source"] = {"kind": "derived_case", "base_case_id": "net6k"}
        it["ensemble_sequence"] = [{"stage": "production", "ensemble": "nve", "restart": True}]
        f = physics_gate.extract_facts(it, {})
        self.assertTrue(f["continuation"])
        self.assertTrue(f["nve_unrelaxed"])  # 结构照记,豁免判断归 AI+经验

    def test_data_measurements(self):
        p = write_data_file([(1, 1, 1.0, 1.0, 1.0, 1.0), (2, 2, 1.3, 1.0, 1.0, 3.0)])
        f = physics_gate.extract_facts(base_intent(), {"data_file": p})
        self.assertAlmostEqual(f["charge_sum"], 2.3, places=6)
        self.assertAlmostEqual(f["charge_delta"], 2.3, places=6)  # 声明 0
        self.assertAlmostEqual(f["dmin_A"], 2.0, places=6)

    def test_missing_elements_measurement(self):
        p = write_ffield(["C", "H", "O"])
        f = physics_gate.extract_facts(base_intent(), {"ffield_file": p})
        self.assertEqual(f["missing_elements"], [])

    def test_derived_ratios_friction(self):
        it = friction_intent()
        f = physics_gate.extract_facts(it, {})
        self.assertAlmostEqual(f["box_over_cutoff"], 20.0)
        self.assertAlmostEqual(f["disp_per_step_A"], 20.0 * 0.25 * 1e-5)
        self.assertAlmostEqual(f["depth_over_thickness"], 5.0 / 80.0)
        self.assertAlmostEqual(f["depth_over_radius"], 5.0 / 50.0)
        self.assertAlmostEqual(f["sliding_room_A"], 100.0)

    def test_missing_fields_never_raise(self):
        f = physics_gate.extract_facts({}, None)
        self.assertIsInstance(f, dict)
        self.assertNotIn("dmin_A", f)
        self.assertNotIn("box_over_cutoff", f)

    def test_no_judgment_surface(self):
        """度量层不再有判定 API:run_checks/HARD_GATES 等阈值判定面必须不存在。"""
        for gone in ("run_checks", "HARD_GATES", "SOFT_GATES", "check_h1", "check_e1"):
            self.assertFalse(hasattr(physics_gate, gone), "%s 应已删除(判断归 AI 审阅)" % gone)


class TestStructuralScan(unittest.TestCase):
    """存在性扫描:缺必填字段=事实清单,交 design_review 合并。"""

    def setUp(self):
        self.checklists = [{"family": "*",
                             "required_fields": ["key_params.timestep_fs",
                                                  "objective.simulation_goal"]},
                            {"family": "friction",
                             "required_fields": ["experiment_design.setup",
                                                  "experiment_design.design_rationale"]}]

    def test_generic_gaps_detected(self):
        gaps = physics_gate.structural_scan(base_intent(), self.checklists)
        paths = {g["path"] for g in gaps}
        self.assertIn("objective.simulation_goal", paths)  # base_intent 无 objective
        self.assertNotIn("key_params.timestep_fs", paths)

    def test_family_checklist_routing(self):
        gaps = physics_gate.structural_scan(base_intent(), self.checklists)
        self.assertNotIn("experiment_design.setup", {g["path"] for g in gaps})  # 非 friction 族不查

    def test_family_checklist_applies_when_type_matches(self):
        gaps = physics_gate.structural_scan(friction_intent(), self.checklists)
        # friction 意图已带 setup/design_rationale/objective → 通用+族清单全部满足
        self.assertEqual({g["path"] for g in gaps}, set())

    def test_dedup_and_empty_checklist(self):
        cl = self.checklists + [{"family": "*",
                                  "required_fields": ["key_params.timestep_fs"]}]
        gaps = physics_gate.structural_scan(base_intent(), cl)
        self.assertEqual([g["path"] for g in gaps].count("key_params.timestep_fs"), 0)
        self.assertEqual(physics_gate.structural_scan(base_intent(), []), [])


if __name__ == "__main__":
    unittest.main()
