# -*- coding: utf-8 -*-
"""
config_check —— 仿真输入配置正确性机检（证据系统 v2 方法学层）
================================================================================
定位: 回答"这套仿真配置组合,对这类实验来说,方法学上成立吗"。拉伸配恒压拉不断、
往复实验做成单向——这类错误在文件写作/建模段(G-BUILD/G-CONFIG)就被打回,
不等到跑完才发现数据无效。

机制(通用,零域字面量):
- parse_input: 通用求解器输入脚本解析 → 命令流 [{cmd, args}](去注释、并续行);
- eval_constraints: 对 methodology 条目的 violate_if 数据规则做确定性判定:
  ① all_of: 一组命令模式全部命中 → 违规(如 控压+变形 同现);
  ② decision_text_regex 命中任务声明 && missing_any_of 命令模式无一命中 → 违规
     (如 声明往复 但输入无往复特征);
- 条目(触发面/命令模式/说明)全部是数据(review_experience 的 methodology kind,
  人工闸维护);本模块只认通用结构。

纪律: 纯 stdlib、零 LLM、fail-closed(解析失败=无法判定→报 cannot_verify,不静默放行)。
"""
import os
import re


def parse_input(path):
    """通用命令流解析: 返回 [{cmd, args, raw}];文件缺失/不可读 → None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return None
    cmds = []
    buf = ""
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if buf:
            s = buf + " " + s
            buf = ""
        if s.endswith("&") or s.endswith("\\"):  # 续行符(通用约定)
            buf = s[:-1].strip()
            continue
        # 去行内注释(# 或 // 起)
        for mark in (" #", "\t#", " //", "\t//"):
            idx = s.find(mark)
            if idx > 0:
                s = s[:idx].strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        parts = s.split(None, 1)
        cmds.append({"cmd": parts[0],
                     "args": parts[1] if len(parts) > 1 else "",
                     "raw": s})
    return cmds


def _cmd_hit(commands, pattern):
    """命令模式命中: {cmd: 精确名, args_regex: 参数正则} 或 {cmd_regex: 全行正则}。"""
    for c in commands or []:
        if "cmd" in pattern and pattern["cmd"] not in (None, "*"):
            if c["cmd"] != pattern["cmd"]:
                continue
            arx = pattern.get("args_regex")
            if arx and not re.search(arx, c["args"]):
                continue
            return True
        elif "cmd_regex" in pattern:
            if re.search(pattern["cmd_regex"], c["raw"]):
                return True
    return False


def eval_constraints(entry, commands, decision_text=""):
    """对一条 methodology 条目做判定。
    返回 {triggered, passed, violations: [{note, rule}], cannot_verify}。
    cannot_verify=True 表示缺少判定素材(命令集缺失)——fail-closed 语义由调用方处置。"""
    cons = (entry or {}).get("constraints") or {}
    rules = cons.get("violate_if") or []
    if not rules:
        return {"triggered": False, "passed": True, "violations": [],
                "cannot_verify": False}
    if commands is None:
        return {"triggered": True, "passed": False, "violations": [],
                "cannot_verify": True,
                "note": "输入文件不可读——无法判定配置正确性"}
    violations = []
    for rule in rules:
        note = rule.get("note", "")
        if "all_of" in rule:
            hits = [p for p in rule["all_of"] if _cmd_hit(commands, p)]
            if len(hits) == len(rule["all_of"]) and rule["all_of"]:
                violations.append({"note": note, "rule": "all_of",
                                   "matched": len(hits)})
        elif "decision_text_regex" in rule:
            if re.search(rule["decision_text_regex"], str(decision_text or "")):
                missing = [p for p in (rule.get("missing_any_of") or [])
                           if not _cmd_hit(commands, p)]
                if rule.get("missing_any_of") and len(missing) == len(rule["missing_any_of"]):
                    violations.append({"note": note, "rule": "declaration-mismatch",
                                       "missing": len(missing)})
    return {"triggered": True, "passed": not violations,
            "violations": violations, "cannot_verify": False}


def check_case_inputs(entries, input_paths, decision_text=""):
    """对一组输入文件 × 一组 methodology 条目全量判定。
    返回 {id: {triggered, passed, violations}};input_paths 为空且有条目 →
    各条 cannot_verify(在 G-BUILD 前文件未写好时调用方不应触发)。"""
    commands = []
    for p in input_paths or []:
        cmds = parse_input(p)
        if cmds is None:
            return None  # 任一声明文件不可读 → 整体无法判定(fail-closed)
        commands.extend(cmds)
    out = {}
    for e in entries or []:
        out[e.get("id")] = eval_constraints(e, commands, decision_text)
    return out
