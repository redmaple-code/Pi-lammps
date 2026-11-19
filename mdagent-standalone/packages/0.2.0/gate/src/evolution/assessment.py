# -*- coding: utf-8 -*-
"""
assessment —— 物理合理性评估引擎（证据系统 v2,确定性零 LLM）
================================================================================
定位: 回答"仿真合理不合理"。对每项设计决策与实测结果,对照物理知识锚
(review_experience 的 physics-anchor 条目)逐条给出 sound/concern/critical
结论,产出不可变 assessment 记录,供门禁 R15 消费(critical 未清零即拦截)。

纪律:
- 纯 stdlib、零 LLM、零副作用(仅落自己的 assessments/ 工件,O_EXCL 不可变);
- 全部物理判据来自知识锚条目(数据,人工闸维护),本模块零域规则字面量;
- 锚不完备 ⇒ 对应项判 na(不拦),锚在才有判断——宁缺判据不瞎判;
- canary 锚只提示不拦(与 canary 经验语义同轨)。
"""
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")

VERDICTS = ("sound", "concern", "critical", "na")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_anchors(data_root=None):
    """载入 physics-anchor 条目: id -> entry(含 status)。种子包+运行时注入库。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_anchors(data_root)
    import stage_gate
    roots = [os.path.join(MCP_DIR, "review_experience")]
    env = os.environ.get("REVIEW_EXPERIENCE_DIR", "").strip()
    if env:
        roots.append(env)
    try:
        dr = stage_gate.stage_state._data_root(data_root)
    except Exception:
        # 独立导入场景(mcp 不在 sys.path):只用种子包,不让锚加载整体失败
        dr = None
    if dr:
        roots.append(os.path.join(dr, "memory", "review_experience"))
    out = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    pack = json.load(f)
            except Exception:
                continue
            if not isinstance(pack, dict):
                continue
            for e in pack.get("entries") or []:
                if isinstance(e, dict) and e.get("kind") == "physics-anchor" and e.get("id"):
                    out[str(e["id"])] = dict(e, _pack=name)
    return out


def _write_record(case_id, rec, phase, data_root):
    import stage_state
    import uuid
    d = os.path.join(stage_state_dir(case_id, data_root), "assessments")
    os.makedirs(d, exist_ok=True)
    name = "%s-%s-%s.json" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                              phase, uuid.uuid4().hex[:6])
    fp = os.path.join(d, name)
    with open(fp, "x", encoding="utf-8") as f:  # 不可变
        f.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return os.path.join("assessments", name).replace("\\", "/")


def stage_state_dir(case_id, data_root=None):
    import stage_state
    return stage_state.task_dir(case_id, data_root)


def _verdict_by_range(value, rng, better="lower"):
    """锚区间判级: recommended 阈内=sound;越推荐但未越 critical=concern;越 critical=critical。"""
    if better == "lower":
        rec, crit = rng.get("recommended_max"), rng.get("critical_max")
        if rec is None:
            rec = rng.get("recommended_max_frac")
        if value <= (rec if rec is not None else math.inf):
            return "sound"
        if crit is not None and value > crit:
            return "critical"
        return "concern"
    lo, hi = rng.get("min"), rng.get("max")
    if lo is not None and hi is not None and lo <= value <= hi:
        return "sound"
    return "critical"


# ---------------- 规则(全部数据驱动,判据在锚里) ----------------
def _rule_dt(anchors, decisions, facts):
    """设计步长 vs 约化步长锚(仅在决策声明了 dt 且锚覆盖该族时评估)。"""
    a = anchors.get("anchor-md-001")
    if not a or a.get("status") not in ("active", "canary"):
        return None
    dt_fs = None
    for sid, slot in decisions.items():
        m = re.search(r"dt[=＝:]\s*([0-9.]+)\s*fs", str(slot.get("value", "")))
        if m:
            dt_fs = float(m.group(1))
            break
    if dt_fs is None:
        return {"id": "PA1", "question": "时间步长是否物理稳定", "verdict": "na",
                "basis": ["exp:anchor-md-001"], "evidence": "决策未声明显式步长(如纯静态最小化)"}
    t_unit = (a.get("reduction") or {}).get("t_unit_ps")
    if not t_unit:
        return None
    dt_star = dt_fs / 1000.0 / t_unit
    v = _verdict_by_range(dt_star, a.get("range") or {}, better="lower")
    return {"id": "PA1", "question": "时间步长是否物理稳定(dt*=%.4f,推荐≤%s/危险>%s)"
            % (dt_star, (a.get("range") or {}).get("recommended_max"),
               (a.get("range") or {}).get("critical_max")),
            "verdict": v, "basis": ["exp:anchor-md-001"],
            "evidence": "决策声明的 dt=%gfs 换算约化步长 dt*=%.4f" % (dt_fs, dt_star),
            "blocking_only_if_active": a.get("status") == "active"}


def _rule_acceptance(anchors, decisions, facts):
    a = anchors.get("anchor-accept-001")
    if not a:
        return None
    items = []
    for sid, slot in decisions.items():
        if "验收" in sid and isinstance(slot.get("value"), list):
            items = [str(x) for x in slot["value"]]
    if not items:
        return None
    bad = [x for x in items
           if not re.search(r"[0-9]|[<>≤≥=]", x)]
    v = "sound" if not bad else "concern"
    return {"id": "PA2", "question": "验收标准是否全部可量化", "verdict": v,
            "basis": ["exp:anchor-accept-001"],
            "evidence": ("全部 %d 条含数值判据" % len(items)) if not bad
            else ("不可量化: %s" % "; ".join(bad[:2]))}


def _rule_temp(anchors, decisions, facts):
    a = anchors.get("anchor-temp-001")
    t_end, t_target = facts.get("temp_end_K"), facts.get("temp_target_K")
    if not a or t_end is None or t_target is None:
        return None
    if not math.isfinite(float(t_end)) or float(t_end) <= 0:
        return {"id": "RA1", "question": "控温是否物理有效", "verdict": "critical",
                "basis": ["exp:anchor-temp-001"], "evidence": "末温非有限/非正(数值失稳)"}
    dev = abs(float(t_end) - float(t_target))
    rng = a.get("range") or {}
    lim = max(float(rng.get("recommended_max_frac", 0.1)) * float(t_target),
              float(rng.get("recommended_max_abs", 15.0)))
    v = "sound" if dev <= lim else "concern"
    return {"id": "RA1", "question": "控温是否物理有效(末温偏离 %.1fK ≤ %.1fK)" % (dev, lim),
            "verdict": v, "basis": ["exp:anchor-temp-001"],
            "evidence": "末温 %s vs 目标 %s" % (t_end, t_target)}


def _rule_rdf(anchors, decisions, facts):
    a = anchors.get("anchor-rdf-001")
    peak = facts.get("rdf_peak_A")
    if not a or peak is None:
        return None
    sigma = (a.get("params") or {}).get("sigma_A")
    rng = a.get("range") or {}
    if not sigma:
        return None
    lo, hi = rng.get("min_frac", 0.9) * sigma, rng.get("max_frac", 1.15) * sigma
    v = "sound" if lo <= float(peak) <= hi else "critical"
    return {"id": "RA2", "question": "液态结构是否合理(首峰 %.2fÅ 应在 %.2f–%.2fÅ)"
            % (float(peak), lo, hi), "verdict": v, "basis": ["exp:anchor-rdf-001"],
            "evidence": "径向分布首峰实测 %.3fÅ" % float(peak)}


def _rule_density(anchors, decisions, facts):
    a = anchors.get("anchor-density-001")
    den = facts.get("density_final")
    if not a or den is None:
        return None
    rng = a.get("range") or {}
    v = "sound" if rng.get("min", 0) <= float(den) <= rng.get("max", 9e9) else "critical"
    return {"id": "RA3", "question": "密度量级是否物理合理(%.3f g/cm³)" % float(den),
            "verdict": v, "basis": ["exp:anchor-density-001"],
            "evidence": "末段密度实测 %s" % den}


def _rule_repro(anchors, decisions, facts):
    a = anchors.get("anchor-repro-001")
    pairs = facts.get("repro_pairs") or []  # [{claimed, measured}]
    if not a or not pairs:
        return None
    worst, worst_dev = None, 0.0
    for p in pairs:
        base = abs(float(p.get("claimed", 0))) or 1.0
        dev = abs(float(p["measured"]) - float(p["claimed"])) / base
        if dev > worst_dev:
            worst, worst_dev = p, dev
    rec = (a.get("range") or {}).get("recommended_max", 0.005)
    v = "sound" if worst_dev <= rec else ("critical" if worst_dev > 10 * rec else "concern")
    return {"id": "RA4", "question": "复现偏差是否在实现噪声量级(最差 %.3f%% ≤ %.1f%%)"
            % (worst_dev * 100, rec * 100), "verdict": v,
            "basis": ["exp:anchor-repro-001"],
            "evidence": ("最差项 %s: 实测 %s vs 锚 %s" % (worst.get("field"),
                                                         worst.get("measured"), worst.get("claimed"))) if worst else "—"}


PLAN_RULES = [_rule_dt, _rule_acceptance]
RESULT_RULES = [_rule_temp, _rule_rdf, _rule_density, _rule_repro]


def _build(phase, case_id, decisions, facts, data_root, extra_rules=()):
    anchors = load_anchors(data_root)
    try:
        import deep_review
        dtext = " ".join(str((v or {}).get("value", ""))
                         for v in (decisions or {}).values()
                         if isinstance(v, dict))
        fam = deep_review.match_family(dtext) or "generic"
    except Exception:
        fam = "generic"
    rules = PLAN_RULES if phase == "plan" else RESULT_RULES
    findings = []
    for fn in list(rules) + list(extra_rules):
        try:
            r = fn(anchors, decisions, facts)
        except Exception as e:
            r = {"id": getattr(fn, "__name__", "?"), "question": "规则内部错误",
                 "verdict": "concern", "basis": [], "evidence": "%s: %s" % (type(e).__name__, e)}
        if r:
            # canary 锚不拦:critical 降 concern 并标注
            if r.get("verdict") == "critical" and r.get("blocking_only_if_active") is False:
                r = dict(r, verdict="concern", note="canary 锚只提示不拦")
            findings.append(r)
    critical_open = [f["id"] for f in findings if f.get("verdict") == "critical"]
    rec = {"schema_version": 1, "case_id": case_id, "phase": phase,
           "family": fam,
           "generated_at": _now_iso(), "findings": findings,
           "critical_open": critical_open,
           "overall": ("significant-risks" if critical_open else
                       ("concerns-identified" if any(f["verdict"] == "concern" for f in findings)
                        else "sound")),
           "proceed": "not-recommended" if critical_open else
                      ("caution" if any(f["verdict"] == "concern" for f in findings)
                       else "recommended"),
           "summary": "; ".join("%s=%s" % (f.get("question", f.get("id"))[:24], f["verdict"])
                                for f in findings)}
    ref = _write_record(case_id, rec, phase, data_root)
    return rec, ref


def assess_plan(case_id, data_root=None):
    import stage_state
    decisions = stage_state.read_decisions(case_id, data_root) or {}
    return _build("plan", case_id, decisions, {}, data_root)


def assess_results(case_id, facts, data_root=None):
    import stage_state
    decisions = stage_state.read_decisions(case_id, data_root) or {}
    return _build("results", case_id, decisions, facts or {}, data_root)


def read_assessment(case_id, rel_ref, data_root=None):
    import stage_state
    fp = os.path.join(stage_state.task_dir(case_id, data_root), rel_ref)
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
