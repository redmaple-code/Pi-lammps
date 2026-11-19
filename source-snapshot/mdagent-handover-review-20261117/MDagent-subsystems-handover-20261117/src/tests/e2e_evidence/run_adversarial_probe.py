# -*- coding: utf-8 -*-
"""对抗探针：构造测试套件里没有预设的输入，看门禁怎么判。"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(SRC, "evolution"))
sys.path.insert(0, os.path.join(SRC, "mcp"))
sys.path.insert(0, os.path.dirname(HERE))

import stage_state  # noqa: E402
import stage_gate   # noqa: E402
import surface      # noqa: E402
import surface_fixture  # noqa: E402

DR = tempfile.mkdtemp(prefix="probe_")
os.environ["MDSIM_DATA_ROOT"] = DR
STD = "paper:10.1103/PhysRevB.33.7983"
RES = []


def probe(name, expect, got, detail=""):
    ok = expect(got)
    RES.append((name, ok, detail))
    print("%s [%s] %s | %s" % ("OK " if ok else "?? ", "PROBE", name, str(detail)[:130]))


def mk(cid, risk="medium"):
    stage_state.init_task(cid, risk_level=risk, data_root=DR)
    surface_fixture.fill_all(DR, cid)
    stage_state.lock_decisions(cid, {"D1_对象与范围": "fcc Cu 弹性常数"}, DR,
                               basis={"D1_对象与范围": {"kind": "literature",
                                                        "ref": STD}})
    d = os.path.join(stage_state.task_dir(cid, DR), "assessments")
    os.makedirs(d, exist_ok=True)
    json.dump({"case_id": cid, "phase": "plan", "family": "elastic-constants",
               "findings": [], "critical_open": [], "overall": "sound"},
              open(os.path.join(d, "p.json"), "w", encoding="utf-8"))


def gate(cid, g, **kw):
    r = {"schema_version": 1, "case_id": cid,
         "stage": {"G-PLAN": "PLAN", "G-BUILD": "BUILD", "G-CONFIG": "CONFIG",
                   "G-EXEC": "EXEC", "G-ANALYZE": "ANALYZE"}.get(g, "PLAN"),
         "gate": g, "decision": "PASS", "summary": "probe",
         "evidence_refs": [], "acceptance_checks": [], "input_files": []}
    r.update(kw)
    return stage_gate.gate_and_log(cid, r, data_root=DR)


# P1 伪造论文引用（不可解析）
cid = "PR-01"
mk(cid)
stage_state.lock_decisions(cid, {"D1_对象与范围": "x", "D5_关键参数": "y"}, DR,
                           basis={"D1_对象与范围": {"kind": "literature", "ref": "paper:x"},
                                  "D5_关键参数": {"kind": "literature", "ref": "paper:x"}})
r = gate(cid, "G-PLAN")
probe("伪造论文引用(paper:x) → 拒",
      lambda x: x["result"] == "BLOCK" and any("evidence-insufficient" in i for i in x["reasons"]),
      r, r["reasons"][:2])

# P2 幽灵案例引用
cid = "PR-02"
mk(cid)
stage_state.lock_decisions(cid, {"D1_对象与范围": "x"}, DR,
                           basis={"D1_对象与范围": {"kind": "prior_case",
                                                    "ref": "case:GHOST-99"}})
r = gate(cid, "G-PLAN")
probe("幽灵案例(case:GHOST-99) → 不计入并拒",
      lambda x: x["result"] == "BLOCK" and any("evidence-insufficient" in i for i in x["reasons"]),
      r, r["reasons"][:2])

# P5 泛泛的人工覆盖理由 + 真实冲突
cid = "PR-05"
mk(cid)
w = os.path.join(stage_state.task_dir(cid, DR), "confirmations")
os.makedirs(w, exist_ok=True)
json.dump({"id": "u5", "scope": cid, "value": 25,
           "ts": "2026-10-23T00:00:00Z"},
          open(os.path.join(w, "u5.json"), "w", encoding="utf-8"))
stage_state.lock_decisions(cid, {"D5_关键参数": "dt=25fs"}, DR,
                           basis={"D5_关键参数": {"kind": "user", "ref": "user:u5",
                                                  "value": 25, "reason": "用户要求",
                                                  "scope": cid}})
doc = stage_state.read_decisions(cid, DR)
doc["D5_关键参数"]["evidence"] = [{"ref": STD, "value": 2.0}]
json.dump(doc, open(os.path.join(stage_state.task_dir(cid, DR),
                                 "decisions.json"), "w", encoding="utf-8"))
r = gate(cid, "G-PLAN")
probe("泛泛理由(「用户要求」)面对真实冲突 → 追问而非放行",
      lambda x: x["result"] == "BLOCK" and any(
          ("conflict-unresolved" in i or "override" in i) for i in x["reasons"])
      and bool(x.get("conflicts")),
      r, {"reasons": r["reasons"][:2], "pack": bool(r.get("conflicts"))})

# P6 REVISE 工件
cid = "PR-06"
mk(cid)
r = gate(cid, "G-PLAN", decision="REVISE")
probe("REVISE 工件 → HOLD（只记录不推进）",
      lambda x: x["result"] == "HOLD", r, r["result"])

# P7 阶段不对时的执行请求
import md_mcp  # noqa: E402
out = md_mcp._exec_gate_check({"case_id": "PR-06", "run_kind": "production"})
probe("PLAN 阶段提交执行 → sim_exec 拒",
      lambda x: isinstance(x, dict) and any("gate-not-passed" in str(i) for i in x.get("reasons", [])),
      out, out.get("reasons") if isinstance(out, dict) else out)

# P8 篡改 factcheck（verdict=PASS 但 measured 与 claimed 不符）
cid = "PR-08"
mk(cid)
d = os.path.join(stage_state.task_dir(cid, DR), "factchecks")
os.makedirs(d, exist_ok=True)
json.dump({"field": "a0", "claimed": 3.615, "measured": 9.99,
           "tolerance": 0.005, "verdict": "PASS", "source": {},
           "ts": "2099-01-01T00:00:00Z"},
          open(os.path.join(d, "f1.json"), "w", encoding="utf-8"))
json.dump({"case_id": cid, "phase": "results", "family": "elastic-constants",
           "findings": [], "critical_open": [], "overall": "sound"},
          open(os.path.join(stage_state.task_dir(cid, DR), "assessments",
                            "r.json"), "w", encoding="utf-8"))
r = gate(cid, "G-ANALYZE", acceptance_checks=[
    {"item": "a0", "claimed": 3.615, "passed": True,
     "factcheck": "factcheck:f1.json"}])
caught = r["result"] == "BLOCK"
probe("篡改 factcheck(measured=9.99 仍标 PASS) → " + ("被拦" if caught else "未拦（缺口）"),
      lambda x: True, r,
      r["reasons"][:2] if caught else "E2 只校验 verdict，不复核 measured/claimed 一致性")

# P10 裸路径当证据
cid = "PR-10"
mk(cid)
r = gate(cid, "G-PLAN", evidence_refs=["C:/Windows/system32/config.sys"])
probe("裸文件路径当证据 → 不计入(F 级)",
      lambda x: all(not e["countable"] and e["grade"] == "F"
                    for e in x["evidence"]),
      r, [(e["ref"][:20], e["grade"], e["countable"]) for e in r["evidence"]])

print()
passed = sum(1 for _, ok, _ in RES if ok)
print("探针 %d/%d 行为符合预期" % (passed, len(RES)))
json.dump({"probes": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RES]},
          open(os.path.join(HERE, "runs", "probe_results.json"), "w",
               encoding="utf-8"), ensure_ascii=False, indent=2)
shutil.rmtree(DR, ignore_errors=True)
