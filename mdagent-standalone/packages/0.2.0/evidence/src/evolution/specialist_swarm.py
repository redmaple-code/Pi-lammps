#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蜂群研究驱动起草(2026-09-05,L2 演化·耗尽触发)
==================================================
策略库耗尽(某根因标准工具箱全败)时,调 mcp/model_research.py 的 repair
流水线(蜂群:拆题→多路并行联网收集→量规评审→代码终裁)寻找新策略,
然后把研究产物**确定性地映射到人工模板空间**(选模板+网格参数)。

红线(用户 2026-09-05 拍板):
- 蜂群只能在模板注册表内做选择——证据门"条目==模板产物"逐字节比对不松动;
- 选择函数是"蜂群输出 × 人工 match_terms/param_patterns 表"的确定性映射,
  AI 判断咨询化、可审计;低置信一律回退模板首参(与盲选逐字节等价);
- 蜂群对全部模板零命中=新机制 → 由 observe 层转 human_review 提案,
  模板永远人工交付;
- 本模块任何失败 → None,起草流程永不被蜂群阻塞(网络/额度/解析全可能坏)。

知识库:rsi_knowledge/repair_kb.jsonl(追加式剧集)。剧集回灌后续蜂群
question(同根因历史研判+金丝雀结局),构成"更新知识库"闭环。
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
KB_PATH = os.path.join(HERE, "rsi_knowledge", "repair_kb.jsonl")

# 根因 → 蜂群提问用的自然语言描述(identify_root_cause 的内部名不可直接联网)
CAUSE_DESC = {
    "thermal_runaway": "ReaxFF NVT 升温阶段温度失控飙升(热失控)",
    "QEq_instability": "ReaxFF QEq 电荷平衡不收敛/能量发散",
    "atomic_ejection": "原子飞出丢失(lost atoms)/局部速度爆炸",
    "structural_collapse": "结构塌缩/密度异常",
    "input_error": "输入脚本错误",
}


def _load_model_research():
    """导入蜂群流水线(mcp/model_research.py,纯 stdlib)。失败 → None。"""
    try:
        if MCP_DIR not in sys.path:
            sys.path.insert(0, MCP_DIR)
        import model_research
        return model_research
    except Exception:
        return None


def kb_episodes(cause=None, types=None, limit=None):
    """读知识库剧集(时间升序)。任何读取问题 → [](知识库绝不可拖垮起草)。"""
    rows = []
    try:
        if not os.path.exists(KB_PATH):
            return rows
        for line in open(KB_PATH, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                ep = json.loads(line)
            except ValueError:
                continue  # 半行/坏行跳过,不容忍即死(追加式日志的常态)
            if not isinstance(ep, dict):
                continue
            if cause is not None and ep.get("root_cause") != cause:
                continue
            if types is not None and ep.get("type") not in types:
                continue
            rows.append(ep)
    except OSError:
        return []
    if limit is not None:
        rows = rows[-limit:]
    return rows


def append_episode(ep):
    """追加一条剧集到知识库。返回 True/False;绝不抛。"""
    try:
        os.makedirs(os.path.dirname(KB_PATH), exist_ok=True)
        ep = dict(ep)
        ep.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(KB_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _throttled(cause, ev):
    """节流判定:同因冷却 + 全局日额度双闸。返回 True=本论不许跑蜂群。"""
    cooldown = float(ev.get("swarm_cooldown_hours", 24)) * 3600
    max_per_day = int(ev.get("swarm_max_per_day", 4))
    try:
        episodes = kb_episodes(types={"swarm_research"})
    except Exception:
        return True  # 知识库不可读 → fail-closed 不跑(省费用)
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    day_count = 0
    last_cause_ts = 0.0
    for ep in episodes:
        ets = _ep_ts(ep)
        if ets is None:
            continue
        if time.strftime("%Y-%m-%d", time.localtime(ets)) == today:
            day_count += 1
        if ep.get("root_cause") == cause:
            last_cause_ts = max(last_cause_ts, ets)
    if day_count >= max_per_day:
        return True
    if last_cause_ts and now - last_cause_ts < cooldown:
        return True
    return False


def _ep_ts(ep):
    try:
        return time.mktime(time.strptime(str(ep.get("ts", "")),
                                         "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def research_cause(cause, ev):
    """跑一遍蜂群 repair 流水线。question 带同因近 3 条剧集(知识库回灌)。
    返回 run() 的结构化结果;禁用/节流/任何失败 → None。"""
    if not ev.get("swarm_research"):
        return None
    if _throttled(cause, ev):
        return None
    mr = _load_model_research()
    if mr is None:
        return None
    desc = CAUSE_DESC.get(cause, cause)
    question = ("%s:该异常的标准修复工具箱已耗尽(全部既有策略失败或被压制),"
                "需要机理不同的新修复策略" % desc)
    history_eps = kb_episodes(cause=cause, limit=3)
    if history_eps:
        ctx_lines = []
        for ep in history_eps:
            if ep.get("type") == "swarm_research":
                ctx_lines.append("既往研判: %s(选了 %s)" % (
                    str(ep.get("verdict", ""))[:120],
                    json.dumps(ep.get("picked"), ensure_ascii=False)))
            elif ep.get("type") == "canary_outcome":
                ctx_lines.append("金丝雀结局: 策略 %s %s" % (
                    ep.get("sid"), ep.get("op")))
        if ctx_lines:
            question += "。知识库历史: " + " | ".join(ctx_lines)
    try:
        return mr.run(question, collectors=4, mode="repair")
    except Exception:
        return None


def pick_template(cause, research, st, allowed):
    """研究产物 → (template_id, params, meta) 的确定性映射。

    计分:每模板 match_terms 在各候选策略文本(verified 权重×2)+verdict 的
    命中数加总;参数:按终裁排序扫策略文本,param_patterns 首个命中定网格值。
    全部零命中或研究不可用 → (首模板, 首参, fallback=True)。
    meta["novel"]=True 表示研究有结论但所有模板零命中(新机制,转人工)。
    """
    tids = [t for t in st.templates_for_cause(cause) if t in allowed]
    if not tids:
        return None
    default = (tids[0], st.first_params(tids[0]))
    if not isinstance(research, dict) or research.get("status") != "OK":
        return (default[0], default[1],
                {"fallback": True, "novel": False, "scores": {}})

    strategies = [s for s in research.get("strategies", [])
                  if isinstance(s, dict) and s.get("name")]
    verdict = str(research.get("verdict", ""))
    if not strategies:
        return (default[0], default[1],
                {"fallback": True, "novel": False, "scores": {}})

    blobs = []
    for s in strategies:  # _arbitrate 已按 verified+分数排序,顺序即优先级
        blob = "%s %s %s" % (s.get("name", ""), s.get("when_to_use", ""), verdict)
        blobs.append((blob, 2 if s.get("verified") else 1))

    scores = {}
    for tid in tids:
        terms = [str(x).lower() for x in
                 st.TEMPLATES[tid].get("match_terms", [])]
        score = 0
        for blob, w in blobs:
            low = blob.lower()
            score += w * sum(1 for term in terms if term and term in low)
        scores[tid] = score
    best_tid = max(tids, key=lambda t: (scores[t], -tids.index(t)))
    if scores[best_tid] <= 0:
        return (default[0], default[1],
                {"fallback": True, "novel": True, "scores": scores})

    params = st.first_params(best_tid)
    patterns = st.TEMPLATES[best_tid].get("param_patterns") or {}
    picked_from = None
    for key, mapping in patterns.items():
        if key not in params or not isinstance(mapping, dict):
            continue
        for blob, _w in blobs:
            hit = None
            for pat, val in mapping.items():
                try:
                    if re.search(pat, blob, re.I):
                        hit = val
                        break
                except re.error:
                    continue
            if hit is not None and hit in st.TEMPLATES[best_tid]["param_grid"].get(key, []):
                params[key] = hit
                picked_from = blob[:80]
                break
    return (best_tid, params,
            {"fallback": False, "novel": False, "scores": scores,
             "param_evidence": picked_from})


def swarm_pick(cause, ev, st, allowed):
    """一步式入口:节流→蜂群研究→映射→写知识库剧集。
    返回 {"template_id","params","fallback","novel","research"} 或 None
    (未跑蜂群:禁用/节流/流水线失败——调用方回退盲选,行为与蜂群接入前等价)。"""
    try:
        research = research_cause(cause, ev)
        if research is None:
            return None
        pick = pick_template(cause, research, st, allowed)
        if pick is None:
            return None
        tid, params, meta = pick
        append_episode({
            "type": "swarm_research", "root_cause": cause,
            "question": research.get("question", ""),
            "verdict": research.get("verdict", ""),
            "strategies": research.get("strategies", []),
            "sources": [s.get("url", "") for s in research.get("sources", [])
                        if isinstance(s, dict)],
            "picked": {"template": tid, "params": params},
            "fallback": bool(meta.get("fallback")),
            "novel": bool(meta.get("novel")),
            "cost": research.get("cost", {}),
            "elapsed_sec": research.get("elapsed_sec"),
        })
        return {"template_id": tid, "params": params,
                "fallback": bool(meta.get("fallback")),
                "novel": bool(meta.get("novel")),
                "research": research}
    except Exception:
        return None
