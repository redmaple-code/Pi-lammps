# -*- coding: utf-8 -*-
"""端到端验收驱动(2026-10-04 AI 审阅架构版):进程内驱动 mdproj 双门全流程。

场景一:坏方案(0.5fs/裸NPT)→ 审阅 hard → GATED_FAIL → C4r 差异整改 → 复验通过
场景二:高熵合金摩擦 → C2e 守卫拦 → 设计论证证据入账 → 放行(design_review 摘要)
场景三:缺 timestep 的方案 → 结构 hard + 补全建议(完整审阅与补全)
场景四:审阅层不可用(REVIEW_ERROR)→ fail-closed 不放行
审阅 LLM 用确定性 fake(真 LLM 行为由 tests/test_design_review.py 用 stub 验证)。
用法: python tests/e2e_dual_gate_demo.py  (独立脚本,非 unittest)
"""
import json
import os
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

import md_proj_mcp  # noqa: E402
import design_review  # noqa: E402

P, CID = "e2e_proj", "e2e_case"


def H(name, **args):
    return md_proj_mcp.HANDLERS[name](args)


def fake_review(intent, ctx=None):
    """e2e 假审阅(2026-10-04 八层三档版):步长>0.25→hard;缺 timestep→结构 hard+补全;
    压深>15→soft;paper_grade 且缺实验参照→uncertain(A 层);其余通过。"""
    import physics_gate
    grade, reasons = physics_gate.effective_grade(intent)
    hard, uncertain, soft, comps = [], [], [], []

    def mk(gate, layer, sev, title, cited, refs=None):
        return {"gate": gate, "layer": layer, "aspect": "e2e", "severity": sev,
                "title": title, "detail": "", "rationale": "e2e 假审阅",
                "cited_fields": cited, "evidence_refs": refs or [],
                "remediation_hints": ["整改A", "整改B"], "unverified": False,
                "canary_experience": False}

    dt = (intent.get("key_params") or {}).get("timestep_fs")
    if dt is None:
        hard.append(mk("S-FIELD", "I", "hard", "缺失必填字段: key_params.timestep_fs",
                       ["key_params.timestep_fs"], ["structural"]))
        comps.append({"layer": "I", "aspect": "key_params.timestep_fs",
                      "why": "步长决定积分精度", "proposal": "ReaxFF 一般 0.1-0.25fs"})
    elif float(dt) > 0.25:
        hard.append(mk("REVIEW", "E", "hard", "步长过大",
                       ["key_params.timestep_fs"], ["exp-phy-001"]))
    depth = (((intent.get("experiment_design") or {}).get("setup") or {})
             .get("indentation_depth_A"))
    if depth is not None and float(depth) > 15:
        soft.append(mk("REVIEW", "C", "soft", "压深偏大须文献锚定",
                       ["experiment_design.setup.indentation_depth_A"]))
    if grade == "paper_grade" and not (intent.get("experiment_reference") or {}).get("sources"):
        uncertain.append(mk("REVIEW", "A", "uncertain", "论文型任务缺实验参照",
                            ["experiment_reference.sources"]))
    return {"checked_at": "e2e", "status": "OK", "passed": not hard and not uncertain,
            "task_grade": grade, "grade_reasons": reasons,
            "hard_violations": hard, "uncertain_findings": uncertain,
            "soft_warnings": soft, "completion_suggestions": comps,
            "layer_summary": {}, "verdict": "e2e fake verdict",
            "experience": {"packs": [{"pack": "physics_basics", "entries_ok": 15}],
                            "used_ids": ["exp-phy-001"], "quarantined": []},
            "deferred_to_runtime": ["H6", "H7"], "accepted_warnings": [],
            "skipped_gates": [], "facts": {"seq_n_stages": 3}, "structural_gaps": []}


GOOD = {
    "system": {"molecule": "碳氢框架", "target_elements": ["C", "H"],
               "size": {"atoms_target": 6000, "density_target": 0.25},
               "declared_net_charge": 0.0},
    "objective": {"simulation_goal": "热解", "target_property": ["char_yield"]},
    "constraints": {"temperature_range": [300, 300],
                    "protocol_flags": {"is_quench": False, "has_heating_ramp": False},
                    "walltime_hours": 48},
    "ensemble_sequence": [{"stage": "minimize"},
                           {"stage": "pre_equil", "ensemble": "nvt", "thermostat": "langevin"},
                           {"stage": "production", "ensemble": "npt"}],
    "key_params": {"timestep_fs": 0.25, "total_steps": 600000, "cores": 48,
                   "forcefield": {"preferred": "ffield.reax.CHO"},
                   "units": "real", "pair_style": "reax/c", "charge_method": "qeq"},
}
DESIGN = {f: {"chosen_by": "C2%s:全部按推荐" % f[0]} for f in
          ("system", "forcefield", "dynamics", "resources")}
DESIGN_EVIDENCE = [{"field": "indentation_depth_A", "verdict": "consistent",
                    "literature_range": "同类 HEA 摩擦 MD 典型 0.05-0.2 R",
                    "evidence": ["https://doi.org/10.1016/j.triboint.2024.05.017"]}]


def drive(payload, case_id, **draft_extra):
    H("new_project", project=P)
    H("new_task", project=P, case_id=case_id)
    H("draft_intent", project=P, case_id=case_id, intent=payload,
       open_questions=[], **draft_extra)
    H("update_intent", project=P, case_id=case_id, section="design", patch=DESIGN,
       cp="C2", answer="全部按推荐")


def main():
    root = tempfile.mkdtemp(prefix="mdproj_e2e_")
    os.environ["MDPROJ_ROOT"] = root
    md_proj_mcp.schedule_for = lambda project: None
    with mock.patch.object(design_review, "run", side_effect=fake_review):
        print("== 场景一:坏方案 审阅→整改→复验 ==")
        bad = json.loads(json.dumps(GOOD))
        bad["key_params"]["timestep_fs"] = 0.5
        drive(bad, CID)
        H("confirm_intent", project=P, case_id=CID, confirm_quote="确认")
        r = H("validate_intent", project=P, case_id=CID)
        print(" 首次校核: status=%s hard=%s" % (r["status"], r["hard_gates"]))
        assert r["status"] == "GATED_FAIL" and "REVIEW" in r["hard_gates"]
        H("update_intent", project=P, case_id=CID, section="key_params",
          patch=GOOD["key_params"], cp="C4r", minor_revision=True, answer="步长降 0.25fs")
        r = H("validate_intent", project=P, case_id=CID)
        print(" 整改复验: status=%s" % r["status"])
        assert r["status"] == "GATED_PASS"

        print("\n== 场景二:高熵合金摩擦(C2e 设计论证守卫)==")
        fric = json.loads(json.dumps(GOOD))
        fric["objective"]["simulation_goal"] = "高熵合金 CoCrFeNi 摩擦磨损"
        fric["experiment_design"] = {
            "experiment_type": "friction_indentation",
            "setup": {"indenter": {"shape": "sphere", "radius_A": 50.0},
                      "indentation_depth_A": 5.0, "sliding_velocity_m_s": 20.0,
                      "sliding_length_A": 80.0,
                      "sample": {"dimensions_A": [200.0, 200.0, 80.0]},
                      "box_l_A": 200.0, "pair_cutoff_A": 10.0,
                      "thermostat": {"region": "底部边界层"}},
            "design_rationale": "压深 5Å=0.1R 浅压"}
        drive(fric, "hea_fric")
        r = H("update_intent", project=P, case_id="hea_fric", section="design",
              patch=DESIGN, cp="C2e", answer="全部按推荐")
        assert "design_gate_blocker" in r, "C2e 守卫必须拦下未论证设计"
        print(" 无证据: %s..." % r["design_gate_blocker"][:48])
        r = H("update_intent", project=P, case_id="hea_fric", section="design",
              patch=DESIGN, cp="C2e", answer="论证通过", design_evidence=DESIGN_EVIDENCE)
        print(" 证据入账: status=%s" % r["status"])
        H("confirm_intent", project=P, case_id="hea_fric", confirm_quote="摩擦方案确认")
        r = H("validate_intent", project=P, case_id="hea_fric")
        print(" 摩擦校核: status=%s verdict=%s" % (
            r["status"], r["gate_report"].get("verdict")))
        assert r["status"] == "GATED_PASS"
        r = H("intent_view", project=P, case_id="hea_fric")
        assert "experiment_design" in r["render"] and "引用经验" in r["render"]
        print(" intent_view 含实验设计论证与经验引用 ✔")

        print("\n== 场景三:缺字段方案 结构hard+补全建议(完整审阅与补全)==")
        incomplete = json.loads(json.dumps(GOOD))
        del incomplete["key_params"]["timestep_fs"]
        drive(incomplete, "incomplete")
        H("confirm_intent", project=P, case_id="incomplete", confirm_quote="确认")
        r = H("validate_intent", project=P, case_id="incomplete")
        comps = r["gate_report"]["completion_suggestions"]
        print(" status=%s hard=%s" % (r["status"], r["hard_gates"]))
        print(" 补全建议: %s → %s" % (comps[0]["aspect"], comps[0]["proposal"]))
        assert r["status"] == "GATED_FAIL" and "S-FIELD" in r["hard_gates"]
        assert any(c["aspect"] == "key_params.timestep_fs" for c in comps)
        H("update_intent", project=P, case_id="incomplete", section="key_params",
          patch=GOOD["key_params"], cp="C4r", minor_revision=True,
          answer="按补全建议补 0.25fs")
        r = H("validate_intent", project=P, case_id="incomplete")
        print(" 补全复验: status=%s 补全建议清零=%s" % (
            r["status"], r["gate_report"]["completion_suggestions"] == []))
        assert r["status"] == "GATED_PASS"

    print("\n== 场景四:审阅层不可用 fail-closed ==")
    def dead_review(intent, ctx=None):
        return {"status": "REVIEW_ERROR", "passed": False, "error": "无 LLM 凭证",
                "hard_violations": [], "soft_warnings": [], "completion_suggestions": [],
                "deferred_to_runtime": ["H6", "H7"], "accepted_warnings": [],
                "skipped_gates": [], "checked_at": "e2e", "experience": {}}
    with mock.patch.object(design_review, "run", side_effect=dead_review):
        drive(GOOD, "dead_review_case")
        H("confirm_intent", project=P, case_id="dead_review_case", confirm_quote="确认")
        r = H("validate_intent", project=P, case_id="dead_review_case")
        print(" REVIEW_ERROR: status=%s error=%s(不静默放行)" % (
            r.get("status"), r["gate_report"].get("review_status")))
        assert r.get("status") == "GATED_FAIL"

    print("\n== 场景五:论文型实验-仿真一致性(schema v3.0 四段+八层审阅)==")
    with mock.patch.object(design_review, "run", side_effect=fake_review):
            paper = json.loads(json.dumps(GOOD))
            paper["objective"] = {"simulation_goal": "复现并解释 CoCrFeNi 摩擦磨损机制",
                                   "target_property": ["friction_coeff"]}
            paper["material_model"] = {
                "class": "solid_solution_fcc",
                "composition": {"Co": 0.25, "Cr": 0.25, "Fe": 0.25, "Ni": 0.25, "basis": "等原子比"},
                "lattice": {"type": "fcc", "constant_A": 3.56, "source": "XRD 3.59Å 折 0K"},
                "solid_solution": {"randomized": True, "method": "随机占位"},
                "construction": {"tool": "atomsk", "validation": ["dmin≥2.2Å"]}}
            paper["experiment_design"] = {
                "experiment_type": "friction_indentation",
                "setup": {"indenter": {"shape": "sphere", "radius_A": 50.0},
                          "indentation_depth_A": 5.0, "sliding_velocity_m_s": 20.0,
                          "sliding_length_A": 80.0,
                          "sample": {"dimensions_A": [200.0, 200.0, 80.0]},
                          "box_l_A": 200.0, "pair_cutoff_A": 10.0,
                          "thermostat": {"region": "底部边界层"}},
                "design_rationale": "浅压 0.1R"}
            drive(paper, "paper_case")
            # 补实验参照(paper_grade 升格后,审阅要求实验侧参照系)
            r = H("update_intent", project=P, case_id="paper_case",
                  section="experiment_reference",
                  patch={"kind": "literature_experiment",
                         "sources": [{"ref": "Tribology Int. 2024",
                                      "doi": "10.1016/j.triboint.2024.05.017",
                                      "excerpt": "CoCrFeNi 摩擦系数 0.35"}],
                         "conditions": {"loading": {"type": "sliding", "value": 5, "unit": "N"},
                                        "temperature_K": 300, "environment": "air"},
                         "measured": [{"property": "friction_coeff", "value": 0.35,
                                        "uncertainty": "±0.05", "method": "pin-on-disc"}]},
                  cp="C2f", answer="补实验参照")
            r = H("update_intent", project=P, case_id="paper_case", section="abstraction",
                  patch={"mappings": [{"experiment": "滑速 10mm/s", "simulation": "20 m/s",
                                        "formula": "速率外推(已知代价)",
                                        "assumptions": ["机制层可比"],
                                        "fidelity": "qualitative_only", "loss": "温升不可复刻"}],
                         "simplifications": [{"aspect": "粗糙度", "simplified_to": "原子级光滑",
                                               "why": "聚焦粘着机制",
                                               "expected_impact": "摩擦系数偏低估计",
                                               "compromises_comparability": True}]},
                  cp="C2f", answer="抽象映射确认")
            r = H("update_intent", project=P, case_id="paper_case", section="comparability",
                  patch={"targets": [{"property": "friction_coeff",
                                       "correspondence": "trend_only",
                                       "experimental_anchor": {"value": 0.35, "uncertainty": "±0.05"},
                                       "comparison_protocol": "稳态段平均 vs 实验稳态",
                                       "tolerance": "趋势方向一致", "noise_floor": "±3%"}],
                         "explicitly_out_of_scope": ["裂纹形貌"],
                         "explanation_role": "解释元素效应与磨损机制方向,不声称复现绝对值"},
                  cp="C2f", answer="对照协议确认")
            r = H("update_intent", project=P, case_id="paper_case", section="design",
                  patch=DESIGN, cp="C2e", answer="全部按推荐", design_evidence=DESIGN_EVIDENCE)
            intent = json.load(open(r["intent_path"], encoding="utf-8"))
            print(" task_grade=%s(代码判定)" % intent.get("task_grade"))
            assert intent.get("task_grade") == "paper_grade"
            H("confirm_intent", project=P, case_id="paper_case", confirm_quote="论文型方案确认")
            r = H("validate_intent", project=P, case_id="paper_case")
            print(" 论文型审阅: status=%s grade=%s" % (r["status"], r["task_grade"]))
            assert r["status"] == "GATED_PASS", (r.get("hard_gates"), r.get("uncertain_gates"))
            v = H("intent_view", project=P, case_id="paper_case")["render"]
            assert "材料模型" in v and "抽象映射" in v and "对照协议" in v
            print(" intent_view 渲染四段(材料模型/实验参照/抽象映射/对照协议) ✔")

    print("\nE2E 五场景全部通过 ✔")


if __name__ == "__main__":
    main()
