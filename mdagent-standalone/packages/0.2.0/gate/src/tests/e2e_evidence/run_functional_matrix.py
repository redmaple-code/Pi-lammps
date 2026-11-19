#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""证据系统功能矩阵 —— 不是"代码能跑"，而是"设计的功能真的生效"。

覆盖：
  A. E1 设计有据：证据分级/门槛/适用性/人工覆盖追问/二次确认
  B. E2 计算有据：链路/数值重算/新鲜性
  C. E3 物理合理：评估缺位/critical/清零
  D. E4 方法学合规：两条真实约束 + 知识注入/撤销
  E. E5 结论完整：验收覆盖/回退
  F. 闸绑定：每个闸只跑本段规则
  G. 高危操作：高风险弱证据、大规模无先行、危险步长
  H. 知识库驱动：10 个方法论维度逐条正反例；锚注入/撤销
  I. 真实产物：用实弹 LIT-01 的日志重算，验证 E2 作用于真实数据

输出：逐项 PASS/FAIL + 规则覆盖/知识条目覆盖表；退出码非零即失败。
"""
import json
import os
import shutil
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
import evidence_grade  # noqa: E402
import assessment   # noqa: E402
import deep_review  # noqa: E402
import content_check  # noqa: E402
import surface_fixture  # noqa: E402

STD = "std:Allen-Tildesley-1989-Computer-Simulation-of-Liquids"
PAPER = "paper:10.1103/PhysRevB.33.7983"
ROWS = []
DATA = None


def wjson(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def check(group, name, ok, detail=""):
    ROWS.append({"group": group, "name": name, "ok": bool(ok),
                 "detail": str(detail)[:220]})
    print("%s [%-22s] %-46s %s" % ("✅" if ok else "❌", group, name,
                                   ("| " + str(detail)[:150]) if detail else ""))


def new_case(cid, risk="medium", scale_gate="none"):
    stage_state.init_task(cid, risk_level=risk, scale_gate=scale_gate, data_root=DATA)
    d = os.path.join(stage_state.task_dir(cid, DATA), "confirmations")
    wjson(os.path.join(d, "%s-brief.json" % cid),
          {"id": "%s-brief" % cid, "scope": cid, "value": "功能矩阵案例",
           "reason": "用户测试需求", "ts": "2026-10-21T00:00:00Z"})


def lock(cid, slots, basis=None):
    stage_state.lock_decisions(cid, slots, DATA, basis=basis or {})


def add_assessment(cid, phase, crit=None):
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "assessments",
                       "%s-%s.json" % (cid, phase)),
          {"schema_version": 1, "case_id": cid, "phase": phase,
           "findings": [], "critical_open": crit or [],
           "overall": "sound", "proceed": "recommended"})


def gate(cid, g, **kw):
    if not kw.pop("skip_surface", False):
        phase = {"G-PLAN": "design", "G-BUILD": "build", "G-CONFIG": "build",
                 "G-EXEC": "exec", "G-ANALYZE": "analyze"}.get(g)
        if phase:
            surface_fixture.fill_phase(DATA, cid, phase)
    r = {"schema_version": 1, "case_id": cid,
         "stage": {"G-PLAN": "PLAN", "G-BUILD": "BUILD", "G-CONFIG": "CONFIG",
                   "G-EXEC": "EXEC", "G-ANALYZE": "ANALYZE"}.get(g, "PLAN"),
         "gate": g, "decision": "PASS", "summary": "matrix",
         "evidence_refs": [], "acceptance_checks": [], "input_files": []}
    r.update(kw)
    return stage_gate.gate_and_log(cid, r, data_root=DATA, facts=kw.get("facts") or {})


def write_input(cid, name, text):
    wd = os.path.join(DATA, "work", cid)
    os.makedirs(wd, exist_ok=True)
    fp = os.path.join(wd, name)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)
    return name


def add_factcheck(cid, name, doc):
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "factchecks", name), doc)
    return "factcheck:factchecks/" + name


def design_ok(cid, risk="medium"):
    """标准设计：D1 用户定义 + 三项标准依据；供各规则测试复用。"""
    lock(cid, {"D1_对象与范围": "fcc Cu 弹性常数 C11 应变能法",
               "D5_关键参数": "EAM Cu_u3",
               "D7_验收标准": ["C11∈[150,178]GPa"]},
         basis={"D1_对象与范围": {"kind": "user", "ref": "user:%s-brief" % cid,
                                  "reason": "用户测试需求", "scope": cid},
                "D5_关键参数": {"kind": "literature", "ref": PAPER},
                "D7_验收标准": {"kind": "literature", "ref": STD}})


# ============================================================ A. E1
def test_e1():
    cid = "M-E1-01"
    new_case(cid)
    r = gate(cid, "G-PLAN")
    check("E1", "无设计决策 → 拒", r["result"] == "BLOCK" and
          any(x.startswith("E1:no-design-decisions") for x in r["reasons"]), r["reasons"])

    cid = "M-E1-02"
    new_case(cid)
    lock(cid, {"D1_对象与范围": "拍脑袋方案"},
         basis={"D1_对象与范围": {"kind": "physics_estimate", "ref": "凭感觉"}})
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E1", "只有 F 级估算 → 拒", r["result"] == "BLOCK" and
          any("evidence-insufficient" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E1-03"
    new_case(cid)
    lock(cid, {"D1_对象与范围": "有案例依据"},
         basis={"D1_对象与范围": {"kind": "prior_case", "ref": "case:OLD-1"}})
    wjson(os.path.join(DATA, "memory", "cases", "OLD-1.json"),
          {"case_id": "OLD-1", "family": "", "engine": "", "result_summary": "旧案例"})
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E1", "D 级过往案例（medium）→ 过",
          r["result"] == "GO" and r["rule_trace"].get("E1"), r["reasons"])

    cid = "M-E1-04"
    new_case(cid, risk="high")
    lock(cid, {"D1_对象与范围": "高风险只有案例"},
         basis={"D1_对象与范围": {"kind": "prior_case", "ref": "case:OLD-1"}})
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E1", "高风险仅 D 级 → 拒", r["result"] == "BLOCK" and
          any("evidence-insufficient" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E1-05"
    new_case(cid, risk="high")
    lock(cid, {"D1_对象与范围": "高风险有论文"},
         basis={"D1_对象与范围": {"kind": "literature", "ref": PAPER}})
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E1", "高风险论文 B → 过",
          r["result"] == "GO" and r["rule_trace"].get("E1"), r["reasons"])

    cid = "M-E1-06"
    new_case(cid, risk="high")
    lock(cid, {"D1_对象与范围": "人工拍板"},
         basis={"D1_对象与范围": {"kind": "user", "ref": "user:%s-brief" % cid,
                                  "reason": "用户明确要求", "scope": cid}})
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E1", "高风险纯人工（有理由）→ 过", r["result"] == "GO", r["reasons"])

    # 适用性：论文 family 不匹配 → 不计入
    cid = "M-E1-07"
    new_case(cid)
    stage_state.lock_decisions(cid, {"D1_对象与范围": "液氩 RDF 液相"}, DATA,
                               basis={"D1_对象与范围": {"kind": "literature",
                                                        "ref": PAPER}})
    # 给槽位挂一条 family 不匹配的论文证据，并去掉 basis 的 B（改用 F 触发门槛判定）
    doc = stage_state.read_decisions(cid, DATA)
    doc["D1_对象与范围"]["basis"] = {"kind": "physics_estimate", "ref": "估算"}
    doc["D1_对象与范围"]["evidence"] = [
        {"ref": PAPER, "meta": {"family": "elastic-constants"}}]
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "decisions.json"), doc)
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E1", "论文与体系不适用 → 拒", r["result"] == "BLOCK" and
          any("evidence-insufficient" in x for x in r["reasons"]), r["reasons"])

    # 人工覆盖：缺理由 → 冲突包
    cid = "M-E1-08"
    new_case(cid)
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations", "%s-dt.json" % cid),
          {"id": "%s-dt" % cid, "scope": cid, "value": 25, "ts": "2026-10-21T00:00:00Z"})
    stage_state.lock_decisions(cid, {"D5_关键参数": "dt=25fs"}, DATA,
                               basis={"D5_关键参数": {"kind": "user",
                                                      "ref": "user:%s-dt" % cid,
                                                      "value": 25, "scope": cid}})
    doc = stage_state.read_decisions(cid, DATA)
    doc["D5_关键参数"]["evidence"] = [{"ref": PAPER, "value": 2.0}]
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "decisions.json"), doc)
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    pack = (r.get("conflicts") or [{}])[0]
    check("E1", "人工覆盖缺理由 → 冲突包", r["result"] == "BLOCK" and
          any("override-reason-missing" in x for x in r["reasons"]) and
          bool(pack.get("fingerprint")), {"reasons": r["reasons"],
                                          "pack": pack})

    # 同一冲突已问未决 → 升级，不重复问
    fp = pack.get("fingerprint")
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations",
                       "%s-ask.json" % cid),
          {"id": "%s-ask" % cid, "slot": "D5_关键参数", "fingerprint": fp,
           "question": "文献 2fs vs 你要求 25fs，为什么？", "ts": "2026-10-21T00:00:00Z"})
    r = gate(cid, "G-PLAN")
    check("E1", "同一冲突已问未决 → 升级不重复", r["result"] == "BLOCK" and
          any("override-unresolved" in x for x in r["reasons"]), r["reasons"])

    # 针对性答复 → 放行
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations",
                       "%s-answer.json" % cid),
          {"id": "%s-answer" % cid, "slot": "D5_关键参数", "fingerprint": fp,
           "question": "文献 2fs vs 你要求 25fs，为什么？",
           "answer": "用户答复：短程结构，接受风险", "resolved": True,
           "ts": "2026-10-21T00:00:00Z"})
    doc = stage_state.read_decisions(cid, DATA)
    doc["D5_关键参数"]["basis"]["reason"] = "用户答复：短程结构，接受风险"
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "decisions.json"), doc)
    r = gate(cid, "G-PLAN")
    check("E1", "答复针对性理由 → 放行", r["result"] == "GO", r["reasons"])

    # 二次确认 → 最高权限
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "confirmations",
                       "%s-confirm2.json" % cid),
          {"id": "%s-confirm2" % cid, "scope": cid, "confirms": "%s-dt" % cid,
           "ts": "2026-10-21T00:00:00Z"})
    r = gate(cid, "G-PLAN")
    trace = " ".join(str(x) for x in
                     (r.get("rule_trace", {}).get("E1", {}).get("D5_关键参数") or []))
    check("E1", "二次确认 → 最高权限人工经验", r["result"] == "GO" and "二次确认" in trace, trace)


# ============================================================ B. E2
def test_e2():
    cid = "M-E2-01"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    gate(cid, "G-PLAN")
    r = gate(cid, "G-EXEC")
    check("E2", "执行无输入文件 → 拒", r["result"] == "BLOCK" and
          any("input-untraceable" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E2-02"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    gate(cid, "G-PLAN")
    write_input(cid, "in.ok", "fix 1 all nvt temp 300 300 0.1\n")
    r = gate(cid, "G-EXEC", input_files=["in.ok"])
    check("E2", "执行链路完整 → 过",
          r["result"] == "GO" and r["rule_trace"].get("E2"), r["reasons"])

    cid = "M-E2-03"
    new_case(cid)
    add_assessment(cid, "results")
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0=3.615", "claimed": 3.615, "passed": True}])
    check("E2", "数值主张无对账 → 拒", r["result"] == "BLOCK" and
          any("claim-unevidenced" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E2-04"
    new_case(cid)
    add_assessment(cid, "results")
    fc = add_factcheck(cid, "fc-bad.json", {
        "field": "a0", "claimed": 3.7, "measured": 3.615,
        "verdict": "FAIL", "source": {}, "ts": "2026-10-21T00:00:00Z"})
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0=3.7", "claimed": 3.7, "passed": True, "factcheck": fc}])
    check("E2", "对账不符 → 拒", r["result"] == "BLOCK" and
          any("claim-failed" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E2-05"
    new_case(cid)
    add_assessment(cid, "results")
    fc = add_factcheck(cid, "fc-ok.json", {
        "field": "a0", "claimed": 3.615, "measured": 3.615,
        "verdict": "PASS", "source": {}, "ts": "2026-10-21T00:00:00Z"})
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0=3.615", "claimed": 3.615, "passed": True, "factcheck": fc}])
    check("E2", "对账一致 → 过", r["result"] == "GO", r["reasons"])

    # 新鲜性：源文件比记录新 → 旧账不撑新主张
    cid = "M-E2-06"
    new_case(cid)
    add_assessment(cid, "results")
    src = os.path.join(DATA, "work", cid, "old.log")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "w", encoding="utf-8") as f:
        f.write("EVLIVE_A0 3.615\n")
    fc = add_factcheck(cid, "fc-stale.json", {
        "field": "a0", "claimed": 3.615, "measured": 3.615, "verdict": "PASS",
        "source": {"path": src}, "ts": "2000-01-01T00:00:00Z"})
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0=3.615", "claimed": 3.615, "passed": True, "factcheck": fc}])
    check("E2", "旧账充新主张 → 拒", r["result"] == "BLOCK" and
          any("stale-evidence" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E2-07"
    new_case(cid)
    add_assessment(cid, "results")
    r = gate(cid, "G-ANALYZE")
    check("E2", "结题无数值主张 → 拒", r["result"] == "BLOCK" and
          any("no-acceptance-claims" in x for x in r["reasons"]), r["reasons"])


# ============================================================ C. E3
def test_e3():
    cid = "M-E3-01"
    new_case(cid)
    design_ok(cid)
    r = gate(cid, "G-PLAN")
    check("E3", "计划段缺评估 → 拒", r["result"] == "BLOCK" and
          any("assessment-missing" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E3-02"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan", crit=["mdev-general-dt"])
    r = gate(cid, "G-PLAN")
    check("E3", "计划段 critical → 拒", r["result"] == "BLOCK" and
          any("E3:critical" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E3-03"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("E3", "计划段 critical 清零 → 过",
          r["result"] == "GO" and r["rule_trace"].get("E3"), r["reasons"])

    cid = "M-E3-04"
    new_case(cid)
    add_assessment(cid, "results", crit=["RA2"])
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "x", "passed": True}])
    check("E3", "结果段 critical → 拒", r["result"] == "BLOCK" and
          any("E3:critical" in x for x in r["reasons"]), r["reasons"])


# ============================================================ D. E4
def test_e4():
    cid = "M-E4-01"
    new_case(cid)
    r = gate(cid, "G-BUILD")
    check("E4", "未声明输入文件 → 拒", r["result"] == "BLOCK" and
          any("no-input-declared" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E4-02"
    new_case(cid)
    f = write_input(cid, "in.bad", "fix 1 all npt temp 300 300 0.1 iso 0 0 1\n"
                                  "fix 2 all deform 1 x erate 1e-4 units box\n")
    r = gate(cid, "G-BUILD", input_files=[f])
    check("E4", "恒压+变形 → 拒(methodo-001)", r["result"] == "BLOCK" and
          any("methodo-001" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E4-03"
    new_case(cid)
    f = write_input(cid, "in.ok", "fix 1 all npt temp 300 300 0.1 y 0 0 1 z 0 0 1\n"
                                 "fix 2 all deform 1 x erate 1e-4 units box\n")
    r = gate(cid, "G-BUILD", input_files=[f])
    check("E4", "仅非变形方向控压 → 过",
          r["result"] == "GO" and r["rule_trace"].get("E4"), r["reasons"])

    cid = "M-E4-04"
    new_case(cid)
    f = write_input(cid, "in.oneway", "fix 1 all nve\nrun 1000\n")
    lock(cid, {"D1_对象与范围": "往复摩擦磨损实验"})
    r = gate(cid, "G-BUILD", input_files=[f])
    check("E4", "往复做成单向 → 拒(methodo-002)", r["result"] == "BLOCK" and
          any("methodo-002" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E4-05"
    new_case(cid)
    f = write_input(cid, "in.osc", "fix drive mobile move wiggle 1 0 0 2 units box\n"
                                  "fix 1 all nve\n")
    lock(cid, {"D1_对象与范围": "往复摩擦磨损实验"})
    r = gate(cid, "G-BUILD", input_files=[f])
    check("E4", "往复+wiggle → 过", r["result"] == "GO", r["reasons"])

    cid = "M-E4-06"
    new_case(cid)
    r = gate(cid, "G-BUILD", input_files=["no-such-file.lmp"])
    check("E4", "输入文件不可读 → 拒(fail-closed)", r["result"] == "BLOCK" and
          any("cannot-verify" in x for x in r["reasons"]), r["reasons"])

    # ---- 知识注入：新增约束立即可用；撤销后立即失效 ----
    cid = "M-E4-07"
    new_case(cid)
    f = write_input(cid, "in.nve", "fix 1 all nve\nrun 1000\n")
    rt = os.path.join(DATA, "memory", "review_experience")
    wjson(os.path.join(rt, "inject.json"), {
        "pack": "inject", "schema_version": 1, "entries": [{
            "id": "methodo-inject-001", "kind": "methodology",
            "statement": "测试注入：NVE 必须配恒温器（演示知识驱动）",
            "constraints": {"violate_if": [{"all_of": [
                {"cmd": "fix", "args_regex": "\\bnve\\b"}],
                "note": "测试注入约束命中"}]},
            "severity_hint": "hard", "status": "active",
            "refs": ["user:test"]}]})
    r = gate(cid, "G-BUILD", input_files=[f])
    injected = r["result"] == "BLOCK" and any(
        "methodo-inject-001" in x for x in r["reasons"])
    check("E4", "注入新知识约束 → 立即拦截", injected, r["reasons"])

    wjson(os.path.join(rt, "inject.json"), {
        "pack": "inject", "schema_version": 1, "entries": [{
            "id": "methodo-inject-001", "kind": "methodology",
            "statement": "测试注入：NVE 必须配恒温器（已撤销）",
            "constraints": {"violate_if": [{"all_of": [
                {"cmd": "fix", "args_regex": "\\bnve\\b"}],
                "note": "测试注入约束命中"}]},
            "severity_hint": "hard", "status": "revoked",
            "refs": ["user:test"]}]})
    r = gate(cid, "G-BUILD", input_files=[f])
    check("E4", "撤销该知识 → 立即放行", r["result"] == "GO", r["reasons"])
    os.remove(os.path.join(rt, "inject.json"))


# ============================================================ E. E5
def test_e5():
    cid = "M-E5-01"
    new_case(cid)
    lock(cid, {"D7_验收标准": ["a0=3.615", "Ec=-3.54"]})
    add_assessment(cid, "results")
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0=3.615", "passed": True}])
    check("E5", "验收项缺覆盖 → 拒", r["result"] == "BLOCK" and
          any("acceptance-not-covered" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E5-02"
    new_case(cid)
    add_assessment(cid, "results")
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0", "passed": False}])
    check("E5", "不达标无回退 → 拒", r["result"] == "BLOCK" and
          any("no-rollback-on-fail" in x for x in r["reasons"]), r["reasons"])

    cid = "M-E5-03"
    new_case(cid)
    add_assessment(cid, "results")
    r = gate(cid, "G-ANALYZE", acceptance_checks=[
        {"item": "a0", "passed": False}], rollback_target="PLAN")
    check("E5", "不达标有回退 → 过",
          r["result"] == "GO" and r["rule_trace"].get("E5"), r["reasons"])


# ============================================================ F. 闸绑定
def test_binding():
    cid = "M-B-01"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    gate(cid, "G-PLAN")
    f = write_input(cid, "in.ok", "fix 1 all nvt temp 300 300 0.1\n")
    r = gate(cid, "G-BUILD", input_files=[f])
    check("BIND", "G-BUILD 不跑 E3（无评估也过）", r["result"] == "GO", r["reasons"])

    cid = "M-B-02"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("BIND", "G-PLAN 不跑 E4（无输入也过）", r["result"] == "GO", r["reasons"])

    cid = "M-B-03"
    new_case(cid)
    r = gate(cid, "G-CUSTOM")
    check("BIND", "未绑定闸不跑证据规则", r["result"] == "GO" and
          "未绑定证据规则" in r["rule_trace"].get("_gate", ""), r["rule_trace"])


# ============================================================ G. 高危
def test_high_risk():
    # 危险步长：E3 critical（用真实评估引擎，不是手工记录）
    cid = "M-HR-01"
    new_case(cid)
    stage_state.lock_decisions(cid, {
        "D1_对象与范围": "液氩 90K 液相",
        "D5_关键参数": "dt=100fs"}, DATA,
        basis={"D1_对象与范围": {"kind": "user", "ref": "user:%s-brief" % cid,
                                 "reason": "测试", "scope": cid},
               "D5_关键参数": {"kind": "physics_estimate", "ref": "激进估算"}})
    pa, _ = assessment.assess_plan(cid, DATA)
    crit = pa["critical_open"]
    r = gate(cid, "G-PLAN")
    check("HIGH", "dt=100fs → 评估 critical + E3 拦截",
          "PA1" in crit and r["result"] == "BLOCK" and
          any("E3:critical" in x for x in r["reasons"]), r["reasons"])

    # 大规模无先行：S2 执行策略（直接测 sim_exec 前置）
    cid = "M-HR-02"
    new_case(cid, scale_gate="large_pending")
    st = stage_state.read_state(cid, DATA)
    st["current_stage"] = "EXEC"
    stage_state._atomic_write_json(stage_state.state_path(cid, DATA), st)
    import md_mcp
    blocked = md_mcp._exec_gate_check({"case_id": cid, "run_kind": "production"})
    ok1 = isinstance(blocked, dict) and any(
        str(x).startswith("S2:scale-gate-unmet") for x in blocked.get("reasons") or [])
    check("HIGH", "大规模无小规模成功 → S2 拦截", ok1, blocked)

    # 记一条小规模成功 → 放行
    fp = os.path.join(DATA, "memory", "LEDGER.jsonl")
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps({"lid": "M-HR-02-probe", "case_id": cid,
                            "result": "success", "ts": "2026-10-21T00:00:01Z"}) + "\n")
    ok2 = md_mcp._exec_gate_check({"case_id": cid, "run_kind": "production"})
    check("HIGH", "有小规模成功 → 放行", ok2 is None, ok2)

    # 高风险决策只有经验证据 → E1 拦截（上面 M-E1-04 已覆盖，这里再独立一条）
    cid = "M-HR-03"
    new_case(cid, risk="high")
    stage_state.lock_decisions(cid, {"D1_对象与范围": "高风险仅经验"}, DATA,
                               basis={"D1_对象与范围": {"kind": "lesson",
                                                        "ref": "atom:exp-1"}})
    wjson(os.path.join(DATA, "memory", "atoms.jsonl"), {}) if False else None
    with open(os.path.join(DATA, "memory", "atoms.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "exp-1", "fact": "旧经验"}) + "\n")
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN")
    check("HIGH", "高风险仅 E 级经验 → 拒", r["result"] == "BLOCK" and
          any("evidence-insufficient" in x for x in r["reasons"]), r["reasons"])


# ============================================================ SURF. 核查面
def test_surface_matrix():
    import surface
    # 空核查面 → E6 拦截 + 问题包
    cid = "M-SURF-01"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    r = gate(cid, "G-PLAN", skip_surface=True)
    q = r.get("questions") or []
    check("SURF", "空核查面 → E6 拦截并产出问题包",
          r["result"] == "BLOCK" and
          any(x.startswith("E6:surface-missing") for x in r["reasons"]) and
          len(q) >= 10, {"reasons": r["reasons"][:3], "questions": len(q)})

    # 填满后过闸
    surface_fixture.fill_phase(DATA, cid, "design")
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF", "核查面完整 → 过闸",
          r["result"] == "GO" and "核查面 design" in str(r["rule_trace"].get("E6", "")),
          r["rule_trace"].get("E6"))

    # N/A 无理由
    cid = "M-SURF-02"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    na_item = [i for i in surface.load_items(DATA).values()
               if i["phase"] == "design" and i["na_allowed"]][0]["id"]
    surface.record_coverage(cid, na_item, "na", reason="", data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF", "N/A 缺理由 → 拒",
          any("surface-na-reason" in x for x in r["reasons"]), r["reasons"])

    # 不允许 N/A 的项被 N/A
    cid = "M-SURF-03"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    strict_item = [i for i in surface.load_items(DATA).values()
                   if i["phase"] == "design" and not i["na_allowed"]][0]["id"]
    surface.record_coverage(cid, strict_item, "na", reason="不想查", data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF", "不允许 N/A 的项 → 拒",
          any("surface-na-not-allowed" in x for x in r["reasons"]), r["reasons"])

    # 证据等级不足
    cid = "M-SURF-04"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    strict_item = [i for i in surface.load_items(DATA).values()
                   if i["phase"] == "design" and not i["na_allowed"]][0]["id"]
    surface.record_coverage(cid, strict_item, "evidenced",
                            evidence=["凭感觉"], data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF", "证据等级不足 → 拒",
          any("surface-evidence-insufficient" in x for x in r["reasons"]), r["reasons"])

    # 待取证 → 点名问题
    cid = "M-SURF-05"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    surface.record_coverage(cid, strict_item, "open", data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF", "待取证项 → 点名提问",
          r["result"] == "BLOCK" and any(q.get("item_id") == strict_item
                                         for q in r.get("questions") or []),
          [q.get("item_id") for q in r.get("questions") or []])


# ============================================================ SURF2. P1 深化
def test_surface_p1():
    import surface
    import md_mcp
    import memory

    # ---- E6 负例：build/exec/analyze 相位缺项 ----
    cid = "M-S2-01"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    write_input(cid, "in.ok", "fix 1 all nvt temp 300 300 0.1" + chr(10))
    r = gate(cid, "G-BUILD", input_files=["in.ok"], skip_surface=True)
    check("SURF2", "G-BUILD 缺 build 核查面 → 拒",
          any(x.startswith("E6:surface-missing") for x in r["reasons"]),
          r["reasons"][:3])
    r = gate(cid, "G-EXEC", input_files=["in.ok"], skip_surface=True)
    check("SURF2", "G-EXEC 缺 exec 核查面 → 拒",
          any(x.startswith("E6:surface-missing") for x in r["reasons"]),
          r["reasons"][:3])
    add_assessment(cid, "results")
    r = gate(cid, "G-ANALYZE", acceptance_checks=[{"item": "x", "passed": True}],
             skip_surface=True)
    check("SURF2", "G-ANALYZE 缺 analyze 核查面 → 拒",
          any(x.startswith("E6:surface-missing") for x in r["reasons"]),
          r["reasons"][:3])

    # ---- 高风险 + C 级证据 → E6 拒（门槛 B）----
    cid = "M-S2-02"
    new_case(cid, risk="high")
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    surface.record_coverage(cid, "sys-potential", "evidenced",
                            evidence=["$lmp_available"], answer="某势函数",
                            data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True, facts={"lmp_available": 1})
    check("SURF2", "高风险核查面只有 C 级证据 → 拒",
          any("surface-evidence-insufficient:sys-potential" in x
              for x in r["reasons"]), r["reasons"][:3])

    # ---- evidenced 无答案 / 数值项答案无数字 ----
    cid = "M-S2-03"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    surface.record_coverage(cid, "sys-structure", "evidenced",
                            evidence=["paper:10.1234/x"], answer="",
                            data_root=DATA)
    surface.record_coverage(cid, "bnd-atoms", "evidenced",
                            evidence=["paper:10.1234/x"], answer="很多原子",
                            data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF2", "只挂引用没回答 → 拒",
          any("surface-answer-missing:sys-structure" in x for x in r["reasons"]),
          r["reasons"][:4])
    check("SURF2", "数值项答案无数字 → 拒",
          any("surface-answer-unquantified:bnd-atoms" in x for x in r["reasons"]),
          r["reasons"][:4])

    # ---- 静态案例：步长 N/A 须给正当理由 ----
    cid = "M-S2-04"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    surface_fixture.fill_phase(DATA, cid, "design")
    surface.record_coverage(cid, "int-timestep", "na", reason="不想填",
                            data_root=DATA)
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF2", "N/A 理由不正当 → 拒",
          any("surface-na-reason-unjustified:int-timestep" in x
              for x in r["reasons"]), r["reasons"][:3])

    # ---- 高风险 + 未知族（generic）→ E3 拒 ----
    cid = "M-S2-05"
    new_case(cid, risk="high")
    design_ok(cid)
    wjson(os.path.join(stage_state.task_dir(cid, DATA), "assessments",
                       "plan-g.json"),
          {"case_id": cid, "phase": "plan", "family": "generic",
           "findings": [], "critical_open": [], "overall": "sound"})
    surface_fixture.fill_phase(DATA, cid, "design")
    r = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF2", "高风险未知族（generic）→ E3 拒",
          any("family-uncovered-high-risk" in x for x in r["reasons"]),
          r["reasons"][:3])

    # ---- 提问 → 回答 → 关闭 → 重审 ----
    cid = "M-S2-06"
    new_case(cid)
    design_ok(cid)
    add_assessment(cid, "plan")
    r0 = gate(cid, "G-PLAN", skip_surface=True)
    q0 = {q["item_id"] for q in r0.get("questions") or []}
    check("SURF2", "提问循环：首轮点名缺项",
          "sys-composition" in q0, sorted(q0)[:4])
    surface.record_coverage(cid, "sys-composition", "evidenced",
                            evidence=["paper:10.1234/x"],
                            answer="fcc Cu", data_root=DATA)
    r1 = gate(cid, "G-PLAN", skip_surface=True)
    q1 = {q["item_id"] for q in r1.get("questions") or []}
    check("SURF2", "回答后该项关闭（其余仍点名）",
          "sys-composition" not in q1 and "sys-structure" in q1,
          sorted(q1)[:4])
    surface_fixture.fill_phase(DATA, cid, "design")
    r2 = gate(cid, "G-PLAN", skip_surface=True)
    check("SURF2", "补齐后过闸", r2["result"] == "GO", r2["reasons"])

    # ---- sim_surface 三动作（status/record/sediment）----
    cid = "M-S2-07"
    new_case(cid)
    out = md_mcp.sim_surface({"case_id": cid, "action": "status"})
    ok1 = isinstance(out.get("items"), list) and len(out["items"]) >= 25
    check("SURF2", "sim_surface status 列出全部条目", ok1,
          len(out.get("items") or []))
    out = md_mcp.sim_surface({"case_id": cid, "action": "record",
                              "item_id": "sys-potential", "status": "evidenced",
                              "evidence_refs": ["paper:10.1234/x"],
                              "answer": "EAM Cu_u3"})
    ok2 = (out.get("item", {}) or {}).get("answer") == "EAM Cu_u3"
    check("SURF2", "sim_surface record 写入答案", ok2, out.get("error", ""))
    out = md_mcp.sim_surface({"case_id": cid, "action": "record",
                              "item_id": "no-such-item", "status": "na"})
    check("SURF2", "sim_surface 拒绝未知条目",
          "unknown-item" in str(out.get("error", "")), out.get("error"))
    memory.EXP_DIR = os.path.join(DATA, "memory")
    memory.ATOMS = os.path.join(DATA, "memory", "atoms.jsonl")
    os.makedirs(memory.EXP_DIR, exist_ok=True)
    out = md_mcp.sim_surface({"case_id": cid, "action": "sediment",
                              "item_id": "sys-potential",
                              "summary": "EAM Cu_u3 适用于 fcc Cu（演示沉淀）",
                              "evidence_refs": ["paper:10.1234/x"]})
    aid = out.get("atom_id")
    ok3 = bool(aid) and any(json.loads(l).get("id") == aid
                            for l in open(memory.ATOMS, encoding="utf-8")
                            if l.strip())
    check("SURF2", "sim_surface sediment 落 atom", ok3, aid)

    # ---- 沉淀复用：atom 作为后续案例的 E 级依据 ----
    cid2 = "M-S2-08"
    new_case(cid2, risk="low")
    stage_state.lock_decisions(
        cid2, {"D1_对象与范围": "复用沉淀经验"}, DATA,
        basis={"D1_对象与范围": {"kind": "lesson", "ref": "atom:%s" % aid}})
    add_assessment(cid2, "plan")
    surface_fixture.fill_phase(DATA, cid2, "design")
    r = gate(cid2, "G-PLAN", skip_surface=True)
    tr = " ".join(str(x) for x in
                  (r.get("rule_trace", {}).get("E1", {})
                   .get("D1_对象与范围") or []))
    check("SURF2", "沉淀 atom 可被后续案例引用（E 级）",
          r["result"] == "GO" and ("atom:%s" % aid + "→E") in tr, tr)



# ============================================================ H. 知识库驱动
def _deep(cid_text, family_text, input_text, facts):
    cid = "M-KB-%s" % (abs(hash(cid_text + family_text)) % 100000)
    new_case(cid)
    stage_state.lock_decisions(cid, {"D1_对象与范围": family_text}, DATA,
                               basis={"D1_对象与范围": {"kind": "user",
                                                        "ref": "user:%s-brief" % cid,
                                                        "reason": "测试", "scope": cid}})
    rec, _ = deep_review.analyze(
        cid, "results", stage_state.read_decisions(cid, DATA), facts,
        data_root=DATA, input_text=input_text)
    return {f["id"]: f for f in rec["findings"]}


def test_knowledge_dimensions():
    cases = [
        ("mdev-ec-method", "fcc Cu 弹性常数计算",
         "fcc Cu 弹性常数 C11(应变能法)", "minimize\n", {}, "concern", "sound"),
        ("mdev-ec-strain-linear", "fcc Cu 弹性常数 C11 应变能法",
         "fcc Cu 弹性常数 C11 应变能法", "box/relax\nminimize\n",
         {"c11_small_amp": 167, "c11_large_amp": 200}, "critical", "sound"),
        ("mdev-ec-relax", "fcc Cu 弹性常数 C11 应变能法",
         "fcc Cu 弹性常数 C11 应变能法", "minimize\n", {}, "critical", "sound"),
        ("mdev-ec-size", "fcc Cu 弹性常数 C11 应变能法",
         "fcc Cu 弹性常数 C11 应变能法", "box/relax\nminimize\n",
         {"atom_count": 50}, "concern", "sound"),
        ("mdev-ec-temp-basis", "fcc Cu 弹性常数 C11 应变能法 0K oracle 对照 300K 实验",
         "fcc Cu 弹性常数 C11 应变能法", "box/relax\nminimize\n", {}, "sound", "concern"),
        ("mdev-liq-equil", "液氩 90K 液相 RDF",
         "液氩 90K 液相 RDF", "fix 1 all nvt temp 90 90\n", {}, "critical", "sound"),
        ("mdev-liq-sampling", "液氩 90K 液相 RDF",
         "液氩 90K 液相 RDF", "fix 1 all nvt temp 90 90\n", {}, "critical", "sound"),
        ("mdev-gs-relax", "LJ 氩基态晶格常数",
         "LJ 氩基态晶格常数", "minimize\n", {}, "critical", "sound"),
        ("mdev-gs-start-off", "LJ 氩基态晶格常数(起始 5.25 偏离目标)",
         "LJ 氩基态晶格常数", "box/relax\nminimize\n", {}, "sound", "concern"),
        ("mdev-general-dt", "液氩 90K 液相 dt=100fs",
         "液氩 90K 液相", "fix 1 all nvt temp 90 90\n", {}, "critical", "sound"),
    ]
    for dim, neg_text, pos_text, neg_input, neg_facts, neg_want, pos_want in cases:
        # 负例：应抓出来
        if dim == "mdev-general-dt":
            neg = _deep(dim + "-neg", neg_text, neg_input, {})
            pos = _deep(dim + "-pos", "液氩 90K 液相 dt=2fs", neg_input, {})
        elif dim == "mdev-ec-strain-linear":
            neg = _deep(dim + "-neg", neg_text, neg_input, neg_facts)
            pos = _deep(dim + "-pos", pos_text, "box/relax\nminimize\n",
                        {"c11_small_amp": 167.1, "c11_large_amp": 167.5})
        elif dim == "mdev-ec-size":
            neg = _deep(dim + "-neg", neg_text, neg_input, neg_facts)
            pos = _deep(dim + "-pos", pos_text, neg_input, {"atom_count": 108})
        else:
            neg = _deep(dim + "-neg", neg_text, neg_input, {})
            pos = _deep(dim + "-pos", pos_text,
                        pos_text if dim in ("mdev-ec-temp-basis",
                                            "mdev-gs-start-off") else
                        ("box/relax\nminimize\n" if dim.startswith("mdev-ec")
                         else ("fix 1 all nvt temp 90 90\nfix 2 all npt temp 90 90 iso 0 0 1\ncompute rdf 100\nfix ave/time 1 10 10 c_thermo_temp\n"
                               if dim.startswith("mdev-liq") else
                               "box/relax\nminimize\n")),
                        {})
        check("KB", "%s 负例被抓" % dim,
              neg.get(dim, {}).get("verdict") == neg_want,
              neg.get(dim, {}).get("evidence", ""))
        check("KB", "%s 正例放行" % dim,
              pos.get(dim, {}).get("verdict") == pos_want,
              pos.get(dim, {}).get("evidence", ""))


def test_anchor_injection():
    """锚注入：同一决策在更严的锚下从 sound 变 concern；撤销后恢复。"""
    cid = "M-KB-ANCHOR"
    new_case(cid)
    stage_state.lock_decisions(cid, {
        "D1_对象与范围": "液氩 90K 液相",
        "D5_关键参数": "dt=2fs"}, DATA,
        basis={"D1_对象与范围": {"kind": "user", "ref": "user:%s-brief" % cid,
                                 "reason": "测试", "scope": cid},
               "D5_关键参数": {"kind": "physics_estimate", "ref": "估算"}})
    pa, _ = assessment.assess_plan(cid, DATA)
    base = [f for f in pa["findings"] if f["id"] == "PA1"][0]["verdict"]
    rt = os.path.join(DATA, "memory", "review_experience")
    wjson(os.path.join(rt, "anchor_override.json"), {
        "pack": "anchor_override", "schema_version": 1, "entries": [{
            "id": "anchor-md-001", "kind": "physics-anchor",
            "quantity": "约化时间步长 dt*", "unit": "无量纲",
            "conditions": {"family": "lj-classical"},
            "reduction": {"t_unit_ps": 2.157},
            "range": {"recommended_max": 0.0002, "critical_max": 0.02},
            "rationale": "测试注入：更严的推荐上限",
            "status": "active"}]})
    pa2, _ = assessment.assess_plan(cid, DATA)
    strict = [f for f in pa2["findings"] if f["id"] == "PA1"][0]["verdict"]
    check("KB", "锚注入 → 同一决策判定变化",
          base == "sound" and strict == "concern", "%s → %s" % (base, strict))
    os.remove(os.path.join(rt, "anchor_override.json"))
    pa3, _ = assessment.assess_plan(cid, DATA)
    back = [f for f in pa3["findings"] if f["id"] == "PA1"][0]["verdict"]
    check("KB", "锚撤销 → 恢复原判定", back == "sound", back)


def test_real_artifact():
    """真实产物：用实弹 LIT-01 的日志独立重算 a0（不经工件快照）。"""
    live = os.path.join(_SRC, "experiments", "evidence_live", "live_data",
                        "work", "LIT-01", "log_lit01-repro.lammps")
    if not os.path.exists(live):
        check("REAL", "实弹 LIT-01 日志存在", False, live)
        return
    cid = "M-REAL-01"
    new_case(cid)
    rec, _ = content_check.check(cid, "a0", 3.615,
                                 {"path": live, "marker": "EVLIVE_A0"},
                                 tolerance=0.005, data_root=DATA)
    check("REAL", "真实日志重算 a0=3.615 → PASS",
          rec["verdict"] == "PASS" and abs(rec["measured"] - 3.615) < 1e-6,
          rec.get("measured"))
    rec2, _ = content_check.check(cid, "a0", 3.70,
                                  {"path": live, "marker": "EVLIVE_A0"},
                                  tolerance=0.005, data_root=DATA)
    check("REAL", "真实日志重算 a0=3.70 → FAIL", rec2["verdict"] == "FAIL",
          rec2.get("reason"))


def main():
    global DATA
    DATA = tempfile.mkdtemp(prefix="ev_matrix_")
    os.environ["MDSIM_DATA_ROOT"] = DATA
    t0 = time.time()
    print("sandbox:", DATA)
    for fn in (test_e1, test_e2, test_e3, test_e4, test_e5, test_binding,
               test_surface_matrix, test_surface_p1,
               test_high_risk, test_knowledge_dimensions,
               test_anchor_injection, test_real_artifact):
        try:
            fn()
        except Exception as ex:
            check("ERROR", fn.__name__, False, "%s: %s" % (type(ex).__name__, ex))
    passed = sum(1 for r in ROWS if r["ok"])
    total = len(ROWS)
    groups = {}
    for r in ROWS:
        g = groups.setdefault(r["group"], [0, 0])
        g[1] += 1
        if r["ok"]:
            g[0] += 1
    print("\n== 分组 ==")
    for g, (a, b) in groups.items():
        print("  %-6s %d/%d" % (g, a, b))
    print("== 总计 %d/%d，用时 %.1fs ==" % (passed, total, time.time() - t0))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs",
                       time.strftime("%Y%m%dT%H%M%S"), "functional_matrix.json")
    wjson(out, {"passed": passed, "total": total, "groups": groups,
                "rows": ROWS, "sandbox": DATA})
    print("report:", out)
    shutil.rmtree(DATA, ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
