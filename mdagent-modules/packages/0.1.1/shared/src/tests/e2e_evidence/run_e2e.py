#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""证据系统 E2E v2.2 —— 按 E1-E5 走一遍完整"设计+计算"流程。

流程：初始化 → G-PLAN（无设计/无依据被拒 → 补分级依据 + 评估 → 过闸）
→ G-BUILD（坏配置被 E4 拦 → 合规配置过闸）→ G-CONFIG → G-EXEC
→ 真实计算（子进程）→ factcheck → G-ANALYZE（错值被 E2 拦 → 真值过闸 + E5 覆盖）
→ 人工覆盖（缺理由→冲突包→答复→放行→二次确认晋升）。

每步一行核验表；任一 FAIL 退出码 1。所有数字来自真实执行，不伪造。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_SRC, "evolution"))
sys.path.insert(0, os.path.join(_SRC, "mcp"))

import stage_state  # noqa: E402
import stage_gate   # noqa: E402
import content_check  # noqa: E402
import surface_fixture  # noqa: E402

RUNS = os.path.join(_HERE, "runs")
RUN_ID = time.strftime("%Y%m%dT%H%M%S")
DATA = os.path.join(RUNS, RUN_ID, "data")
os.makedirs(DATA, exist_ok=True)
os.environ["MDSIM_DATA_ROOT"] = DATA

RESULTS = []
STD = "std:Allen-Tildesley-1989-Computer-Simulation-of-Liquids"


def check(cid, desc, ok, detail=""):
    RESULTS.append((cid, desc, bool(ok), str(detail)[:200]))
    print("%s [%-14s] %s %s" % ("✅" if ok else "❌", cid, desc,
                                ("| " + str(detail)[:140]) if detail else ""))


def wjson(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def rec(cid, gate, stage, **kw):
    r = {"schema_version": 1, "case_id": cid, "stage": stage, "gate": gate,
         "decision": "PASS", "summary": "e2e", "evidence_refs": [],
         "acceptance_checks": [], "input_files": []}
    r.update(kw)
    return r


def gate(cid, record, facts=None):
    return stage_gate.gate_and_log(cid, record, data_root=DATA, facts=facts or {})


def setup_case(cid, risk="medium", basis=None, decisions=None):
    stage_state.init_task(cid, risk_level=risk, data_root=DATA)
    d = os.path.join(stage_state.task_dir(cid, DATA), "confirmations")
    wjson(os.path.join(d, "%s-brief.json" % cid),
          {"id": "%s-brief" % cid, "scope": cid, "value": "e2e 案例",
           "reason": "用户测试需求", "ts": "2026-10-19T00:00:00Z"})
    surface_fixture.fill_all(DATA, cid)
    stage_state.lock_decisions(cid, decisions or {
        "D1_对象与范围": "蒙特卡洛估算圆周率(基线)",
        "D5_关键参数": "N=200000, seed=42",
        "D7_验收标准": ["|pi_hat-3.14159|<=0.01"]}, DATA, basis=basis)
    pa = {"schema_version": 1, "case_id": cid, "phase": "plan",
          "findings": [], "critical_open": [], "overall": "sound",
          "proceed": "recommended"}
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "assessments", "plan-1.json"), pa)


def add_results_assessment(cid, crit=None):
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "assessments", "res-1.json"),
          {"schema_version": 1, "case_id": cid, "phase": "results",
           "findings": [], "critical_open": crit or [], "overall": "sound",
           "proceed": "recommended"})


def main():
    ok_all = True

    # ---------- 1. G-PLAN：无设计 → E1 ----------
    cid = "E2E-01"
    stage_state.init_task(cid, data_root=DATA)
    r = gate(cid, rec(cid, "G-PLAN", "PLAN"))
    ok = r["result"] == "BLOCK" and any(x.startswith("E1:no-design-decisions")
                                       for x in r["reasons"])
    check("E1-no-design", "无设计决策 → G-PLAN 拒", ok, r["reasons"])
    ok_all &= ok

    # ---------- 2. G-PLAN：无依据 → E1 ----------
    setup_case(cid, basis=None)
    r = gate(cid, rec(cid, "G-PLAN", "PLAN"))
    ok = r["result"] == "BLOCK" and any(x.startswith("E1:evidence-insufficient")
                                       for x in r["reasons"])
    check("E1-no-basis", "决策无依据 → E1 拒", ok, r["reasons"])
    ok_all &= ok

    # ---------- 3. G-PLAN：论文依据 + 评估 → GO ----------
    cid = "E2E-02"
    setup_case(cid, basis={
        "D1_对象与范围": {"kind": "user", "ref": "user:%s-brief" % cid,
                          "reason": "用户测试需求", "scope": cid},
        "D5_关键参数": {"kind": "literature", "ref": "paper:10.1234/mc"},
        "D7_验收标准": {"kind": "literature", "ref": STD}})
    # 需要 D1 的确认记录存在（setup_case 已写 brief）
    r = gate(cid, rec(cid, "G-PLAN", "PLAN"))
    ok = r["result"] == "GO"
    check("E1-paper", "论文依据达到门槛 → G-PLAN GO", ok, r["reasons"])
    ok_all &= ok

    # ---------- 4. G-BUILD：坏配置 → E4 ----------
    wd = os.path.join(DATA, "work", cid)
    os.makedirs(wd, exist_ok=True)
    with open(os.path.join(wd, "in.bad"), "w", encoding="utf-8") as f:
        f.write("fix 1 all npt temp 300 300 0.1 iso 0 0 1.0\n"
                "fix 2 all deform 1 x erate 1e-4 units box\n")
    r = gate(cid, rec(cid, "G-BUILD", "BUILD", input_files=["in.bad"]))
    ok = r["result"] == "BLOCK" and any(x.startswith("E4:violation")
                                       for x in r["reasons"])
    check("E4-bad-config", "恒压+变形配置 → E4 拒", ok, r["reasons"])
    ok_all &= ok

    # ---------- 5. G-BUILD：干净配置 → GO ----------
    with open(os.path.join(wd, "in.good"), "w", encoding="utf-8") as f:
        f.write("fix 1 all nvt temp 300 300 0.1\n")
    r = gate(cid, rec(cid, "G-BUILD", "BUILD", input_files=["in.good"]))
    ok = r["result"] == "GO"
    check("E4-good", "合规配置 → G-BUILD GO", ok, r["reasons"])
    ok_all &= ok

    # ---------- 6. G-CONFIG ----------
    r = gate(cid, rec(cid, "G-CONFIG", "CONFIG", input_files=["in.good"]))
    ok = r["result"] == "GO"
    check("E4-config", "配置段复核 → GO", ok, r["reasons"])
    ok_all &= ok

    # ---------- 7. G-EXEC：无输入 → E2 ----------
    r = gate(cid, rec(cid, "G-EXEC", "EXEC"))
    ok = r["result"] == "BLOCK" and any(x.startswith("E2:input-untraceable")
                                       for x in r["reasons"])
    check("E2-no-input", "执行无输入文件 → E2 拒", ok, r["reasons"])
    ok_all &= ok

    # ---------- 8. G-EXEC：有输入 → GO ----------
    r = gate(cid, rec(cid, "G-EXEC", "EXEC", input_files=["in.good"]))
    ok = r["result"] == "GO"
    check("E2-exec", "链路完整 → G-EXEC GO", ok, r["reasons"])
    ok_all &= ok

    # ---------- 9. 真实计算 + factcheck ----------
    out = os.path.join(RUNS, RUN_ID, "mc.json")
    subprocess.run([sys.executable, os.path.join(_HERE, "compute_run.py"),
                    "200000", "42", out], check=True, capture_output=True)
    res = json.load(open(out, encoding="utf-8"))
    logp = os.path.join(wd, "run.log")
    with open(logp, "w", encoding="utf-8") as f:
        f.write("EVLIVE_PI %.6f\n" % res["pi_hat"])
    fc, fc_ref = content_check.check(cid, "pi_hat", round(res["pi_hat"], 4),
                                     {"path": logp, "marker": "EVLIVE_PI"},
                                     tolerance=1e-4, data_root=DATA)
    ok = fc["verdict"] == "PASS"
    check("E2-factcheck", "报告值从原始日志重算一致", ok, fc["verdict"])
    ok_all &= ok

    # ---------- 10. G-ANALYZE：错值 → E2 ----------
    add_results_assessment(cid)
    bad_fc, bad_fc_ref = content_check.check(cid, "pi_hat", round(res["pi_hat"] + 0.01, 4),
                                    {"path": logp, "marker": "EVLIVE_PI"},
                                    tolerance=1e-4, data_root=DATA)
    r = gate(cid, rec(cid, "G-ANALYZE", "ANALYZE", acceptance_checks=[
        {"item": "|pi_hat-3.14159|<=0.01", "claimed": res["pi_hat"] + 0.01,
         "passed": True, "factcheck": "factcheck:" + bad_fc_ref}]))
    ok = r["result"] == "BLOCK" and any(x.startswith("E2:claim-failed")
                                       for x in r["reasons"])
    check("E2-wrong-number", "数值主张与重算不符 → E2 拒", ok, r["reasons"])
    ok_all &= ok

    # ---------- 11. G-ANALYZE：真值 → GO（E5 覆盖） ----------
    r = gate(cid, rec(cid, "G-ANALYZE", "ANALYZE", acceptance_checks=[
        {"item": "|pi_hat-3.14159|<=0.01", "claimed": round(res["pi_hat"], 4),
         "passed": abs(res["pi_hat"] - 3.14159265) <= 0.01,
         "factcheck": "factcheck:" + fc_ref}]))
    ok = r["result"] == "GO"
    check("E5-covered", "结论覆盖验收项 → G-ANALYZE GO", ok, r["reasons"])
    ok_all &= ok

    # ---------- 12. 人工覆盖：缺理由 → 冲突包 ----------
    cid = "E2E-03"
    setup_case(cid, basis={
        "D1_对象与范围": {"kind": "user", "ref": "user:%s-brief" % cid,
                          "reason": "用户测试需求", "scope": cid},
        "D5_关键参数": {"kind": "user", "ref": "user:%s-dt" % cid,
                        "value": 25, "scope": cid},
        "D7_验收标准": {"kind": "literature", "ref": STD}})
    # 人工输入与文献值冲突
    doc = stage_state.read_decisions(cid, DATA)
    doc["D5_关键参数"]["evidence"] = [{"ref": "paper:10.1234/mc", "value": 2.0}]
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "decisions.json"), doc)
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations",
                       "%s-dt.json" % cid),
          {"id": "%s-dt" % cid, "scope": cid, "value": 25,
           "ts": "2026-10-19T00:00:00Z"})
    r = gate(cid, rec(cid, "G-PLAN", "PLAN"))
    pack = (r.get("conflicts") or [{}])[0]
    ok = r["result"] == "BLOCK" and bool(pack.get("fingerprint")) and \
        any(x.startswith("E1:override-reason-missing") for x in r["reasons"])
    check("E1-override-ask", "人工覆盖缺理由 → 冲突包(供追问)", ok,
          {"fp": pack.get("fingerprint"), "conflicts": pack.get("conflicts")})
    ok_all &= ok

    # ---------- 13. 答复后放行 ----------
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations",
                       "%s-dt-answer.json" % cid),
          {"id": "%s-dt-answer" % cid, "slot": "D5_关键参数",
           "fingerprint": pack.get("fingerprint"), "scope": cid,
           "question": "文献用 2fs，你要求 25fs，为什么仍采用？",
           "answer": "用户答复：该案例只需短程结构，接受风险", "resolved": True,
           "ts": "2026-10-19T00:00:00Z"})
    doc = stage_state.read_decisions(cid, DATA)
    doc["D5_关键参数"]["basis"]["reason"] = "用户答复：短程结构，接受风险"
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "decisions.json"), doc)
    r = gate(cid, rec(cid, "G-PLAN", "PLAN"))
    ok = r["result"] == "GO"
    check("E1-override-answer", "答复针对性理由后放行(记录异议)", ok, r["reasons"])
    ok_all &= ok

    # ---------- 14. 二次确认 → 最高权限 ----------
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations",
                       "%s-dt-confirm2.json" % cid),
          {"id": "%s-dt-confirm2" % cid, "scope": cid, "confirms": "%s-dt" % cid,
           "ts": "2026-10-19T00:00:00Z"})
    r = gate(cid, rec(cid, "G-PLAN", "PLAN"))
    trace = " ".join(str(x) for x in
                     r.get("rule_trace", {}).get("E1", {}).get("D5_关键参数") or [])
    ok = r["result"] == "GO" and "二次确认" in trace
    check("E1-second-confirm", "二次确认识别为最高权限人工经验", ok, trace)
    ok_all &= ok

    # ---------- 15. fail-closed ----------
    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")
    r = stage_gate.validate_decision_record(Boom(gate="G-PLAN"), {})
    ok = r["result"] == "BLOCK" and r["reasons"][0].startswith("E0:internal-error")
    check("E0-fail-closed", "内部错误 → BLOCK", ok, r["reasons"])
    ok_all &= ok

    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    print("\n== %d/%d 通过 ==" % (passed, len(RESULTS)))
    wjson(os.path.join(RUNS, RUN_ID, "e2e_report.json"),
          {"run_at": RUN_ID, "checks": [{"id": a, "desc": b, "ok": c, "detail": d}
                                        for a, b, c, d in RESULTS],
           "passed": passed, "total": len(RESULTS)})
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
