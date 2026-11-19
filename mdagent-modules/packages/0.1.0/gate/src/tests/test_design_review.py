# -*- coding: utf-8 -*-
"""design_review AI 审阅流水线回归:代码度量/AI 判断/经验供智(2026-10-04 架构)。

全部 LLM 调用 stub 顶替 design_review._review_llm / model_research._post_anthropic,不联网。
运行: python -m unittest tests.test_design_review -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

import design_review  # noqa: E402
import physics_gate  # noqa: E402


def base_intent():
    it = {
        "schema_version": "2.1", "case_id": "rev_case", "status": "CONFIRMED",
        "system": {"molecule": "碳氢框架", "target_elements": ["C", "H"],
                   "size": {"density_target": 0.25}, "declared_net_charge": 0.0},
        "objective": {"simulation_goal": "热解"},
        "constraints": {"temperature_range": [300, 300], "protocol_flags": {}},
        "ensemble_sequence": [{"stage": "minimize"},
                               {"stage": "pre_equil", "ensemble": "nvt", "thermostat": "langevin"},
                               {"stage": "production", "ensemble": "npt"}],
        "key_params": {"timestep_fs": 0.25, "total_steps": 600000,
                       "units": "real", "pair_style": "reax/c"},
    }
    return it


def review_payload(findings=(), completions=(), verdict="方案总体合理"):
    return {"findings": list(findings), "completions": list(completions),
            "verdict": verdict}


class LlmStub:
    """按脚本逐次返回 _review_llm 五元组;briefs 记录简报文本供断言。"""

    def __init__(self, payloads=None, infra_error=None, degraded=False):
        self.payloads = list(payloads or [])
        self.infra_error = infra_error
        self.degraded = degraded
        self.briefs = []

    def __call__(self, brief_text):
        self.briefs.append(brief_text)
        if self.infra_error:
            return None, 0, "", self.infra_error, True
        if self.degraded:
            return None, 0, "stub", "输出非合法 JSON(两次)", False
        return self.payloads.pop(0), 10, "stub", None, False


class ReviewBase(unittest.TestCase):
    def setUp(self):
        # 经验库指向临时目录(隔离种子库,按需复制种子包),保证用例确定性
        self.expdir = tempfile.mkdtemp(prefix="review_exp_")
        self._env = mock.patch.dict(os.environ, {"REVIEW_EXPERIENCE_DIR": self.expdir})
        self._env.start()
        design_review._LOAD_CACHE.clear()
        self._llm = mock.patch.object(design_review, "_review_llm")
        self.mock_llm = self._llm.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._llm.stop)
        self.addCleanup(design_review._LOAD_CACHE.clear)

    def seed_experience(self, entries):
        with open(os.path.join(self.expdir, "test_pack.json"), "w", encoding="utf-8") as f:
            json.dump({"pack": "test_pack", "schema_version": 1, "entries": entries}, f,
                      ensure_ascii=False)
        design_review._LOAD_CACHE.clear()

    def run_review(self, intent, **kw):
        stub = LlmStub(**kw)
        self.mock_llm.side_effect = stub
        report = design_review.run(intent, {})
        report["_stub"] = stub
        return report


class TestReviewPipeline(ReviewBase):
    def test_clean_plan_passes_with_experience_cited(self):
        self.seed_experience([
            {"id": "exp-t-001", "kind": "boundary",
             "statement": "测试边界", "severity_hint": "hard", "status": "active"}])
        r = self.run_review(base_intent(), payloads=[review_payload()])
        self.assertEqual(r["status"], "OK")
        self.assertTrue(r["passed"])
        self.assertEqual(r["hard_violations"], [])
        self.assertIn("exp-t-001", r["experience"]["used_ids"])
        """审阅简报必须带度量事实/结构缺口/经验条目——AI 判断的供智三件套。"""
        self.seed_experience([
            {"id": "exp-t-002", "kind": "lesson", "statement": "摩擦控温伪影",
             "severity_hint": "soft", "status": "active",
             "conditions": {"family": "friction"}}])
        it = base_intent()
        del it["key_params"]["timestep_fs"]  # 制造结构缺口
        it["experiment_design"] = {"experiment_type": "friction_indentation",
                                    "setup": {"indentation_depth_A": 5.0}}
        r = self.run_review(it, payloads=[review_payload()])
        brief = r["_stub"].briefs[0]
        self.assertIn("度量事实", brief)
        self.assertIn("key_params.timestep_fs", brief)  # 结构缺口进简报
        self.assertIn("exp-t-002", brief)               # 经验条目进简报
        self.assertIn("$", brief)                       # 事实名供 evidence_refs 引用

    def test_structural_gap_merged_as_hard_deterministically(self):
        """缺必填字段=hard,不依赖 AI(AI 返回空 findings 也要拦)。"""
        it = base_intent()
        del it["key_params"]["timestep_fs"]
        r = self.run_review(it, payloads=[review_payload()])
        self.assertFalse(r["passed"])
        hard = [v for v in r["hard_violations"] if v["gate"] == "S-FIELD"]
        self.assertEqual(hard[0]["cited_fields"], ["key_params.timestep_fs"])
        self.assertEqual(hard[0]["evidence_refs"], ["structural"])

    def test_fabricated_evidence_downgraded(self):
        """伪造引用(简报没出现过的 id)→ unverified 且 hard 降 uncertain(三档:判不了)。"""
        self.seed_experience([{"id": "exp-real-1", "kind": "boundary",
                                "statement": "真经验", "status": "active"}])
        it = base_intent()
        it["key_params"]["timestep_fs"] = 0.5
        r = self.run_review(it, payloads=[review_payload(findings=[{
            "aspect": "动力学", "severity": "hard", "title": "步长过大",
            "detail": "0.5fs", "rationale": "自热",
            "cited_fields": ["key_params.timestep_fs"],
            "evidence_refs": ["exp-fabricated-999"],   # 伪造
            "remediation": ["降到 0.25fs"]}])])
        self.assertEqual([v for v in r["hard_violations"] if v["gate"] == "REVIEW"], [])
        unc = [w for w in r["uncertain_findings"] if w["title"] == "步长过大"]
        self.assertTrue(unc[0]["unverified"])
        self.assertEqual(unc[0]["evidence_refs"], [])  # 伪造引用被剔除
        self.assertFalse(r["passed"])                  # uncertain 不放行

    def test_verified_hard_finding_kept(self):
        self.seed_experience([{"id": "exp-real-2", "kind": "boundary",
                                "statement": "步长边界", "status": "active"}])
        it = base_intent()
        it["key_params"]["timestep_fs"] = 0.5
        r = self.run_review(it, payloads=[review_payload(findings=[{
            "aspect": "动力学", "severity": "hard", "title": "步长过大",
            "detail": "0.5fs 超界", "rationale": "自热事故",
            "cited_fields": ["key_params.timestep_fs"],
            "evidence_refs": ["exp-real-2"],
            "remediation": ["降到 0.25fs"]}])])
        hard = [v for v in r["hard_violations"] if v["gate"] == "REVIEW"]
        self.assertEqual(len(hard), 1)
        self.assertFalse(hard[0]["unverified"])
        self.assertFalse(r["passed"])

    def test_fact_reference_counts_as_evidence(self):
        """引用 $事实名(代码实测)算真凭据;引用不存在的事实名同样降级。"""
        self.seed_experience([{"id": "exp-fact-1", "kind": "boundary",
                                "statement": "电荷核对", "status": "active"}])
        it = base_intent()
        # 场景 A:引用真实存在的事实(seq 结构事实) → 硬拦成立
        r = self.run_review(it, payloads=[review_payload(findings=[{
            "aspect": "动力学", "severity": "hard", "title": "有事实依据的硬拦",
            "detail": "d", "rationale": "r",
            "cited_fields": ["key_params.timestep_fs"],
            "evidence_refs": ["$seq_n_stages"], "remediation": []}])])
        self.assertEqual(len([v for v in r["hard_violations"] if v["gate"] == "REVIEW"]), 1)
        # 场景 B:引用不存在的事实名 → 无效 → 降 uncertain
        r2 = self.run_review(it, payloads=[review_payload(findings=[{
            "aspect": "体系", "severity": "hard", "title": "幻觉事实引用",
            "detail": "d", "rationale": "r",
            "cited_fields": ["system.declared_net_charge"],
            "evidence_refs": ["$charge_delta"], "remediation": []}])])
        self.assertEqual([v for v in r2["hard_violations"] if v["gate"] == "REVIEW"], [])
        self.assertTrue(any(w["unverified"] for w in r2["uncertain_findings"]))

    def test_hallucinated_cited_fields_dropped(self):
        it = base_intent()
        r = self.run_review(it, payloads=[review_payload(findings=[{
            "aspect": "幻觉", "severity": "soft", "title": "标题",
            "detail": "d", "rationale": "r",
            "cited_fields": ["no.such.path", "key_params.timestep_fs"],
            "evidence_refs": [], "remediation": []}])])
        w = [x for x in r["soft_warnings"] if x["title"] == "标题"][0]
        self.assertEqual(w["cited_fields"], ["key_params.timestep_fs"])  # 幻觉路径被剔除

    def test_canary_experience_cannot_support_hard(self):
        """canary(试验性)经验不能撑 hard——降 uncertain(判不了),须消解后才放行。"""
        self.seed_experience([{"id": "exp-canary-1", "kind": "boundary",
                                "statement": "试验边界", "status": "canary"}])
        it = base_intent()
        r = self.run_review(it, payloads=[review_payload(findings=[{
            "aspect": "实验设计", "severity": "hard", "title": "canary 依据的硬拦",
            "detail": "d", "rationale": "r",
            "cited_fields": ["key_params.timestep_fs"],
            "evidence_refs": ["exp-canary-1"], "remediation": []}])])
        self.assertEqual([v for v in r["hard_violations"] if v["gate"] == "REVIEW"], [])
        unc = [w for w in r["uncertain_findings"] if w["title"] == "canary 依据的硬拦"]
        self.assertTrue(unc[0]["canary_experience"])
        self.assertFalse(r["passed"])  # uncertain 阻断放行

    def test_completions_from_guidance_and_ai_merged(self):
        it = base_intent()
        del it["key_params"]["total_steps"]
        r = self.run_review(it, payloads=[review_payload(
            findings=[], completions=[{"aspect": "分析口径", "why": "缺指标定义",
                                        "proposal": "补 RDF/密度分析定义"}])])
        aspects = {c["aspect"] for c in r["completion_suggestions"]}
        self.assertIn("分析口径", aspects)          # AI 发现的缺口
        self.assertIn("key_params.total_steps", aspects)  # 结构缺口+种子素材
        self.assertFalse(r["passed"])      # 结构缺口仍是 hard

    def test_infra_failure_review_error_fail_closed(self):
        """无凭证/断网=REVIEW_ERROR,fail-closed 不放行,但结构缺口仍如实报。"""
        it = base_intent()
        del it["objective"]
        stub = LlmStub(infra_error="RuntimeError: 找不到任何 LLM 凭证")
        self.mock_llm.side_effect = stub
        r = design_review.run(it, {})
        self.assertEqual(r["status"], "REVIEW_ERROR")
        self.assertFalse(r["passed"])
        # 结构缺口不依赖 AI,失败时也如实报 hard(fail-closed 但不隐瞒事实)
        self.assertIn("simulation_goal", json.dumps(r["hard_violations"], ensure_ascii=False))

    def test_invalid_json_degraded(self):
        r = self.run_review(base_intent(), degraded=True)
        self.assertEqual(r["status"], "DEGRADED")
        self.assertFalse(r["passed"])
        self.assertIn("brief_digest", r)

    def test_experience_hot_reload(self):
        """运行时注入:落盘新经验 → 下一次 run 即生效(不改代码)。"""
        it = base_intent()
        r1 = self.run_review(it, payloads=[review_payload()])
        self.assertNotIn("exp-hot-1", json.dumps(r1["experience"]))
        self.seed_experience([{"id": "exp-hot-1", "kind": "lesson",
                                "statement": "热注入经验", "status": "active"}])
        r2 = self.run_review(it, payloads=[review_payload()])
        self.assertIn("exp-hot-1", r2["experience"]["used_ids"])
        self.assertIn("exp-hot-1", r2["_stub"].briefs[0])  # 进了审阅简报

    def test_bad_pack_quarantined_not_crashing(self):
        with open(os.path.join(self.expdir, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.seed_experience([{"id": "ok-1", "kind": "lesson", "statement": "好条目",
                                "status": "active"},
                               {"id": "bad id!", "statement": "非法 id", "status": "active"}])
        r = self.run_review(base_intent(), payloads=[review_payload()])
        self.assertEqual(r["status"], "OK")
        self.assertIn("ok-1", r["experience"]["used_ids"])
        self.assertTrue(any(q["file"] == "broken.json"
                            for q in r["experience"]["quarantined"]))
        self.assertTrue(any("非法条目" in json.dumps(q, ensure_ascii=False)
                            for q in r["experience"]["quarantined"]))

    def test_revoked_entries_not_injected(self):
        self.seed_experience([{"id": "exp-dead-1", "kind": "lesson",
                                "statement": "已撤回", "status": "revoked"}])
        r = self.run_review(base_intent(), payloads=[review_payload()])
        self.assertNotIn("exp-dead-1", r["experience"]["used_ids"])

    def test_runtime_gaps_report_shape_compat(self):
        """报告形状与 mdproj 兼容:hard/soft/completions/deferred/accepted/skipped 齐备。"""
        r = self.run_review(base_intent(), payloads=[review_payload()])
        for key in ("passed", "hard_violations", "soft_warnings", "completion_suggestions",
                    "deferred_to_runtime", "accepted_warnings", "checked_at", "experience"):
            self.assertIn(key, r)
        self.assertEqual(r["deferred_to_runtime"], physics_gate.DEFERRED_TO_RUNTIME)


class TestSeedPackIntegrity(ReviewBase):
    """经验库种子自检(2026-10-17 加;experiment_abstraction 出生即坏 4 天无人知的教训):

    种子目录所有 *.json 必须可解析;归属本流水线(无 consumer 标记)的包条目全过
    _entry_ok;外来包必须显式声明 consumer;load_experience 在种子目录上零隔离零拒收。"""

    SEED_DIR = os.path.join(MCP_DIR, "review_experience")

    def test_seed_packs_parse_and_own_entries_valid(self):
        files = [fn for fn in sorted(os.listdir(self.SEED_DIR))
                 if fn.endswith(".json") and not fn.startswith("_")]
        self.assertTrue(files, "种子目录为空")
        own, foreign = 0, []
        for fn in files:
            with open(os.path.join(self.SEED_DIR, fn), encoding="utf-8") as f:
                pack = json.load(f)  # 不可解析=用例失败(experiment_abstraction 回归守卫)
            self.assertEqual(pack.get("schema_version"), 1, fn)
            consumer = str(pack.get("consumer") or "").strip()
            if consumer not in ("", "design-review"):
                foreign.append(fn)  # 证据系统共享包,本流水线整包跳过
                continue
            own += 1
            for e in pack.get("entries") or []:
                self.assertTrue(design_review._entry_ok(e),
                                "%s 条目 %s 未过校验" % (fn, e.get("id")))
        self.assertGreater(own, 0)
        # 外来包只允许显式声明过的(不允许无标记的非本流水线 schema 混进"非法条目"计数)
        self.assertEqual(foreign, ["anchors_seed.json", "confirmation_surface.json",
                                   "methodology_deep.json"])

    def test_load_experience_all_green_on_seeds(self):
        exp = design_review.load_experience(base_intent())
        self.assertEqual(exp["quarantined"], [], "种子库存在隔离/拒收条目")
        packs = {p["pack"] for p in exp["packs"]}
        self.assertIn("experiment_abstraction", packs)   # 回归守卫:曾被出生即坏的包
        self.assertIn("methodology_seed", packs)         # 共享包(带 statement)注入
        self.assertNotIn("physics_anchors", packs)       # 外来包整包跳过
        self.assertNotIn("methodology_deep", packs)


if __name__ == "__main__":
    unittest.main()
