#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Specialist 沙盒试跑器(2026-09-03,L2 结构自进化)

install 提案的证据门:在 tempdir 沙盒里把候选策略**真跑一遍**恢复链。
考题(fixture/异常/断言)全部出自 specialist_templates(代码),绝不采信提案内容
——提案里的 sandbox_report.json 只是审计佐证,门由本模块重执行。

三层检查:
  1. 静态:validate_rule + 幂等(apply∘apply == apply)+ 空/垃圾/超长输入探针
  2. 链路:fixture prod_dir + 候选入库 + 网络硬断 + mock research/restart,
     history 标记六 legacy 已用(候选=唯一可选,直测金丝雀兜底路径)
  3. 断言:根因命中/候选被选中/production.in 改写且 invariant 成立/
     db 不变量(uses+1、恰一条 pending、根因正确)/目录树无越界写

CLI(供 apply_proposal 子进程调用,timeout 180s):
  python3.11 specialist_sandbox.py --trial spec.json
  spec = {"template": "...", "sid": "G", "params": {...}}
  stdout 末行输出 JSON 报告 {"ok": bool, "checks": {...}};退出码 0/1。

仅 py3.11(演化层侧;kit 运行时不依赖本文件)。
"""
import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import urllib.request

EVO_DIR = os.path.dirname(os.path.abspath(__file__))
if EVO_DIR not in sys.path:
    sys.path.insert(0, EVO_DIR)

import specialist_templates  # noqa: E402

KIT_V33 = os.environ.get(
    "KIT_V33",
    "/opt/mdagent/kit/agents/runtime/v3.3")

LEGACY_SIDS = ["A", "B", "C", "D", "E", "F"]
EXPECTED_WRITES = ("production.in", "production.in.recovery_",
                   ".v3.3_strategy_db.json", ".v3.3_strategy_db.json.lock",
                   "trial_in.json", "trial_out.json")


def _load_kit_module(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(KIT_V33, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _no_net(*a, **kw):
    raise AssertionError("sandbox: network access blocked")


class _NoSock(object):
    """21 轮对抗:urlopen 之外把 socket 层也封死——被试试跑不得有任何外联面。"""
    def __init__(self, *a, **kw):
        raise AssertionError("sandbox: network access blocked")


def _snapshot(root):
    snap = {}
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for fn in files:
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, root)
            st = os.stat(p)
            snap[rel] = (st.st_size, st.st_mtime_ns)
    return snap


def _static_checks(entry, checks):
    specialist_rules = _load_kit_module("specialist_rules")
    rule = entry["rule"]
    err = specialist_rules.validate_rule(rule)
    checks["validate_rule"] = (err is None, err or "ok")
    if err:
        return False

    fixture = ""
    tpl = specialist_templates.TEMPLATES[entry["template"]]
    fixture = tpl["fixture_input"]
    c1, ch1 = specialist_rules.apply_rule(fixture, rule)
    c2, ch2 = specialist_rules.apply_rule(c1, rule)
    checks["idempotent"] = (c2 == c1 and ch2 == [], "两次执行结果一致" if c2 == c1 else "非幂等")

    probes_ok = True
    detail = "ok"
    for probe in ("", "\x00\xff 垃圾 \n" * 3, "timestep 9\n" * 20000):
        try:
            specialist_rules.apply_rule(probe, rule)
        except Exception as e:  # 探针必须优雅,任何异常都算失败
            probes_ok = False
            detail = "probe raised: %r" % e
            break
    checks["probes"] = (probes_ok, detail)
    return all(v[0] for v in checks.values())


def run_trial(template_id, sid, params):
    """完整试跑。返回 (ok, report dict)。任何步骤失败都继续收集完报告。"""
    checks = {}
    entry = specialist_templates.instantiate(template_id, sid, params)
    if entry is None:
        return False, {"ok": False, "error": "instantiate 失败(模板未知/参数越网格)",
                       "checks": checks}
    tpl = specialist_templates.TEMPLATES[template_id]

    if not _static_checks(entry, checks):
        return False, {"ok": False, "checks": checks}

    tmp = tempfile.mkdtemp(prefix="spec_trial_")
    prod_dir = os.path.join(tmp, "prod")
    os.makedirs(prod_dir)
    with open(os.path.join(prod_dir, "production.in"), "w") as f:
        f.write(tpl["fixture_input"])

    os.environ["AGENT_BASE"] = tmp
    ra = _load_kit_module("recover_agent")
    db = ra.load_db()  # 无库文件 → 默认 A-F
    db["strategies"][sid] = entry
    with open(ra.STRATEGY_DB, "w") as f:
        json.dump(db, f, indent=2)

    ra.run_research = lambda *a, **kw: None
    ra.restart_production = lambda *a, **kw: [4242]
    urllib.request.urlopen = _no_net

    in_path = os.path.join(tmp, "trial_in.json")
    out_path = os.path.join(tmp, "trial_out.json")
    with open(in_path, "w") as f:
        json.dump({
            "prod_dir": prod_dir,
            "anomalies": tpl["fault_anomalies"],
            "history": [{"strategy": s} for s in LEGACY_SIDS],  # legacy 全用过→候选唯一可选
            "risk_score": 90, "risk_factors": [],
            "case_id": "sandbox-trial", "cores": 1,
        }, f)

    before = _snapshot(tmp)
    old_argv = sys.argv
    old_sock, old_conn = socket.socket, socket.create_connection
    socket.socket = _NoSock
    socket.create_connection = _no_net
    sys.argv = ["recover_agent.py", in_path, out_path]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ra.main()
    except Exception as e:
        checks["main_runs"] = (False, "main 抛异常: %r" % e)
        return False, {"ok": False, "checks": checks}
    finally:
        sys.argv = old_argv
        socket.socket = old_sock
        socket.create_connection = old_conn
    checks["main_runs"] = (True, "ok")

    with open(out_path) as f:
        result = json.load(f)
    checks["cause_match"] = (
        result.get("root_cause") in tpl["root_causes"],
        "root_cause=%s" % result.get("root_cause"))
    checks["candidate_selected"] = (
        result.get("strategy") == sid,
        "strategy=%s" % result.get("strategy"))
    checks["success"] = (result.get("success") is True,
                         "success=%s err=%s" % (result.get("success"), result.get("error")))

    with open(os.path.join(prod_dir, "production.in")) as f:
        new_content = f.read()
    checks["rewritten"] = (new_content != tpl["fixture_input"], "production.in 已改写"
                           if new_content != tpl["fixture_input"] else "未改写")
    inv = False
    try:
        inv = bool(tpl["invariant"](params, new_content))
    except Exception as e:
        checks["invariant"] = (False, "invariant 抛异常: %r" % e)
    else:
        checks["invariant"] = (inv, "ok" if inv else "模板断言不成立")

    with open(ra.STRATEGY_DB) as f:
        db2 = json.load(f)
    pend = [h for h in db2.get("history", [])
            if h.get("strategy") == sid and h.get("outcome") == "pending"]
    checks["db_uses"] = (db2["strategies"][sid].get("uses") == 1,
                         "uses=%s" % db2["strategies"][sid].get("uses"))
    checks["db_pending"] = (len(pend) == 1
                            and pend[0].get("root_cause") == result.get("root_cause"),
                            "%d 条 pending" % len(pend))

    after = _snapshot(tmp)
    strays = []
    for rel in set(list(before) + list(after)):
        if before.get(rel) == after.get(rel):
            continue
        base = rel.split(os.sep)[-1]
        if not any(base == w or base.startswith(w) for w in EXPECTED_WRITES):
            strays.append(rel)
    checks["tree_clean"] = (not strays, "ok" if not strays else "越界写: %s" % strays)

    ok = all(v[0] for v in checks.values())
    return ok, {"ok": ok, "sid": sid, "template": template_id,
                "params": params, "checks": checks}


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--trial":
        with open(sys.argv[2]) as f:
            spec = json.load(f)
        ok, report = run_trial(spec.get("template"), spec.get("sid"),
                               spec.get("params") or {})
        print(json.dumps(report, ensure_ascii=False))
        return 0 if ok else 1
    print("usage: specialist_sandbox.py --trial spec.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
