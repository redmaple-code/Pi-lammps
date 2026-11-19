# -*- coding: utf-8 -*-
"""model_research 研讨流水线回归门:拆题确定性、并行收集容错、评测器重试、
代码终裁(钳分/零引用压分/来源去重)、全断网不抛。

全部 HTTP 用 stub 顶替 model_research._post_anthropic,不联网。
运行: python3.11 -m unittest discover -s tests -v (在 dsh-agent/ 下)
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

import model_research  # noqa: E402


def collector_payload(note, urls):
    """伪造一路收集器响应:text + web_search_tool_result。"""
    return {"content": [
        {"type": "text", "text": note},
        {"type": "web_search_tool_result",
         "content": [{"type": "web_search_result", "url": u, "title": "t%d" % i}
                     for i, u in enumerate(urls)]},
    ], "usage": {"input_tokens": 100, "output_tokens": 50}}


def evaluator_payload(strategies, verdict="用 A"):
    return {"content": [{"type": "text", "text": json.dumps(
        {"strategies": strategies, "verdict": verdict}, ensure_ascii=False)}],
        "usage": {"input_tokens": 200, "output_tokens": 100}}


class Stub:
    """按请求体是否带 tools 区分收集器/评测器;fail_idx 里的收集器抛网络错。"""

    def __init__(self, strategies=None, fail_idx=(), evaluator_texts=None):
        self.fail_idx = set(fail_idx)
        self.collector_calls = 0
        self.evaluator_texts = evaluator_texts  # 非法输出脚本(逐次)
        self.strategies = strategies if strategies is not None else [
            {"name": "参数集A", "score": 85, "evidence": ["http://a/0"],
             "risks": ["高温外推差"], "when_to_use": "CHO 常温"},
            {"name": "参数集B", "score": 92, "evidence": [],  # 零引用
             "risks": [], "when_to_use": "无来源"},
        ]

    def __call__(self, body, timeout, api_key=None):
        if "tools" in body:  # 收集器
            idx = self.collector_calls
            self.collector_calls += 1
            if idx in self.fail_idx:
                raise ConnectionError("simulated network down")
            return (collector_payload("子课题%d 笔记:有关键数字" % idx,
                                      ["http://a/%d" % idx, "http://b/%d" % idx]),
                    "stub")
        # 评测器
        if self.evaluator_texts:
            text = self.evaluator_texts.pop(0)
            return ({"content": [{"type": "text", "text": text}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}, "stub")
        return evaluator_payload(self.strategies), "stub"


class TestRun(unittest.TestCase):
    def setUp(self):
        self.old_post = model_research._post_anthropic
        self.addCleanup(setattr, model_research, "_post_anthropic", self.old_post)

    def _run(self, stub, q="ReaxFF CHO 选哪个参数集"):
        model_research._post_anthropic = stub
        return model_research.run(q)

    def test_happy_path_sorted_and_capped(self):
        r = self._run(Stub())
        self.assertEqual(r["status"], "OK")
        # 零引用策略被压到 UNVERIFIED_SCORE_CAP 且排后
        self.assertEqual(r["strategies"][0]["name"], "参数集A")
        self.assertTrue(r["strategies"][0]["verified"])
        self.assertFalse(r["strategies"][1]["verified"])
        self.assertLessEqual(r["strategies"][1]["score"],
                             model_research.UNVERIFIED_SCORE_CAP)
        self.assertEqual(r["verdict"], "用 A")
        self.assertEqual(len(r["sources"]), 8)  # 4 路 × 2 URL 去重后
        self.assertEqual(r["cost"]["llm_calls"], 5)

    def test_plan_deterministic(self):
        r = self._run(Stub())
        self.assertEqual(len(r["sub_queries"]), 4)
        self.assertTrue(all("ReaxFF CHO" in q for q in r["sub_queries"]))

    def test_partial_collector_failure_still_ok(self):
        r = self._run(Stub(fail_idx={1, 2}))
        self.assertEqual(r["status"], "OK")
        self.assertEqual(len(r["degraded"]), 2)
        self.assertIn("ConnectionError", r["degraded"][0]["error"])

    def test_all_collectors_fail_returns_error_not_raise(self):
        r = self._run(Stub(fail_idx={0, 1, 2, 3}))
        self.assertEqual(r["status"], "RESEARCH_ERROR")
        self.assertNotIn("strategies", r)

    def test_evaluator_garbage_twice_degrades(self):
        r = self._run(Stub(evaluator_texts=["我想说……", "还是不是JSON"]))
        self.assertEqual(r["status"], "DEGRADED")
        self.assertIn("notes", r)
        self.assertNotIn("strategies", r)

    def test_evaluator_retry_recovers(self):
        r = self._run(Stub(evaluator_texts=[
            "垃圾输出",
            json.dumps({"strategies": [{"name": "X", "score": 70,
                                        "evidence": ["http://a/0"], "risks": [],
                                        "when_to_use": "w"}], "verdict": "选 X"})]))
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["strategies"][0]["name"], "X")

    def test_score_clamped_and_fake_url_filtered(self):
        stub = Stub(strategies=[
            {"name": "S", "score": 250, "evidence": ["http://fake/9", "http://a/1"],
             "risks": [], "when_to_use": ""}])
        r = self._run(stub)
        s = r["strategies"][0]
        self.assertEqual(s["score"], 100, "评分必须钳到 0-100")
        self.assertEqual(s["evidence"], ["http://a/1"], "编造 URL 必须被剔除")
        self.assertTrue(s["verified"])

    def test_empty_question_rejected(self):
        r = model_research.run("  ")
        self.assertEqual(r["status"], "RESEARCH_ERROR")

    def test_repair_mode_templates_and_mode_field(self):
        r = self._run(Stub(), q="NPT 阶段温度爆炸怎么修")
        # 默认模式仍是 select
        self.assertEqual(r["mode"], "select")
        model_research._post_anthropic = Stub()
        r = model_research.run("NPT 阶段温度爆炸怎么修", mode="repair")
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["mode"], "repair")
        self.assertTrue(all("修" in q or "病因" in q or "副作用" in q or "案例" in q
                            for q in r["sub_queries"]))
        self.assertIn("官方病因", r["sub_queries"][0])

    def test_bogus_mode_falls_back_to_select(self):
        model_research._post_anthropic = Stub()
        r = model_research.run("哪个参数集稳", mode="nonsense")
        self.assertEqual(r["mode"], "select")
        self.assertIn("候选力场/参数集清单", r["sub_queries"][0])

    def test_repair_research_handler_shape(self):
        sys.path.insert(0, MCP_DIR)
        import md_mcp
        model_research._post_anthropic = Stub()
        try:
            # anomalies 自动拼题
            r = md_mcp.sim_repair_research(
                {"anomalies": ["close contacts 警告", "TotEng 跳变"], "case_id": "run1"})
            self.assertEqual(r["status"], "OK")
            self.assertEqual(r["mode"], "repair")
            self.assertIn("close contacts", r["question"])
            self.assertIn("case=run1", r["question"])
            self.assertIn("_discipline", r)
            self.assertIn("sim_repair_research", md_mcp.HANDLERS)
            # 空 question 且无 anomalies/快照 → RESEARCH_ERROR 而非抛异常
            r2 = md_mcp.sim_repair_research({"case_id": "no_such_case_xyz"})
            self.assertEqual(r2["status"], "RESEARCH_ERROR")
        finally:
            model_research._post_anthropic = self.old_post

    def test_mcp_handler_shape(self):
        sys.path.insert(0, MCP_DIR)
        import md_mcp
        model_research._post_anthropic = Stub()
        try:
            r = md_mcp.sim_model_research({"question": "哪个力场稳"})
            self.assertEqual(r["status"], "OK")
            self.assertIn("_discipline", r)
            self.assertIn("sim_model_research", md_mcp.HANDLERS)
        finally:
            model_research._post_anthropic = self.old_post


class TestProviderChain(unittest.TestCase):
    """2026-09-14 Kimi 401 修复的回归: BigModel 形态解析 + 供应商链契约。"""

    def test_parse_blocks_bigmodel_string_tool_result(self):
        # BigModel 的 tool_result.content 是纯字符串(非 Kimi 的结构化列表),
        # URL 靠正则收割;尾随标点要剥掉,去重保留首见
        payload = {"content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "server_tool_use", "name": "web_search"},
            {"type": "tool_result",
             "content": "结果1 https://docs.lammps.org/qeq.html, 结果2 "
                        "https://matsci.org/t/123。"},
            {"type": "text", "text": "结论…https://docs.lammps.org/qeq.html 重复"},
        ], "usage": {"input_tokens": 10, "output_tokens": 5}}
        text, sources, tokens = model_research._parse_blocks(payload)
        urls = [s["url"] for s in sources]
        self.assertEqual(urls, ["https://docs.lammps.org/qeq.html",
                                "https://matsci.org/t/123"])  # 去重+剥尾点
        self.assertEqual(tokens, 15)
        self.assertIn("结论", text)

    def test_parse_blocks_kimi_structured(self):
        payload = {"content": [
            {"type": "web_search_tool_result", "content": [
                {"url": "http://k/1", "title": "t1", "page_age": "2025"},
                {"url": "http://k/1"},  # 重复
            ]},
        ], "usage": {}}
        _, sources, _ = model_research._parse_blocks(payload)
        self.assertEqual([s["url"] for s in sources], ["http://k/1"])
        self.assertEqual(sources[0]["title"], "t1")

    def test_post_anthropic_returns_provider(self):
        # 契约: 返回 (payload, provider); 401 走链不抛单点错误
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req.full_url)

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"content": [{"type": "text", "text": "ok"}],
                                       "usage": {}}).encode()

            return R()

        old_p = model_research._providers
        old_u = getattr(model_research.urllib.request, "urlopen")
        model_research._providers = lambda: [("kimi-k3", "http://kimi/x", "k3", "k"),
                                             ("glm-4.6", "http://glm/y", "glm-4.6", "g")]
        model_research.urllib.request.urlopen = fake_urlopen
        try:
            payload, prov = model_research._post_anthropic(
                {"max_tokens": 1000, "messages": []}, 5)
            self.assertEqual(prov, "kimi-k3")
            self.assertEqual(payload["content"][0]["text"], "ok")
            # glm 路径: max_tokens 放大 2.5 倍(思考链吃预算)
            body_seen = {}

            def fake_urlopen2(req, timeout):
                body_seen["v"] = json.loads(req.data.decode())
                return fake_urlopen(req, timeout)

            model_research.urllib.request.urlopen = fake_urlopen2
            model_research._providers = lambda: [("glm-4.6", "http://glm/y",
                                                  "glm-4.6", "g")]
            _, prov2 = model_research._post_anthropic(
                {"max_tokens": 2000, "messages": []}, 5)
            self.assertEqual(prov2, "glm-4.6")
            self.assertEqual(body_seen["v"]["max_tokens"], 5000)  # 2.5 倍放大
            self.assertEqual(body_seen["v"]["model"], "glm-4.6")
            # 下限: 小 body 也至少 3000
            _, _ = model_research._post_anthropic(
                {"max_tokens": 100, "messages": []}, 5)
            self.assertEqual(body_seen["v"]["max_tokens"], 3000)
        finally:
            model_research._providers = old_p
            model_research.urllib.request.urlopen = old_u


class TestDesignMode(unittest.TestCase):
    """design 模式(2026-09-30 前置双门 C2e):实验设计合理性外部论证。"""

    def setUp(self):
        self.old_post = model_research._post_anthropic
        self.addCleanup(setattr, model_research, "_post_anthropic", self.old_post)

    def test_templates_and_rubric_registered(self):
        self.assertIn("design", model_research.MODE_TEMPLATES)
        self.assertIn("design", model_research.MODE_RUBRICS)
        self.assertTrue(any("文献基准" in t for t in model_research.MODE_TEMPLATES["design"]))
        self.assertIn("实验设计评审", model_research.MODE_RUBRICS["design"])

    def test_design_run_uses_design_subqueries(self):
        stub = Stub(strategies=[
            {"name": "indentation_depth_A", "score": 80,
             "evidence": ["http://a/0"], "risks": [], "when_to_use": "0.05-0.2 R"},
            {"name": "sliding_velocity_m_s", "score": 60,
             "evidence": ["http://b/1"], "risks": ["速率外推"], "when_to_use": "1-100 m/s"},
        ])
        model_research._post_anthropic = stub
        r = model_research.run("高熵合金 CoCrFeNi 摩擦 MD 设计论证: R=50Å 压深5Å 滑速20m/s",
                               mode="design")
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["mode"], "design")
        # 拆题必须用 design 模板(纯代码路由,不许落回 select)
        self.assertTrue(all("文献基准" in q or "经典判据" in q or "设计缺陷" in q
                            or "实验对照" in q for q in r["sub_queries"]))
        self.assertEqual(r["strategies"][0]["name"], "indentation_depth_A")
        self.assertTrue(r["strategies"][0]["verified"])

    def test_unknown_mode_falls_back_select(self):
        stub = Stub()
        model_research._post_anthropic = stub
        r = model_research.run("随便问", mode="nonexistent")
        self.assertEqual(r["mode"], "select")  # 未知模式回落 select(既有行为不变)


if __name__ == "__main__":
    unittest.main(verbosity=2)
