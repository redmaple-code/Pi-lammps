# -*- coding: utf-8 -*-
"""
evidence_grade —— 证据分级模型（证据系统 v2.2 核心）
================================================================================
定位：判断"一条证据有多强、是否适用、是否新鲜、能否计入风险门槛"。
纯 stdlib、零 LLM、零副作用、fail-closed。

六级来源（所有者 2026-10-19 定）：
  A 人工确认 / B 论文标准手册 / C 本任务实测 / D 过往案例 / E 过往经验教训 / F 物理估算

有效等级 = 来源等级 − 未核验（不计入） − 不适用（降为参考） − 不新鲜（无效）

设计文档：.project-to-act/docs/证据系统重构_五规则与证据分级_v2.2.md
"""
import hashlib
import json
import os
import re

GRADES = ("A", "B", "C", "D", "E", "F")
RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1, "F": 0}

# 风险门槛：设计决策 / 结果结论
THRESHOLDS = {
    "high": {"design": {"min": 4, "note": "≥1 条 A 或 B（A 单独即可，高风险允许纯人工）"},
             "result": {"min": 3, "require_factcheck": True, "note": "≥1 条 C 且数值可重算"}},
    "medium": {"design": {"min": 2, "note": "≥1 条 B/C/D"},
               "result": {"min": 3, "note": "≥1 条 C"}},
    "low": {"design": {"min": 1, "note": "≥1 条已核验证据"},
            "result": {"min": 3, "note": "≥1 条 C"}},
}

VALID_BASIS_KINDS = ("user", "literature", "prior_case", "physics_estimate",
                     "measured", "lesson")
KIND_GRADE = {"user": "A", "literature": "B", "prior_case": "D",
              "physics_estimate": "F", "measured": "C", "lesson": "E"}


# ---------------------------------------------------------------- 来源判定
def source_grade(ref):
    """ref 族 → 来源等级。无法归类的（裸路径/纯文本）按 F 处理（仅提示）。"""
    r = str(ref or "").strip()
    if r.startswith("user:"):
        return "A"
    if r.startswith("paper:") or r.startswith("std:"):
        return "B"
    if r.startswith(("$", "LEDGER:", "factcheck:", "tool:")):
        return "C"
    if r.startswith("case:"):
        return "D"
    if r.startswith(("atom:", "exp:")):
        return "E"
    return "F"


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 逐条解析
def resolve(ref, ctx, entry=None):
    """核验一条证据，返回分级条目。

    {ref, grade, verified, applicable, fresh, countable, value, note, source}
    - verified=False → 不得计入（伪造/已撤销/查无记录）
    - applicable=False → 降为参考，不参与门槛与冲突裁决
    - fresh=False → 结果类证据无效
    """
    r = str(ref or "").strip()
    g = source_grade(r)
    e = entry if isinstance(entry, dict) else {}
    out = {"ref": r, "grade": g, "source": g, "verified": False,
           "applicable": True, "fresh": True, "countable": False,
           "value": e.get("value"), "note": "", "soft_only": False}

    if not r:
        out["note"] = "empty"
        return out

    if g == "A":
        rows = (ctx.get("confirmations") or [])
        hit = [x for x in rows if str(x.get("id", "")) == r[5:]]
        if hit:
            out.update(verified=True, note="human-confirmation")
            out["value"] = e.get("value", hit[0].get("value"))
            scope = str(hit[0].get("scope") or "")
            if scope and ctx.get("case_id") and scope != ctx.get("case_id"):
                out["applicable"] = False
                out["note"] = "human-input-scope-mismatch"
        else:
            out["note"] = "user-record-absent"
        out["countable"] = out["verified"] and out["applicable"]
        return out

    if g == "B":
        # 论文/标准：语法可解析即收；带 meta 时核对适用性（体系/引擎）
        body = r.split(":", 1)[1]
        ok = len(body.strip()) >= 2
        out.update(verified=ok, note="paper-parseable" if ok else "paper-malformed")
        meta = e.get("meta") if isinstance(e.get("meta"), dict) else {}
        want_family = (ctx.get("family") or "").strip().lower()
        want_engine = (ctx.get("engine") or "").strip().lower()
        if meta.get("family") and want_family and \
                str(meta["family"]).lower() != want_family:
            out["applicable"] = False
            out["note"] = "paper-family-mismatch"
        if meta.get("engine") and want_engine and \
                str(meta["engine"]).lower() != want_engine:
            out["applicable"] = False
            out["note"] = "paper-engine-mismatch"
        out["countable"] = out["verified"] and out["applicable"]
        return out

    if g == "C":
        if r.startswith("$"):
            key = r[1:]
            facts = ctx.get("facts") or {}
            out.update(verified=key in facts,
                       note="fact" if key in facts else "fact-key-absent")
            if key in facts:
                out["value"] = facts[key] if e.get("value") is None else e.get("value")
        elif r.startswith("LEDGER:"):
            parts = r.split(":", 2)[1:]
            lid, expect = parts[0], (parts[1] if len(parts) > 1 else None)
            hit = [x for x in (ctx.get("ledger") or [])
                   if str(x.get("lid", x.get("id", ""))) == lid]
            if not hit:
                out["note"] = "ledger-absent"
            elif expect and str(hit[0].get("result", "")) != expect:
                out["note"] = "ledger-result-mismatch"
            else:
                out.update(verified=True, note="ledger")
                out["value"] = e.get("value", hit[0].get("value"))
                out["fresh"] = _ledger_fresh(hit[0], ctx)
        elif r.startswith("factcheck:"):
            fcid = r.split(":", 1)[1]
            idx = ctx.get("factchecks") or {}
            key = fcid.split("/")[-1]          # 兼容 factchecks/<name>.json 形式
            doc = (idx.get(key) or idx.get(fcid)
                   or idx.get(key if key.endswith(".json") else key + ".json"))
            if not doc:
                out["note"] = "factcheck-absent"
            elif doc.get("verdict") != "PASS":
                out["note"] = "factcheck-failed"
            else:
                out.update(verified=True, note="factcheck-pass")
                out["value"] = e.get("value", doc.get("measured"))
                out["fresh"] = _factcheck_fresh(doc)
        else:  # tool:
            parts = r.split(":", 2)[1:]
            name = parts[0] if parts else ""
            ts = parts[1] if len(parts) > 1 else None
            hit = [x for x in (ctx.get("toolcalls") or [])
                   if str(x.get("name", "")) == name and
                   (not ts or str(x.get("ts", "")) == ts)]
            out.update(verified=bool(hit),
                       note="toolcall" if hit else "toolcall-absent")
            out["soft_only"] = True  # 工具调用只证执行，不支撑设计依据
        out["countable"] = out["verified"] and out["applicable"] and \
            out["fresh"] and not out["soft_only"]
        return out

    if g == "D":
        cid = r.split(":", 1)[1]
        rec = (ctx.get("cases") or {}).get(cid)
        if not rec:
            out["note"] = "case-absent"
            return out
        out.update(verified=True, note="case-record")
        out["value"] = e.get("value", rec.get("result_summary"))
        # 适用性：案例的 family/engine 与本任务不符 → 降为参考
        cf = str(rec.get("family") or "").lower()
        ce = str(rec.get("engine") or "").lower()
        want_f = str(ctx.get("family") or "").lower()
        want_e = str(ctx.get("engine") or "").lower()
        if cf and want_f and cf != want_f:
            out["applicable"] = False
            out["note"] = "case-family-mismatch"
        if ce and want_e and ce != want_e:
            out["applicable"] = False
            out["note"] = "case-engine-mismatch"
        out["countable"] = out["applicable"]
        return out

    if g == "E":
        if r.startswith("atom:"):
            aid = r[5:]
            hit = [a for a in (ctx.get("atoms") or [])
                   if str(a.get("id", "")) == aid]
            if not hit:
                out["note"] = "atom-absent"
            elif hit[0].get("retracted") or hit[0].get("superseded_by"):
                out["note"] = "atom-retracted"
            else:
                out.update(verified=True, note="atom-active")
        else:
            key = r[4:]
            info = (ctx.get("exp_index") or {}).get(key)
            if not info:
                out["note"] = "exp-absent"
            elif info.get("status") == "revoked":
                out["note"] = "exp-revoked"
            else:
                out.update(verified=True,
                           note="exp-canary" if info.get("status") == "canary" else "exp-active")
                out["soft_only"] = info.get("status") == "canary"
        out["countable"] = out["verified"] and not out["soft_only"]
        return out

    # F：物理估算/常识/裸文本——只作提示，永不计入门槛
    out.update(verified=False, note="estimate-or-unclassified", countable=False)
    return out


def _ledger_fresh(row, ctx):
    """台账行必须不早于任务进入 EXEC 的时间（旧账不撑新结论）。"""
    since = (ctx.get("state") or {}).get("stage_since") or {}
    base = since.get("EXEC") or since.get("CONFIG")
    if not base:
        return True
    try:
        return str(row.get("ts", "")) >= str(base)
    except Exception:
        return True


def _factcheck_fresh(doc):
    src = ((doc.get("source") or {}).get("path"))
    if not src:
        return True
    try:
        import datetime as _dt
        m = os.path.getmtime(src)
        t = _dt.datetime.fromisoformat(
            str(doc.get("ts", "2000-01-01T00:00:00Z")).replace("Z", "+00:00")).timestamp()
        return m <= t + 1
    except (OSError, ValueError):
        return True


# ---------------------------------------------------------------- 门槛判定
def grade_all(entries, ctx):
    """批量分级。entries 支持字符串或 {ref, ...}。返回条目列表（保持顺序）。"""
    out = []
    for e in entries or []:
        if isinstance(e, dict):
            out.append(resolve(e.get("ref"), ctx, e))
        else:
            out.append(resolve(e, ctx))
    return out


def best_grade(graded):
    ranks = [RANK[g["grade"]] for g in graded if g.get("countable")]
    return max(ranks) if ranks else -1


def meets_threshold(risk, kind, graded):
    """返回 (ok, 说明)。kind ∈ design|result。"""
    rule = (THRESHOLDS.get(str(risk or "medium")) or THRESHOLDS["medium"]).get(kind) or {}
    need = rule.get("min", 1)
    got = best_grade(graded)
    ok = got >= need
    label = {5: "A", 4: "B", 3: "C", 2: "D", 1: "E", 0: "F"}
    msg = "最高有效等级=%s（门槛 %s）" % (
        label.get(got, "无可用证据"), label.get(need, need))
    if rule.get("require_factcheck") and kind == "result":
        has_fc = any(g["ref"].startswith("factcheck:") and g.get("countable")
                     for g in graded)
        if not has_fc:
            ok = False
            msg += "；结果结论缺可重算的数值对账（factcheck）"
    return ok, msg


# ---------------------------------------------------------------- 冲突检测
def detect_conflicts(slot_id, slot, graded, ctx):
    """人工输入与当前证据的冲突。返回冲突列表（结构化，供追问使用）。

    冲突类型：
      value        人工声明的数值与其他证据的数值不一致（超容差）
      risk         人工覆盖时，物理评估已对同一决策给出 critical
      applicability 其他证据不适用（降为参考，不构成需追问的冲突）
    """
    conflicts = []
    basis = slot.get("basis") if isinstance(slot.get("basis"), dict) else {}
    if basis.get("kind") != "user":
        return conflicts
    hv = _as_float(basis.get("value"))
    for g in graded:
        if g["ref"] == str(basis.get("ref") or ""):
            continue
        if not g.get("verified") or g["grade"] in ("A",):
            continue
        ev = _as_float(g.get("value"))
        if hv is not None and ev is not None:
            tol = 0.05 * max(abs(hv), abs(ev), 1e-12)
            if abs(hv - ev) > tol:
                conflicts.append({
                    "type": "value", "slot": slot_id, "ref": g["ref"],
                    "grade": g["grade"],
                    "human_value": hv, "evidence_value": ev,
                    "summary": "人工值 %g 与 %s 级证据值 %g 不一致（容差 %.3g）"
                               % (hv, g["grade"], ev, tol)})
    for f in ((ctx.get("assessment_findings") or {}).get(slot_id) or []):
        if f.get("verdict") == "critical":
            conflicts.append({
                "type": "risk", "slot": slot_id, "ref": f.get("id", ""),
                "grade": "C", "summary": str(f.get("question") or "")[:120]})
    return conflicts


def fingerprint(slot_id, slot, conflicts):
    """同一输入 + 同一冲突集 → 同一指纹（防止机械重复追问）。"""
    basis = slot.get("basis") if isinstance(slot.get("basis"), dict) else {}
    payload = json.dumps({
        "slot": slot_id,
        "value": basis.get("value", slot.get("value")),
        "ref": basis.get("ref", ""),
        "conflicts": sorted([(c.get("type"), c.get("ref"), c.get("summary"))
                             for c in conflicts]),
    }, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def conflict_pack(slot_id, slot, conflicts, ctx, asked_before=None):
    """门禁产出给协调者的冲突包：用户说了什么、和谁冲突、风险与后果、问过什么。"""
    basis = slot.get("basis") if isinstance(slot.get("basis"), dict) else {}
    return {
        "slot": slot_id,
        "fingerprint": fingerprint(slot_id, slot, conflicts),
        "human_input": {"ref": basis.get("ref", ""),
                        "value": basis.get("value", slot.get("value")),
                        "scope": basis.get("scope", ""),
                        "stated_reason": basis.get("reason", "")},
        "conflicts": conflicts,
        "risk": (ctx.get("state") or {}).get("risk_level", "medium"),
        "asked_before": list(asked_before or []),
    }


def needs_reason(slot):
    """人工覆盖是否需要追问：有冲突且未写理由。"""
    basis = slot.get("basis") if isinstance(slot.get("basis"), dict) else {}
    return basis.get("kind") == "user" and not str(basis.get("reason") or "").strip()


def is_second_confirmed(slot, ctx):
    """同一输入被人工二次确认 → 最高权限人工经验（不再受冲突追问约束）。

    识别：确认记录显式 confirmed_twice；或存在另一条记录以 confirms 指向该记录 id
    （"对同一条输入再次确认"），防止把两次不同输入误判为二次确认。
    """
    basis = slot.get("basis") if isinstance(slot.get("basis"), dict) else {}
    if basis.get("kind") != "user":
        return False
    ref = str(basis.get("ref") or "")
    if not ref.startswith("user:"):
        return False
    rid = ref[5:]
    rows = ctx.get("confirmations") or []
    first = [r for r in rows if str(r.get("id", "")) == rid]
    if not first:
        return False
    if first[0].get("confirmed_twice"):
        return True
    return any(str(r.get("confirms", "")) == rid for r in rows)


def summarize(graded):
    """人读摘要：每条证据一行（等级/是否计入/原因）。"""
    out = []
    for g in graded:
        out.append("%s→%s%s%s" % (
            g["ref"], g["grade"],
            "" if g["countable"] else "（不计入）",
            ":" + g["note"] if g.get("note") else ""))
    return out
