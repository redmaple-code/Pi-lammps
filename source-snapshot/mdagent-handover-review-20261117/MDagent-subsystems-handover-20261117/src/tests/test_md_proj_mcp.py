# -*- coding: utf-8 -*-
"""mdproj 受控写 MCP 回归:五道写闸 + 状态机守卫 + 工具注册表 + stdio 冒烟。

运行(仓库根或本目录均可):
    python -m unittest tests.test_md_proj_mcp -v
被测对象:mcp/md_proj_mcp.py(设计规格 §4:9 工具 + 五道闸)。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

import md_proj_mcp  # noqa: E402
import design_review  # noqa: E402


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fake_review(intent, ctx=None):
    """确定性假审阅(替 AI 判断层,2026-10-04 三档版):步长>0.25 或首段即 npt → hard;
    压深>15 → soft 点名实验设计字段;缺 timestep → 结构 hard。
    只为驱动 mdproj 状态机/R-VERIFY 测试,审阅行为本身见 test_design_review。"""
    import physics_gate
    grade, reasons = physics_gate.effective_grade(intent)
    hard, uncertain, soft, comps = [], [], [], []

    def mk(gate, layer, sev, title, cited, refs=None):
        return {"gate": gate, "layer": layer, "aspect": "fake", "severity": sev,
                "title": title, "detail": "", "rationale": "fake-review",
                "cited_fields": cited, "evidence_refs": refs or [],
                "remediation_hints": [], "unverified": False,
                "canary_experience": False}

    kp = intent.get("key_params") or {}
    dt = kp.get("timestep_fs")
    if dt is None:
        hard.append(mk("S-FIELD", "I", "hard", "缺失必填字段: key_params.timestep_fs",
                       ["key_params.timestep_fs"], ["structural"]))
    elif float(dt) > 0.25:
        hard.append(mk("REVIEW", "E", "hard", "步长过大", ["key_params.timestep_fs"]))
    seq = intent.get("ensemble_sequence") or []
    if seq and isinstance(seq[0], dict) and seq[0].get("ensemble") == "npt":
        hard.append(mk("REVIEW", "E", "hard", "未弛豫直冲 NPT", ["ensemble_sequence"]))
    setup = ((intent.get("experiment_design") or {}).get("setup") or {})
    depth = setup.get("indentation_depth_A")
    if depth is not None and float(depth) > 15:
        soft.append(mk("REVIEW", "C", "soft", "压深偏大",
                       ["experiment_design.setup.indentation_depth_A"]))
    return {"checked_at": "test", "status": "OK", "passed": not hard and not uncertain,
            "task_grade": grade, "grade_reasons": reasons,
            "hard_violations": hard, "uncertain_findings": uncertain,
            "soft_warnings": soft, "completion_suggestions": comps,
            "layer_summary": {}, "verdict": "fake-review",
            "experience": {"packs": [], "used_ids": [], "quarantined": []},
            "deferred_to_runtime": ["H6", "H7"], "accepted_warnings": [],
            "skipped_gates": [], "facts": {}, "structural_gaps": []}


def good_design():
    return {
        "system": {"chosen_by": "C2a:全部按推荐"},
        "forcefield": {"chosen_by": "C2b:全部按推荐"},
        "dynamics": {"chosen_by": "C2c:全部按推荐"},
        "resources": {"chosen_by": "C2d:全部按推荐"},
    }


def good_payload():
    """能过物理门的合法意图载荷(无 data/ffield 依赖)。"""
    return {
        "system": {"molecule": "碳氢框架", "target_elements": ["C", "H"],
                   "size": {"atoms_target": 6000, "density_target": 0.25},
                   "declared_net_charge": 0.0},
        "objective": {"simulation_goal": "热解", "target_property": ["char_yield"]},
        "constraints": {"temperature_range": [300, 300],
                        "protocol_flags": {"is_quench": False, "has_heating_ramp": False},
                        "walltime_hours": 48},
        "ensemble_sequence": [
            {"stage": "minimize"},
            {"stage": "pre_equil", "ensemble": "nvt", "thermostat": "langevin"},
            {"stage": "production", "ensemble": "npt"},
        ],
        "key_params": {"timestep_fs": 0.25, "total_steps": 600000, "cores": 48,
                       "forcefield": {"preferred": "ffield.reax.CHO"},
                       "units": "real", "pair_style": "reax/c", "charge_method": "qeq"},
    }


def drive_to_confirmed(case_id="case_ok", payload=None):
    """走完 C0→C3:new_project/new_task/draft/澄清/设计/终审。"""
    p = payload or good_payload()
    md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
    md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": case_id})
    md_proj_mcp.HANDLERS["draft_intent"]({
        "project": "t_proj", "case_id": case_id, "intent": p,
        "open_questions": [{"id": "Q1", "question": "密度口径?", "options": ["骨架", "堆积"],
                             "answered": True, "answer": "骨架"}]})
    md_proj_mcp.HANDLERS["update_intent"]({
        "project": "t_proj", "case_id": case_id, "section": "design",
        "patch": good_design(), "cp": "C2", "answer": "全部按推荐"})
    return md_proj_mcp.HANDLERS["confirm_intent"]({
        "project": "t_proj", "case_id": case_id, "confirm_quote": "确认,按此方案执行"})


class BaseTmpRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mdproj_test_")
        self._env = mock.patch.dict(os.environ, {"MDPROJ_ROOT": self.root})
        self._env.start()
        self._sched = mock.patch.object(md_proj_mcp, "schedule_for", return_value=None)
        self._sched.start()
        # 审阅层(AI 判断)打桩:mdproj 测试只考状态机/写闸/E-VERIFY 流程纪律
        self._review = mock.patch.object(design_review, "run", side_effect=fake_review)
        self._review.start()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._env.stop)
        self.addCleanup(self._sched.stop)
        self.addCleanup(self._review.stop)

    def audit_lines(self):
        p = os.path.join(md_proj_mcp.project_root(), "_audit", "audit.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]


class TestToolRegistry(BaseTmpRoot):
    def test_tools_handlers_consistent(self):
        self.assertEqual({t["name"] for t in md_proj_mcp.TOOLS},
                         set(md_proj_mcp.HANDLERS.keys()))
        for t in md_proj_mcp.TOOLS:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)


class TestWriteGates(BaseTmpRoot):
    def test_ident_traversal_rejected(self):
        for bad in ("../evil", "a/b", "..", "x.y", "空 格", "a" * 65):
            r = md_proj_mcp.HANDLERS["new_project"]({"project": bad})
            self.assertIn("error", r, "标识符 %r 应被拒" % bad)
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil")))

    def test_new_project_skeleton(self):
        r = md_proj_mcp.HANDLERS["new_project"]({"project": "alpha"})
        self.assertNotIn("error", r)
        pj = os.path.join(self.root, "alpha", "project.json")
        self.assertTrue(os.path.exists(pj))
        data = load(pj)
        self.assertEqual(data["case_ids"], [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "alpha", "归纳说明.md")))
        self.assertTrue(self.audit_lines())  # 闸④:审计落盘

    def test_new_task_layout_and_registry(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "alpha"})
        r = md_proj_mcp.HANDLERS["new_task"]({"project": "alpha", "case_id": "net6k"})
        self.assertNotIn("error", r)
        run_dir = r["run_dir"]
        for sub in ("inputs", "logs", "results"):
            self.assertTrue(os.path.isdir(os.path.join(run_dir, sub)))
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "DRAFT")
        self.assertEqual(intent["case_id"], "net6k")
        data = load(os.path.join(self.root, "alpha", "project.json"))
        self.assertIn("net6k", data["case_ids"])

    def test_unknown_project_rejected(self):
        r = md_proj_mcp.HANDLERS["new_task"]({"project": "ghost", "case_id": "x"})
        self.assertIn("error", r)

    def test_realpath_symlink_escape_rejected(self):
        outside = tempfile.mkdtemp(prefix="mdproj_out_")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        try:
            os.symlink(outside, os.path.join(self.root, "link_out"))
        except (OSError, NotImplementedError):
            self.skipTest("本机无符号链接权限")
        r = md_proj_mcp.HANDLERS["new_project"]({"project": "link_out"})
        self.assertIn("error", r)  # 闸①:realpath 落点出根必拒

    def test_prod_dir_ref_injection_rejected(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "alpha"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "alpha", "case_id": "c1"})
        md_proj_mcp.HANDLERS["draft_intent"](
            {"project": "alpha", "case_id": "c1", "intent": good_payload()})
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "alpha", "case_id": "c1", "section": "prod_dir_ref",
            "patch": "/var/lib/mdsim/xxx", "cp": "C2"})
        self.assertIn("error", r)  # 闸③:越界字段不在分段白名单


class TestStateMachine(BaseTmpRoot):
    def test_draft_filters_injected_fields(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": "c1"})
        payload = good_payload()
        payload["status"] = "CONFIRMED"          # 注入状态 → 无效
        payload["prod_dir_ref"] = "/evil/path"   # 注入指针 → 丢弃
        payload["custom_key"] = 1                # 未知段 → 丢弃
        r = md_proj_mcp.HANDLERS["draft_intent"](
            {"project": "t_proj", "case_id": "c1", "intent": payload})
        self.assertNotIn("error", r)
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "DRAFT")
        self.assertIsNone(intent.get("prod_dir_ref"))  # 注入的指针被丢弃,骨架仍是 None
        self.assertNotIn("custom_key", intent)

    def test_clarify_requires_all_questions_answered(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": "c1"})
        md_proj_mcp.HANDLERS["draft_intent"]({
            "project": "t_proj", "case_id": "c1", "intent": good_payload(),
            "open_questions": [{"id": "Q1", "question": "密度口径?", "answered": False}]})
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "c1", "section": "system",
            "patch": good_payload()["system"], "cp": "C0"})
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "DRAFT")  # 问题未答,不推进

    def test_full_advance_draft_to_confirmed(self):
        r = drive_to_confirmed()
        self.assertNotIn("error", r)
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "CONFIRMED")
        cps = [d["cp"] for d in intent["decisions"]]
        self.assertIn("C2", cps)
        self.assertIn("C3", cps)
        self.assertTrue(any(d.get("confirm_quote") for d in intent["decisions"]))

    def test_confirm_requires_designed(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": "c1"})
        md_proj_mcp.HANDLERS["draft_intent"](
            {"project": "t_proj", "case_id": "c1", "intent": good_payload(),
             "open_questions": []})
        r = md_proj_mcp.HANDLERS["confirm_intent"](
            {"project": "t_proj", "case_id": "c1", "confirm_quote": "ok"})
        self.assertIn("error", r)  # 未到 DESIGNED,终审门拒绝

    def test_validate_requires_confirmed(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": "c1"})
        md_proj_mcp.HANDLERS["draft_intent"](
            {"project": "t_proj", "case_id": "c1", "intent": good_payload(),
             "open_questions": []})
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "c1", "section": "design",
            "patch": good_design(), "cp": "C2"})
        r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "c1"})
        self.assertIn("error", r)  # 仅 CONFIRMED 可校核

    def test_good_intent_gated_pass(self):
        drive_to_confirmed()
        r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "case_ok"})
        self.assertNotIn("error", r)
        self.assertEqual(r["status"], "GATED_PASS")
        self.assertTrue(r["gate_report"]["passed"])
        self.assertIn("validate_intent", [a["action"] for a in self.audit_lines()])


class TestRemediationLoop(BaseTmpRoot):
    def _drive_bad_then_fail(self):
        payload = good_payload()
        payload["key_params"] = dict(payload["key_params"], timestep_fs=0.5)
        payload["ensemble_sequence"] = [{"stage": "production", "ensemble": "npt"}]
        payload["system"] = dict(payload["system"], packmol_input="packing draft")
        drive_to_confirmed(case_id="case_bad", payload=payload)
        return md_proj_mcp.HANDLERS["validate_intent"](
            {"project": "t_proj", "case_id": "case_bad"})

    def test_hard_violation_blocks(self):
        r = self._drive_bad_then_fail()
        self.assertEqual(r["status"], "GATED_FAIL")
        gates = [v["gate"] for v in r["gate_report"]["hard_violations"]]
        self.assertEqual(gates.count("REVIEW"), 2)  # 步长+裸NPT(假审阅)双硬拦

    def test_cited_field_remediation_keeps_confirmed(self):
        self._drive_bad_then_fail()
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_bad", "section": "key_params",
            "patch": dict(good_payload()["key_params"], timestep_fs=0.25),
            "cp": "C4r", "minor_revision": True, "answer": "步长降 0.25fs"})
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "CONFIRMED")  # 差异确认整改:不回设计态

    def test_non_cited_change_demotes_to_designed(self):
        self._drive_bad_then_fail()
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_bad", "section": "objective",
            "patch": {"simulation_goal": "热解(改目标)", "target_property": ["density"]},
            "cp": "C4r", "minor_revision": True, "answer": "顺手改目标"})
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "DESIGNED")  # 非违规点名字段=降级重审
        self.assertEqual(intent["revision"], 1)

    def test_revalidate_after_fix_passes(self):
        self._drive_bad_then_fail()
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_bad", "section": "key_params",
            "patch": good_payload()["key_params"], "cp": "C4r", "minor_revision": True,
            "answer": "步长回 0.25fs"})
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_bad", "section": "ensemble_sequence",
            "patch": good_payload()["ensemble_sequence"], "cp": "C4r",
            "minor_revision": True, "answer": "补弛豫链条"})
        r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "case_bad"})
        self.assertEqual(r["status"], "GATED_PASS")

    def test_change_after_pass_demotes(self):
        drive_to_confirmed()
        md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "case_ok"})
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_ok", "section": "objective",
            "patch": {"simulation_goal": "改为碳化", "target_property": ["density"]},
            "cp": "C2", "answer": "用户改目标"})
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "DESIGNED")
        self.assertEqual(intent["revision"], 1)


class TestReadTools(BaseTmpRoot):
    def test_list_projects(self):
        md_proj_mcp.HANDLERS["new_project"]({"project": "alpha"})
        r = md_proj_mcp.HANDLERS["list_projects"]({})
        self.assertEqual([p["project"] for p in r["projects"]], ["alpha"])

    def test_task_status_decision_chain(self):
        drive_to_confirmed()
        r = md_proj_mcp.HANDLERS["task_status"]({"project": "t_proj", "case_id": "case_ok"})
        self.assertEqual(r["status"], "CONFIRMED")
        self.assertGreaterEqual(len(r["decisions"]), 2)
        self.assertIn("无对应活动 run", json.dumps(r, ensure_ascii=False))

    def test_intent_view_render(self):
        drive_to_confirmed()
        r = md_proj_mcp.HANDLERS["intent_view"]({"project": "t_proj", "case_id": "case_ok"})
        text = r["render"]
        self.assertIn("timestep_fs", text)
        self.assertIn("CONFIRMED", text)
        self.assertIn("validate_intent", text)  # 下一步指引


def friction_payload():
    """高熵合金摩擦:含 experiment_design(会触发 E 门与设计证据守卫)。"""
    p = good_payload()
    p.setdefault("objective", {})["simulation_goal"] = "高熵合金 CoCrFeNi 摩擦磨损"
    p["experiment_design"] = {
        "experiment_type": "friction_indentation",
        "setup": {
            "indenter": {"shape": "sphere", "radius_A": 50.0},
            "indentation_depth_A": 5.0,
            "sliding_velocity_m_s": 20.0,
            "sliding_length_A": 80.0,
            "sample": {"dimensions_A": [200.0, 200.0, 80.0]},
            "box_l_A": 200.0,
            "pair_cutoff_A": 10.0,
            "thermostat": {"region": "底部边界层", "method": "langevin"},
        },
        "design_rationale": "压深 5Å=0.1R 浅压,控温只在底部边界层",
    }
    return p


DESIGN_EVIDENCE = [{
    "field": "indentation_depth_A",
    "verdict": "consistent",
    "literature_range": "同类 HEA 摩擦 MD 压深典型 0.05-0.2 R",
    "evidence": ["https://doi.org/10.1016/j.triboint.2024.05.017"],
}]


class TestDesignEvidenceStateMachine(BaseTmpRoot):
    """C2e 实验设计论证:状态机守卫 + design_evidence 入账 + E-VERIFY 放行闸。"""

    def _draft_friction(self, case_id="case_fric"):
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": case_id})
        md_proj_mcp.HANDLERS["draft_intent"]({
            "project": "t_proj", "case_id": case_id, "intent": friction_payload(),
            "open_questions": []})

    def _update_design(self, case_id="case_fric", **extra):
        return md_proj_mcp.HANDLERS["update_intent"](dict({
            "project": "t_proj", "case_id": case_id, "section": "design",
            "patch": good_design(), "cp": "C2e", "answer": "全部按推荐"}, **extra))

    def test_designed_blocked_without_evidence_or_assumption(self):
        self._draft_friction()
        r = self._update_design()
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "CLARIFIED")  # 未论证不得进设计终审
        self.assertIn("design_gate_blocker", r)
        self.assertIn("sim_design_research", r["design_gate_blocker"])

    def test_designed_unlocked_by_design_evidence(self):
        self._draft_friction()
        r = self._update_design(design_evidence=DESIGN_EVIDENCE)
        intent = load(r["intent_path"])
        self.assertEqual(intent["status"], "DESIGNED")
        plaus = intent["plausibility"]  # v3 泛化:证据容器在顶层(2026-10-14)
        self.assertEqual(plaus["evidence"][0]["field"], "indentation_depth_A")

    def test_designed_unlocked_by_user_assumption(self):
        self._draft_friction()
        ed = friction_payload()["experiment_design"]
        ed["plausibility"] = {"unverified_assumptions": [
            {"field": "sliding_velocity_m_s", "assumption": "先按 20m/s 跑通",
             "accepted_by": "user"}]}
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_fric", "section": "experiment_design",
            "patch": ed, "cp": "C2e", "answer": "用户接受存疑假设"})
        self.assertEqual(load(r["intent_path"])["status"], "CLARIFIED")  # 设计四要素还没确认
        r2 = self._update_design()
        self.assertEqual(load(r2["intent_path"])["status"], "DESIGNED")

    def test_patch_plausibility_evidence_rejected(self):
        self._draft_friction()
        ed = friction_payload()["experiment_design"]
        ed["plausibility"] = {"evidence": [{"field": "x", "evidence": ["http://fake"]}]}
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_fric", "section": "experiment_design",
            "patch": ed, "cp": "C2e", "answer": "自填证据"})
        self.assertIn("error", r)  # 外部证据只能经 design_evidence 参数(绑研讨产出)

    def test_validate_blocks_uncovered_soft_cited_fields(self):
        # E5 触发(压深 25Å=0.5R)但该字段无证据也无假设 → E-VERIFY 硬拦
        self._draft_friction()
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_fric", "section": "experiment_design",
            "patch": dict(friction_payload()["experiment_design"],
                          **{"setup": dict(friction_payload()["experiment_design"]["setup"],
                                           indentation_depth_A=25.0)}),
            "cp": "C2e", "answer": "深压工况",
            "design_evidence": [{"field": "other_field", "verdict": "consistent",
                                  "evidence": ["https://x.example/1"]}]})
        self._update_design()
        md_proj_mcp.HANDLERS["confirm_intent"]({
            "project": "t_proj", "case_id": "case_fric", "confirm_quote": "确认"})
        r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "case_fric"})
        self.assertEqual(r["status"], "GATED_FAIL")
        self.assertIn("R-VERIFY", r["hard_gates"])  # 2026-10-04 由 E-VERIFY 泛化
        dr = r["gate_report"]["design_review"]
        self.assertIn("experiment_design.setup.indentation_depth_A", dr["unverified_critical"])

    def test_validate_passes_when_evidence_covers_cited_fields(self):
        self._draft_friction()
        self._update_design(design_evidence=DESIGN_EVIDENCE)
        md_proj_mcp.HANDLERS["confirm_intent"]({
            "project": "t_proj", "case_id": "case_fric", "confirm_quote": "确认"})
        r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "case_fric"})
        self.assertEqual(r["status"], "GATED_PASS")  # 基线摩擦设计 E 门全过+证据已覆盖
        self.assertIn("design_review", r["gate_report"])

    def test_evidence_refs_recorded_in_decisions(self):
        drive_to_confirmed()
        r = md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "case_ok", "section": "objective",
            "patch": {"simulation_goal": "热解(证据修订)", "target_property": ["density"]},
            "cp": "C2", "answer": "按文献修订",
            "evidence_refs": ["https://doi.org/10.1234/x"]})
        intent = load(r["intent_path"])
        last = intent["decisions"][-1]
        self.assertEqual(last.get("evidence_refs"), ["https://doi.org/10.1234/x"])

    def test_intent_view_renders_experiment_design(self):
        self._draft_friction()
        self._update_design(design_evidence=DESIGN_EVIDENCE)
        r = md_proj_mcp.HANDLERS["intent_view"]({"project": "t_proj", "case_id": "case_fric"})
        self.assertIn("experiment_design", r["render"])
        self.assertIn("indentation_depth_A", r["render"])
        self.assertIn("design_rationale", r["render"])


def paper_payload():
    """论文型(paper_grade):四段齐 + 实验对照(高熵合金摩擦)。"""
    p = friction_payload()
    p["objective"]["simulation_goal"] = "复现并解释 CoCrFeNi 摩擦磨损机制"
    p["material_model"] = {
        "class": "solid_solution_fcc",
        "composition": {"Co": 0.25, "Cr": 0.25, "Fe": 0.25, "Ni": 0.25, "basis": "等原子比"},
        "lattice": {"type": "fcc", "constant_A": 3.56, "source": "文献 XRD 3.59Å 折 0K"},
        "supercell": {"size": [10, 10, 8], "atoms": 4000, "orientation": [1, 0, 0]},
        "solid_solution": {"randomized": True, "method": "随机占位"},
        "construction": {"tool": "atomsk", "protocol": "lattice 构建→随机占位→minimize",
                          "validation": ["dmin≥2.2Å"]},
    }
    p["experiment_reference"] = {
        "kind": "literature_experiment",
        "sources": [{"ref": "Tribology Int. 2024", "doi": "10.1016/j.triboint.2024.05.017",
                     "excerpt": "CoCrFeNi 摩擦系数 0.35"}],
        "conditions": {"loading": {"type": "sliding", "value": 5, "unit": "N", "rate": "10 mm/s"},
                       "temperature_K": 300, "environment": "air"},
        "measured": [{"property": "friction_coeff", "value": 0.35, "uncertainty": "±0.05",
                       "method": "pin-on-disc"}],
    }
    p["abstraction"] = {
        "mappings": [{"experiment": "滑速 10mm/s", "simulation": "滑速 20 m/s",
                       "formula": "速率外推(已知代价)", "assumptions": ["机制层可比"],
                       "fidelity": "qualitative_only", "loss": "温升不可复刻"}],
        "simplifications": [{"aspect": "表面粗糙度", "simplified_to": "原子级光滑",
                              "why": "聚焦粘着机制", "expected_impact": "摩擦系数偏低估计",
                              "compromises_comparability": True}],
        "scale_bridging": {"length": "1mm→20nm 单晶微区", "time": "60s→1ns"},
    }
    p["comparability"] = {
        "targets": [{"property": "friction_coeff", "correspondence": "trend_only",
                      "experimental_anchor": {"value": 0.35, "uncertainty": "±0.05"},
                      "comparison_protocol": "稳态段平均 vs 实验稳态", "tolerance": "趋势方向一致",
                      "noise_floor": "seed spread ±3%"}],
        "explicitly_out_of_scope": ["裂纹形貌"],
        "explanation_role": "解释元素效应与磨损机制方向,不声称复现绝对值",
    }
    return p


class TestSchemaV3AndGrade(BaseTmpRoot):
    """schema v3.0(2026-10-04):四段 + task_grade 代码升级 + uncertain 阻断 + R-VERIFY 泛化。"""

    def _draft(self, payload, case_id="v3_case"):
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": case_id})
        r = md_proj_mcp.HANDLERS["draft_intent"]({
            "project": "t_proj", "case_id": case_id, "intent": payload,
            "open_questions": []})
        return r

    def test_paper_grade_auto_upgraded_by_code(self):
        """paper_grade 触发(experiment_reference/comparability)→ 代码即时升级落盘。"""
        p = paper_payload()
        p.pop("task_grade", None)
        self._draft(p)
        intent = load(md_proj_mcp.HANDLERS["intent_view"](
            {"project": "t_proj", "case_id": "v3_case"}) and
            md_proj_mcp._intent_path(md_proj_mcp._find_run_dir("t_proj", "v3_case")))
        self.assertEqual(intent.get("task_grade"), "paper_grade")
        self.assertTrue(intent.get("grade_reasons"))

    def test_four_sections_accepted_and_rendered(self):
        p = paper_payload()
        self._draft(p)
        r = md_proj_mcp.HANDLERS["intent_view"]({"project": "t_proj", "case_id": "v3_case"})
        self.assertIn("material_model", r["render"])

    def test_uncertain_blocks_pass(self):
        """uncertain findings 不放行(GATED_PASS 要求 hard=0 且 uncertain=0)。"""
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": "u_case"})
        md_proj_mcp.HANDLERS["draft_intent"]({
            "project": "t_proj", "case_id": "u_case", "intent": good_payload(),
            "open_questions": []})
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "u_case", "section": "design",
            "patch": good_design(), "cp": "C2", "answer": "全部按推荐"})
        md_proj_mcp.HANDLERS["confirm_intent"]({
            "project": "t_proj", "case_id": "u_case", "confirm_quote": "确认"})
        with mock.patch.object(design_review, "run", side_effect=uncertain_review):
            r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "u_case"})
        self.assertEqual(r["status"], "GATED_FAIL")
        self.assertEqual(r["uncertain_gates"], ["REVIEW"])

    def test_resolution_covers_r_verify(self):
        """resolutions[](书面处置)可消解 R-VERIFY 点名字段 → GATED_PASS。"""
        md_proj_mcp.HANDLERS["new_project"]({"project": "t_proj"})
        md_proj_mcp.HANDLERS["new_task"]({"project": "t_proj", "case_id": "res_case"})
        md_proj_mcp.HANDLERS["draft_intent"]({
            "project": "t_proj", "case_id": "res_case", "intent": good_payload(),
            "open_questions": []})
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "res_case", "section": "resolutions",
            "patch": [{"field": "key_params.timestep_fs", "issue_digest": "步长环境敏感性",
                        "resolution": "accepted", "refs": [], "accepted_by": "user",
                        "ts": "2026-10-04"}],
            "cp": "C5", "answer": "用户书面接受"})
        md_proj_mcp.HANDLERS["update_intent"]({
            "project": "t_proj", "case_id": "res_case", "section": "design",
            "patch": good_design(), "cp": "C2", "answer": "全部按推荐"})
        md_proj_mcp.HANDLERS["confirm_intent"]({
            "project": "t_proj", "case_id": "res_case", "confirm_quote": "确认"})
        with mock.patch.object(design_review, "run", side_effect=soft_review):
            r = md_proj_mcp.HANDLERS["validate_intent"]({"project": "t_proj", "case_id": "res_case"})
        self.assertEqual(r["status"], "GATED_PASS", r.get("hard_gates"))
        dr = r["gate_report"]["design_review"]
        self.assertIn("key_params.timestep_fs", dr["resolution_fields"])


def uncertain_review(intent, ctx=None):
    """假审阅:一条 uncertain(点名字段) → 阻断放行。"""
    base = fake_review(intent, ctx)
    base["uncertain_findings"] = [{
        "gate": "REVIEW", "layer": "D", "aspect": "尺度", "severity": "uncertain",
        "title": "力场能否表达该物理存疑", "detail": "", "rationale": "证据不足",
        "cited_fields": ["key_params.forcefield"], "evidence_refs": [],
        "remediation_hints": [], "unverified": True, "canary_experience": False}]
    base["passed"] = False
    return base


def soft_review(intent, ctx=None):
    """假审阅:一条 soft 点名字段(须 resolutions 覆盖)。"""
    base = fake_review(intent, ctx)
    base["soft_warnings"] = [{
        "gate": "REVIEW", "layer": "E", "aspect": "动力学", "severity": "soft",
        "title": "步长环境敏感性提示", "detail": "", "rationale": "惯例",
        "cited_fields": ["key_params.timestep_fs"], "evidence_refs": [],
        "remediation_hints": [], "unverified": False, "canary_experience": False}]
    return base



    def test_initialize_and_tools_list(self):
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, MDPROJ_ROOT=os.path.join(root, "projs"),
                       PYTHONIOENCODING="utf-8")
            proc = subprocess.Popen(
                [sys.executable, os.path.join(MCP_DIR, "md_proj_mcp.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8")
            try:
                reqs = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                ]
                out, _ = proc.communicate(
                    "\n".join(json.dumps(q) for q in reqs) + "\n", timeout=60)
                lines = [ln for ln in out.splitlines() if ln.strip()]
                self.assertEqual(len(lines), 2)
                init = json.loads(lines[0])["result"]
                self.assertEqual(init["serverInfo"]["name"], "mdproj")
                tools = json.loads(lines[1])["result"]["tools"]
                names = {t["name"] for t in tools}
                self.assertIn("draft_intent", names)
                self.assertIn("validate_intent", names)
            finally:
                if proc.poll() is None:
                    proc.kill()


class TestFailClosed(BaseTmpRoot):
    """fail-closed 铁律(2026-10-17 修,e2e 场景四抓到的回归):

    审阅层不可用(REVIEW_ERROR,如无凭证/断网)或输出不可解析(DEGRADED)时,
    报告违规清单为空——重算只数违规曾把它洗成 GATED_PASS。守卫:合成显性
    REVIEW-INFRA 硬拦,内部错误绝不放行。"""

    @staticmethod
    def _dead_review(status, error, extra_hard=None):
        def _run(intent, ctx=None):
            return {"status": status, "passed": False, "error": error,
                    "hard_violations": list(extra_hard or []),
                    "uncertain_findings": [], "soft_warnings": [],
                    "completion_suggestions": [],
                    "deferred_to_runtime": ["H6", "H7"], "accepted_warnings": [],
                    "skipped_gates": [], "checked_at": "test", "experience": {}}
        return _run

    def _validate_with_dead_review(self, status, error, extra_hard=None):
        drive_to_confirmed("fc_case")
        with mock.patch.object(design_review, "run",
                               side_effect=self._dead_review(status, error,
                                                             extra_hard)):
            return md_proj_mcp.HANDLERS["validate_intent"](
                {"project": "t_proj", "case_id": "fc_case"})

    def test_review_error_blocked(self):
        r = self._validate_with_dead_review("REVIEW_ERROR", "无 LLM 凭证")
        self.assertEqual(r["status"], "GATED_FAIL")
        self.assertIn("REVIEW-INFRA", r["hard_gates"])
        self.assertFalse(r["gate_report"]["passed"])

    def test_degraded_blocked(self):
        r = self._validate_with_dead_review("DEGRADED", "输出不可解析")
        self.assertEqual(r["status"], "GATED_FAIL")
        self.assertIn("REVIEW-INFRA", r["hard_gates"])

    def test_review_error_keeps_structural_hard(self):
        gap = {"gate": "S-FIELD", "severity": "hard", "title": "缺失必填字段",
               "cited_fields": ["key_params.timestep_fs"]}
        r = self._validate_with_dead_review("REVIEW_ERROR", "无凭证",
                                            extra_hard=[gap])
        self.assertEqual(r["status"], "GATED_FAIL")
        for g in ("S-FIELD", "REVIEW-INFRA"):
            self.assertIn(g, r["hard_gates"])


if __name__ == "__main__":
    unittest.main()
