# -*- coding: utf-8 -*-
"""
deep_review —— 方法论深度分析引擎(证据系统 v2 深度层)
================================================================================
定位: 回答"这套仿真方法本身对不对、准不准"。不是查一两个参数,而是对匹配实验族
**遍历全部方法论分析维度**(维度=知识条目,数据驱动),每个维度给出
问题/依据/证据/结论四要素,产出深度分析记录。

双层架构(设计 §5.3):
- 本引擎=确定性深度分析(零 LLM,维度来自知识库,引擎只认通用 check 类型);
- design_review.py AI 审阅层(生产环境有 LLM 凭证时叠加自由分析,不预设问题)。

纪律: 纯 stdlib;维度条目=数据(人工闸维护);fail-closed(缺素材=concern 不放行)。
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_dimensions(data_root=None):
    """载入方法论分析维度: family -> [entries]。种子+运行时注入库。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_dimensions(data_root)
    import stage_gate
    roots = [os.path.join(MCP_DIR, "review_experience")]
    env = os.environ.get("REVIEW_EXPERIENCE_DIR", "").strip()
    if env:
        roots.append(env)
    dr = stage_gate.stage_state._data_root(data_root)
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
            for e in (pack.get("entries") or []) if isinstance(pack, dict) else []:
                if isinstance(e, dict) and e.get("kind") == "methodology-dimension" \
                        and e.get("status") == "active":
                    fam = str(e.get("family") or "*")
                    out.setdefault(fam, []).append(e)
    return out


def match_family(decisions_text):
    """从决策文本匹配实验族(通用关键词→族映射表,数据可扩)。"""
    FAMILY_MAP = [
        ("elastic-constants", r"弹性模量|弹性常数|elastic|C11|C12|C44|杨氏|bulk modulus|剪切模量"),
        ("liquid-md", r"液体|液相|liquid|熔化|melt|RDF|径向分布|密度统计"),
        ("ground-state", r"基态|ground.state|平衡晶格|晶格常数|cohesive|结合能|0K"),
    ]
    for fam, pat in FAMILY_MAP:
        if re.search(pat, str(decisions_text), re.I):
            return fam
    return None


# ---------- 通用检查器(引擎只认这些类型;域判据在条目 params 里) ----------
def _ck_text_matches(dim, decisions_text, facts, input_text):
    p = dim.get("check", {}).get("params", {})
    hit = bool(re.search(p.get("decision_regex", "."), str(decisions_text), re.I))
    if hit:
        note = p.get("note_match", "声明匹配")
        return "sound", note
    return "concern", p.get("note_miss", "声明中未找到预期模式")


def _ck_input_regex_all(dim, decisions_text, facts, input_text):
    pats = dim.get("check", {}).get("params", {}).get("patterns", [])
    if not pats:
        return "na", "条目未声明模式"
    if input_text is None:
        return "concern", "输入文件不可读——无法核验(深度分析需要真实输入)"
    missing = [p for p in pats if not re.search(p, input_text, re.I | re.S)]
    if not missing:
        return "sound", "输入文件包含全部必需要素(%d 项核验通过)" % len(pats)
    return "critical", dim["check"]["params"].get(
        "note_fail", "输入缺少: %s" % ", ".join(missing))


def _ck_numeric_min(dim, decisions_text, facts, input_text):
    p = dim.get("check", {}).get("params", {})
    v = facts.get(p.get("fact_key", ""))
    if v is None:
        return "concern", "事实缺 %s——无法核验(需先度量)" % p.get("fact_key")
    if float(v) >= float(p.get("min", 0)):
        return "sound", "%s=%s ≥ 下限 %s" % (p.get("fact_key"), v, p.get("min"))
    return "concern", p.get("note_fail", "%s=%s 低于下限" % (p.get("fact_key"), v))


def _ck_two_value_consistency(dim, decisions_text, facts, input_text):
    p = dim.get("check", {}).get("params", {})
    a, b = facts.get(p.get("key_a")), facts.get(p.get("key_b"))
    if a is None or b is None:
        return "concern", "缺 %s/%s——需两组独立幅值结果(事实缺: %s)" % (
            p.get("key_a"), p.get("key_b"),
            [k for k in (p.get("key_a"), p.get("key_b")) if facts.get(k) is None])
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return "concern", "幅值结果非数值"
    tol = float(p.get("rel_tol", 0.1))
    dev = abs(a - b) / max(abs(a), abs(b), 1e-12)
    if dev <= tol:
        return "sound", "%s=%s 与 %s=%s 相对偏差 %.1f%% ≤ %.0f%%——%s" % (
            p.get("key_a"), a, p.get("key_b"), b, dev * 100, tol * 100,
            p.get("note_pass", "一致"))
    return "critical", p.get("note_fail", "偏差 %.1f%%" % (dev * 100))


def _ck_reduced_timestep(dim, decisions_text, facts, input_text):
    """步长维度：自己从决策文本读 dt、从锚读特征时间并换算 dt*，
    不再依赖外部写入的 dt_star_verdict（该键全仓无生产写点，曾是死路径）。"""
    import assessment
    m = re.search(r"dt[=＝:]\s*([0-9.]+)\s*fs", str(decisions_text or ""))
    if not m:
        return "na", "未声明步长（非动态模拟或步长由输入文件给定）"
    dt_fs = float(m.group(1))
    anchors = assessment.load_anchors()
    a = anchors.get("anchor-md-001")
    if not a or a.get("status") not in ("active", "canary"):
        return "na", "无可用步长锚（知识库缺 anchor-md-001）"
    t_unit = (a.get("reduction") or {}).get("t_unit_ps")
    if not t_unit:
        return "na", "锚缺少特征时间换算参数"
    dt_star = dt_fs / 1000.0 / float(t_unit)
    rng = a.get("range") or {}
    rec, crit = rng.get("recommended_max"), rng.get("critical_max")
    canary = a.get("status") == "canary"
    note = "dt=%gfs → dt*=%.4f（推荐≤%s，失稳线>%s）" % (dt_fs, dt_star, rec, crit)
    if crit is not None and dt_star > float(crit):
        return ("concern" if canary else "critical"), note + "：超失稳线"
    if rec is not None and dt_star > float(rec):
        return "concern", note + "：超推荐上限"
    return "sound", note


CHECKERS = {
    "text_matches": _ck_text_matches,
    "input_regex_all_present": _ck_input_regex_all,
    "numeric_min": _ck_numeric_min,
    "facts_two_value_consistency": _ck_two_value_consistency,
    "reduced_timestep": _ck_reduced_timestep,
}


def analyze(case_id, phase, decisions, facts, data_root=None, input_text=None):
    """深度分析方法论:匹配实验族→遍历该族+通配(*)全部维度→逐维度判定。
    返回 (record, 相对引用)。"""
    import stage_state
    dtext = " ".join(str((v or {}).get("value", "")) for v in (decisions or {}).values()
                     if isinstance(v, dict))
    fam = match_family(dtext)
    dims = load_dimensions(data_root)
    if fam:
        entries = dims.get(fam, []) + dims.get("*", [])
    else:
        entries = dims.get("*", [])
    findings = []
    for e in entries:
        ck = (e.get("check") or {})
        fn = CHECKERS.get(ck.get("type"))
        if fn is None:
            findings.append({"id": e["id"], "question": e.get("question", ""),
                             "verdict": "concern", "basis": ["exp:%s" % e["id"]],
                             "evidence": "未知检查类型 %s(引擎不识别)" % ck.get("type"),
                             "why": e.get("why", "")})
            continue
        try:
            verdict, note = fn(e, dtext, facts or {}, input_text)
        except Exception as ex:
            verdict, note = "concern", "检查器异常: %s" % ex
        findings.append({
            "id": e["id"], "question": e.get("question", ""),
            "verdict": verdict, "basis": ["exp:%s" % e["id"]],
            "evidence": note, "why": e.get("why", ""),
            "rationale": e.get("rationale", "")})
    critical_open = [f["id"] for f in findings if f["verdict"] == "critical"]
    rec = {
        "schema_version": 2, "case_id": case_id, "phase": phase,
        "mode": "deep-methodology", "family": fam,
        "generated_at": _now_iso(),
        "dimension_count": len(findings),
        "findings": findings, "critical_open": critical_open,
        "overall": ("significant-risks" if critical_open else
                    ("concerns-identified" if any(f["verdict"] == "concern" for f in findings)
                     else "sound")),
        "proceed": "not-recommended" if critical_open else
                   ("caution" if any(f["verdict"] == "concern" for f in findings)
                    else "recommended"),
        "summary": "实验族=%s;%d 个方法论维度:%s" % (
            fam or "(未匹配,仅通用)",
            len(findings),
            "; ".join("%s=%s" % (f["id"].replace("mdev-", ""), f["verdict"])
                      for f in findings)),
    }
    d = os.path.join(stage_state.task_dir(case_id, data_root), "assessments")
    os.makedirs(d, exist_ok=True)
    name = "%s-deep-%s.json" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                                 phase)
    fp = os.path.join(d, name)
    with open(fp, "x", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return rec, os.path.join("assessments", name).replace("\\", "/")
