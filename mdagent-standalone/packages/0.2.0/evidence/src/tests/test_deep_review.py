#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""deep_review(方法论深度分析)单测:族匹配/维度遍历/弹性模量方法完整分析/液体族。"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "evolution"))

import stage_state   # noqa: E402
import deep_review   # noqa: E402


ELASTIC_INPUT_OK = """lattice fcc 3.615
fix 1 all box/relax iso 0.0
minimize
change_box all x scale 1.004
minimize
"""
ELASTIC_INPUT_NO_RELAX = """lattice fcc 3.615
change_box all x scale 1.004
minimize
"""


class TestDeepReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deep_rev_")
        self.cid = "TASK-UT-DEEP"
        stage_state.init_task(self.cid, data_root=self.tmp)

    def _dec(self, text):
        stage_state.lock_decisions(self.cid, {"D1_对象与范围": text}, self.tmp)

    def test_family_match(self):
        self.assertEqual(deep_review.match_family("铜的弹性模量 C11"), "elastic-constants")
        self.assertEqual(deep_review.match_family("液氩熔化 RDF 结构"), "liquid-md")
        self.assertEqual(deep_review.match_family("基态晶格常数与结合能"), "ground-state")
        self.assertIsNone(deep_review.match_family("随便一个东西"))

    def test_elastic_full_analysis(self):
        """弹性模量方法完整分析:五维度+通用步长,含真实数据驱动的一致性判据。"""
        self._dec("fcc Cu 弹性模量 C11(应变能法;0K 静态;对照 oracle 163.6/实验 300K)")
        facts = {"atom_count": 108,
                 "c11_small_amp": 167.1,   # ±0.4% 幅值
                 "c11_large_amp": 167.5}   # ±0.8% 幅值
        rec, ref = deep_review.analyze(
            self.cid, "results", stage_state.read_decisions(self.cid, self.tmp),
            facts, data_root=self.tmp, input_text=ELASTIC_INPUT_OK)
        by_id = {f["id"]: f for f in rec["findings"]}
        self.assertIn("mdev-ec-method", by_id)
        self.assertIn("mdev-ec-strain-linear", by_id)
        self.assertIn("mdev-ec-relax", by_id)
        self.assertIn("mdev-ec-size", by_id)
        self.assertIn("mdev-ec-temp-basis", by_id)
        self.assertIn("mdev-general-dt", by_id)
        # 一致性判据(实际数据驱动): 167.1 vs 167.5 → 0.24% ≤ 15% → sound
        self.assertEqual(by_id["mdev-ec-strain-linear"]["verdict"], "sound")
        self.assertIn("0.2%", by_id["mdev-ec-strain-linear"]["evidence"])
        # 弛豫在输入中 → sound
        self.assertEqual(by_id["mdev-ec-relax"]["verdict"], "sound")
        # 尺寸 108 ≥ 108 → sound
        self.assertEqual(by_id["mdev-ec-size"]["verdict"], "sound")
        # 温度口径: 决策文本含"0K"和"实验 300K" → sound
        self.assertEqual(by_id["mdev-ec-temp-basis"]["verdict"], "sound")
        self.assertEqual(rec["critical_open"], [])

    def test_elastic_nonlinear_detected(self):
        """两组幅值 C11 偏差大 → 非线性污染被抓住(critical)。"""
        self._dec("Cu 弹性模量 C11 应变能法 0K oracle")
        facts = {"atom_count": 108, "c11_small_amp": 150.0, "c11_large_amp": 180.0}
        rec, _ = deep_review.analyze(
            self.cid, "results", stage_state.read_decisions(self.cid, self.tmp),
            facts, data_root=self.tmp, input_text=ELASTIC_INPUT_OK)
        by_id = {f["id"]: f for f in rec["findings"]}
        self.assertEqual(by_id["mdev-ec-strain-linear"]["verdict"], "critical")
        self.assertEqual(rec["proceed"], "not-recommended")

    def test_elastic_missing_relax_detected(self):
        """输入缺 box/relax → critical(应力基准不是平衡态)。"""
        self._dec("Cu 弹性模量 C11")
        facts = {"atom_count": 108, "c11_small_amp": 165.0, "c11_large_amp": 166.0}
        rec, _ = deep_review.analyze(
            self.cid, "results", stage_state.read_decisions(self.cid, self.tmp),
            facts, data_root=self.tmp, input_text=ELASTIC_INPUT_NO_RELAX)
        by_id = {f["id"]: f for f in rec["findings"]}
        self.assertEqual(by_id["mdev-ec-relax"]["verdict"], "critical")

    def test_elastic_missing_facts_concern(self):
        """缺幅值数据 → concern(无法核验,不静默放行)。"""
        self._dec("Cu 弹性模量 C11")
        rec, _ = deep_review.analyze(
            self.cid, "results", stage_state.read_decisions(self.cid, self.tmp),
            {}, data_root=self.tmp, input_text=ELASTIC_INPUT_OK)
        by_id = {f["id"]: f for f in rec["findings"]}
        self.assertEqual(by_id["mdev-ec-strain-linear"]["verdict"], "concern")
        self.assertIn("两组独立幅值", by_id["mdev-ec-strain-linear"]["evidence"])

    def test_liquid_family(self):
        # 决策里声明了步长 → 步长维度现在自己换算 dt*（不再依赖外部写入的键）
        self._dec("液氩 90K 液相密度与 RDF，步长 dt=2fs")
        liq_input = "fix 1 all nvt temp 90 90\nfix 2 all npt\ncompute rdf\nfix ave/time\n"
        rec, _ = deep_review.analyze(
            self.cid, "results", stage_state.read_decisions(self.cid, self.tmp),
            {},
            data_root=self.tmp, input_text=liq_input)
        by_id = {f["id"]: f for f in rec["findings"]}
        self.assertIn("mdev-liq-equil", by_id)
        self.assertIn("mdev-liq-sampling", by_id)
        self.assertEqual(by_id["mdev-liq-equil"]["verdict"], "sound")
        self.assertEqual(by_id["mdev-liq-sampling"]["verdict"], "sound")
        self.assertEqual(by_id["mdev-general-dt"]["verdict"], "sound")

    def test_ground_state_family(self):
        self._dec("LJ 氩基态晶格常数与结合能(起始 5.25 偏离目标)")
        gs_input = "fix 1 all box/relax iso 0.0\nminimize\n"
        rec, _ = deep_review.analyze(
            self.cid, "plan", stage_state.read_decisions(self.cid, self.tmp),
            {}, data_root=self.tmp, input_text=gs_input)
        by_id = {f["id"]: f for f in rec["findings"]}
        self.assertEqual(by_id["mdev-gs-relax"]["verdict"], "sound")
        self.assertEqual(by_id["mdev-gs-start-off"]["verdict"], "sound")

    def test_unknown_check_type_concern(self):
        """条目声明了引擎不认识的检查类型 → concern(不静默跳过)。"""
        dims = {"elastic-constants": [{
            "id": "mdev-x-bad", "kind": "methodology-dimension",
            "family": "elastic-constants", "question": "x",
            "check": {"type": "nonexistent", "params": {}}, "status": "active"}]}
        orig = deep_review.load_dimensions
        deep_review.load_dimensions = lambda dr=None: dict(
            dims, **{"*": orig(self.tmp).get("*", [])})
        try:
            self._dec("Cu 弹性模量 C11")
            rec, _ = deep_review.analyze(
                self.cid, "plan", stage_state.read_decisions(self.cid, self.tmp),
                {}, data_root=self.tmp, input_text="")
            by_id = {f["id"]: f for f in rec["findings"]}
            self.assertEqual(by_id["mdev-x-bad"]["verdict"], "concern")
            self.assertIn("未知检查类型", by_id["mdev-x-bad"]["evidence"])
        finally:
            deep_review.load_dimensions = orig

    def test_record_immutable_and_cited(self):
        """深度分析记录不可变;每个维度 basis 引用知识条目 id。"""
        self._dec("Cu 弹性模量 C11")
        rec, ref = deep_review.analyze(
            self.cid, "plan", stage_state.read_decisions(self.cid, self.tmp),
            {}, data_root=self.tmp, input_text=ELASTIC_INPUT_OK)
        for f in rec["findings"]:
            self.assertTrue(all(b.startswith("exp:mdev-") for b in f["basis"]))
        fp = os.path.join(stage_state.task_dir(self.cid, self.tmp), ref)
        with self.assertRaises(FileExistsError):
            with open(fp, "x", encoding="utf-8") as f:
                f.write("{}")


if __name__ == "__main__":
    unittest.main()
