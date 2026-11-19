#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Specialist 模板族 v1(2026-09-03,L2 结构自进化)

演化通道起草新恢复策略时,**不能自由发挥**:只能从本注册表选模板+网格内参数。
本文件在 POLICY forbidden_paths 内(只能人工改)——模板是"考题与答案"的来源,
若通道能改模板,证据门全部失效(循环论证)。

每模板字段:
  root_causes     适用根因(identify_root_cause 的五类)
  param_grid      参数网格,instantiate 只接受网格内取值
  entry_for(sid, params)  生成 db 策略条目(确定性,无时间戳——apply 门要逐字节比对)
  fixture_input   沙盒试跑的 production.in 初值
  fault_anomalies 驱动 identify_root_cause 命中目标根因的异常输入
  invariant(new_content)  试跑后必须成立的断言
  match_terms     蜂群映射关键词表(人工交付):联网研究文本命中计分用
  param_patterns  参数网格值 ← 文本提及的 regex 映射(人工交付),蜂群选参用
"""

TEMPLATES = {
    # 热失控兜底:现有 B(reduce_timestep)之外的新机制假设——只压步长不动阻尼
    "timestep_floor_damp": {
        "root_causes": ["thermal_runaway"],
        "param_grid": {"timestep": ["0.1", "0.25"]},
        "match_terms": ["timestep", "步长", "time step", "积分步长"],
        "param_patterns": {"timestep": {r"0\.1\s*fs|timestep\s+0\.1\b|步长.{0,12}0\.1": "0.1",
                                        r"0\.25\s*fs|timestep\s+0\.25|步长.{0,12}0\.25": "0.25"}},
        "for": ["TEMPERATURE_SPIKE", "rapid_heating"],
        "desc_tpl": "专家规则:timestep 压至 %s(仅步长,不动阻尼)",
        "rule": lambda p: {
            "version": 1,
            "replace": {"pattern": r"^timestep\s+[\d.]+",
                        "template": "timestep %s" % p["timestep"],
                        "count": 1, "flags": ["M"]},
            "guard": {"require_present": ["fix"]},
            "max_output_delta": 500,
        },
        "fixture_input": ("units real\nread_data heat_final.data\n"
                          "fix QEQ all qeq/reax 1 0.0 10.0 1.0e-6\n"
                          "timestep 0.5\n"
                          "fix 1 all nvt temp 300.0 1200.0 100.0\n"
                          "run 50000\n"),
        "fault_anomalies": [{"type": "TEMPERATURE_SPIKE", "detail": "sandbox"}],
        "invariant": lambda p, c: ("timestep %s" % p["timestep"]) in c
                                  and "timestep 0.5" not in c,
    },
    # QEq 不收敛兜底:F(qeq_relax 1e-5)之后更强的放宽档位
    "qeq_relax_stronger": {
        "root_causes": ["QEq_instability"],
        "param_grid": {"tol": ["1.0e-4", "1.0e-3"]},
        "match_terms": ["qeq", "收敛", "convergence", "tolerance", "电荷平衡",
                        "charge equilibration", "放宽"],
        "param_patterns": {"tol": {r"1\.?0?e-?4\b|0\.0001": "1.0e-4",
                                   r"1\.?0?e-?3\b|0\.001": "1.0e-3"}},
        "for": ["QEQ_FAILURE", "ENERGY_SPIKE"],
        "desc_tpl": "专家规则:QEq 收敛阈值放宽至 %s(强档)",
        "rule": lambda p: {
            "version": 1,
            "replace": {"pattern": r"fix\s+QEQ\s+all\s+qeq/reax\s+1\s+0\.0\s+10\.0\s+1\.0e-6",
                        "template": "fix QEQ all qeq/reax 1 0.0 10.0 %s" % p["tol"],
                        "count": 1},
            "guard": {"require_present": ["qeq/reax"]},
            "max_output_delta": 500,
        },
        "fixture_input": ("units real\nread_data heat_final.data\n"
                          "fix QEQ all qeq/reax 1 0.0 10.0 1.0e-6\n"
                          "timestep 0.5\n"
                          "run 50000\n"),
        "fault_anomalies": [{"type": "QEQ_FAILURE", "detail": "sandbox"}],
        "invariant": lambda p, c: ("qeq/reax 1 0.0 10.0 %s" % p["tol"]) in c,
    },
    # 原子飞出兜底:D(nve/limit 0.05)之后更紧的位移限制(插入式,带回溯引用)
    "nve_limit_tighten": {
        "root_causes": ["atomic_ejection"],
        "param_grid": {"lim": ["0.01", "0.02"]},
        "match_terms": ["nve/limit", "位移限制", "lost atoms", "原子飞出", "limit"],
        "param_patterns": {"lim": {r"nve/limit\s+0\.01\b|位移.{0,12}0\.01": "0.01",
                                   r"nve/limit\s+0\.02\b|位移.{0,12}0\.02": "0.02"}},
        "for": ["LOST_ATOMS", "TEMPERATURE_SPIKE"],
        "desc_tpl": "专家规则:全程 nve/limit %s(紧档)",
        "rule": lambda p: {
            "version": 1,
            "replace": {"pattern": r"(fix\s+\w+\s+all\s+nvt\s+temp[^\n]*)",
                        "template": "fix NVE_LIM all nve/limit %s\n\\1" % p["lim"],
                        "count": 1},
            "guard": {"require_present": ["nvt"], "require_absent": ["nve/limit"]},
            "max_output_delta": 500,
        },
        "fixture_input": ("units real\nread_data heat_final.data\n"
                          "timestep 0.5\n"
                          "fix 1 all nvt temp 300.0 1200.0 100.0\n"
                          "run 50000\n"),
        "fault_anomalies": [{"type": "LOST_ATOMS", "detail": "sandbox"}],
        "invariant": lambda p, c: ("nve/limit %s" % p["lim"]) in c,
    },
}


def templates_for_cause(root_cause):
    """该根因可用的模板 id 列表(注册表顺序)。"""
    return [tid for tid, t in TEMPLATES.items() if root_cause in t["root_causes"]]


def instantiate(template_id, sid, params):
    """网格内参数 → 确定性 db 条目(无时间戳,apply 门逐字节比对用);
    模板未知/参数越网格 → None。"""
    t = TEMPLATES.get(template_id)
    if t is None or not isinstance(params, dict):
        return None
    for k, v in params.items():
        grid = t["param_grid"].get(k)
        if grid is None or v not in grid:
            return None
    if set(params) != set(t["param_grid"]):
        return None  # 参数必须恰好覆盖网格键,不多不少
    entry = {
        "name": "spec_%s" % template_id,
        "desc": t["desc_tpl"] % tuple(params[k] for k in sorted(params)),
        "for": list(t["for"]),
        "success_rate": 0.5,
        "uses": 0,
        "canary": True,
        "rule": t["rule"](params),
        "template": template_id,
        "params": dict(params),
    }
    return entry


def first_params(template_id):
    """网格首组参数(起草默认值;晋升/降级不挑参数,试点期一因一模板一首参)。"""
    t = TEMPLATES[template_id]
    return {k: v[0] for k, v in sorted(t["param_grid"].items())}
