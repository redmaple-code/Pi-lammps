# -*- coding: utf-8 -*-
"""
stage_gate —— 证据门校验器（v2.2：E1-E5）
================================================================================
定位：只回答"这个仿真设计/这条计算结论凭什么"。零 LLM、零副作用、fail-closed
——任何内部异常 → BLOCK + E0:internal-error，绝不放行。

五条规则（所有者 2026-10-19 口径）：
  E1 设计有据  每项锁定设计决策须有达到风险门槛的分级证据；人工覆盖走冲突感知追问
  E2 计算有据  生产计算可回溯（决策↔run↔输入↔日志）；结论数值从本次产物重算
  E3 物理合理  物理评估/方法论维度 critical 未清零不得放行
  E4 方法学合规 输入文件与配置组合须符合方法学约束（独立复算磁盘真实文件）
  E5 结论完整  结论覆盖验收项；不达标必须给回退目标

不属于本系统（已从 v1 的 R1-R19 中移除）：schema/枚举校验、文件存在性、
工具调用审计、3-strike 计数、小规模先行、审阅流程本身。

闸绑定（每闸只跑本段规则，见证据系统规则阶段绑定矩阵 v0.1）：
  G-PLAN    E1 E3
  G-BUILD   E4
  G-CONFIG  E4
  G-EXEC    E2
  G-ANALYZE E2 E3 E5
"""
import json
import os

try:
    import stage_state
    import evidence_grade
    import surface
except ImportError:  # 被测试以模块路径导入时
    from . import stage_state  # noqa: F401
    from . import evidence_grade  # noqa: F401
    from . import surface  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")

DECISIONS = ("PASS", "REVISE", "BLOCKED")
BLOCK_RESULT, HOLD_RESULT, GO_RESULT = "BLOCK", "HOLD", "GO"
# 恒 BLOCK 的拒因前缀
ALWAYS_BLOCK = ("E0:", "E3:critical", "E4:violation")

# 规则 → 生效闸（数据化的单一出处）
GATE_RULES = {
    "G-PLAN": ("E1", "E3", "E6"),
    "G-BUILD": ("E4", "E6"),
    "G-CONFIG": ("E4", "E6"),
    "G-EXEC": ("E2", "E6"),
    "G-ANALYZE": ("E2", "E3", "E5", "E6"),
}

# 每个闸"本段必须存在"的工件类别（缺失即按缺项拒，不静默跳过）
# 说明：评估类缺位由 E3 判定，输入类由 E4 判定，这里只补设计基线。
REQUIRED_ARTIFACTS = {
    "G-PLAN": (("decisions", "锁定决策", "E1:no-design-decisions"),),
}


class GateResult(dict):
    """{gate, result: GO|HOLD|BLOCK, reasons: [...], downgraded: [...],
        evidence: [...], conflicts: [...], rule_trace: {...}}"""

    def __bool__(self):  # `if result:` = 放行，语义与 fail-closed 对齐
        return self.get("result") == GO_RESULT


def _strict_loads(text):
    return json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError("strict-json:%s" % c)))


def _read_json_file(fp):
    if not os.path.isfile(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return _strict_loads(f.read())


def _load_jsonl(fp):
    rows = []
    if os.path.isfile(fp):
        with open(fp, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
    return rows


def _index_task_files(case_id, sub, data_root):
    """任务工件目录 → {相对名: 记录}。目录不存在 → {}。"""
    if not case_id:
        return {}
    d = os.path.join(stage_state.task_dir(case_id, data_root), sub)
    out = {}
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        doc = _read_json_file(os.path.join(d, name))
        if isinstance(doc, dict):
            out[name] = doc
    return out


def _index_dir_records(root, key_field="case_id"):
    out = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        doc = _read_json_file(os.path.join(root, name))
        if isinstance(doc, dict):
            out[str(doc.get(key_field) or name[:-5])] = doc
    return out


def _load_atoms(dr):
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_atoms(dr)
    return [r for r in _load_jsonl(os.path.join(dr, "memory", "atoms.jsonl"))
            if isinstance(r, dict)]


def exp_status_index(data_root=None):
    """review_experience 条目状态索引：id → {status, kind, severity}。
    种子包 + 运行时注入库；坏包隔离不打崩校验器。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.exp_status_index(data_root)
    idx = {}
    roots = [os.path.join(MCP_DIR, "review_experience")]
    env = os.environ.get("REVIEW_EXPERIENCE_DIR", "").strip()
    if env:
        roots.append(env)
    dr = stage_state._data_root(data_root)
    runtime = os.path.join(dr, "memory", "review_experience")
    if runtime not in roots:
        roots.append(runtime)
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            try:
                pack = _read_json_file(os.path.join(root, name))
            except Exception:
                continue
            if not isinstance(pack, dict):
                continue
            for e in pack.get("entries") or []:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                idx[str(e["id"])] = {
                    "status": e.get("status", "active"),
                    "kind": e.get("kind", ""),
                    "severity": e.get("severity_hint", "soft"),
                    "pack": pack.get("pack", name),
                    "entry": e,
                }
    return idx


def load_methodology_entries(data_root=None):
    """方法学约束条目（kind=methodology，带 constraints）——E4 的判据。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_methodology_entries(data_root)
    out = []
    for eid, info in exp_status_index(data_root).items():
        e = info.get("entry") or {}
        if e.get("kind") != "methodology":
            continue
        if info.get("status") == "revoked":
            continue
        if not isinstance(e.get("constraints"), dict):
            continue
        out.append(dict(e, _status=info.get("status")))
    return out


def load_confirmations(case_id, data_root=None):
    """人工确认记录（追加式，不可变）：tasks/<case>/confirmations/*.json。"""
    d = os.path.join(stage_state.task_dir(case_id, data_root), "confirmations")
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        doc = _read_json_file(os.path.join(d, name))
        if isinstance(doc, dict):
            out.append(doc)
    return out


# ---------- 上下文构建 ----------
def build_context(case_id, data_root=None, facts=None, engine=None,
                  family=None, record=None):
    """从磁盘组装校验上下文；任何一节缺失降级为空，不打崩校验器。"""
    ctx = {"case_id": case_id, "data_root": data_root, "engine": engine,
           "family": family, "state": None, "decisions": {},
           "atoms": [], "ledger": [], "exp_index": {}, "facts": facts or {},
           "assessments": {}, "factchecks": {}, "cases": {},
           "confirmations": [], "toolcalls": [], "assessment_findings": {}}
    try:
        ctx["state"] = stage_state.read_state(case_id, data_root)
    except Exception:
        ctx["state"] = {"_corrupt": True}
    dr = stage_state._data_root(data_root)
    ctx["data_root"] = dr
    try:
        ctx["decisions"] = stage_state.read_decisions(case_id, data_root) or {}
    except Exception:
        pass
    ctx["atoms"] = _load_atoms(dr)
    ctx["ledger"] = _load_jsonl(os.path.join(dr, "memory", "LEDGER.jsonl"))
    ctx["exp_index"] = exp_status_index(data_root)
    ctx["assessments"] = _index_task_files(case_id, "assessments", data_root)
    ctx["factchecks"] = _index_task_files(case_id, "factchecks", data_root)
    ctx["cases"] = _index_dir_records(os.path.join(dr, "memory", "cases"))
    ctx["confirmations"] = load_confirmations(case_id, data_root)
    ctx["toolcalls"] = _load_jsonl(os.path.join(dr, "events", "toolcalls.jsonl"))
    ctx["assessment_findings"] = _findings_by_slot(ctx["assessments"])
    if not ctx["family"]:
        ctx["family"] = _infer_family(ctx)
    # 输入文件是否真实存在于工作目录（E4 独立复算的前置事实）
    files = [str(f) for f in (record or {}).get("input_files") or []]
    if files:
        wd = os.path.join(dr, "work", str(case_id or ""))
        ctx["input_files_present"] = all(
            os.path.isfile(os.path.join(wd, f)) for f in files)
    return ctx


def _findings_by_slot(assessments):
    """评估记录 → {决策槽: [findings]}（供冲突检测引用"物理评估已判 critical"）。"""
    out = {}
    for doc in (assessments or {}).values():
        for f in doc.get("findings") or []:
            if not isinstance(f, dict):
                continue
            for slot in f.get("slots") or []:
                out.setdefault(str(slot), []).append(f)
    return out


def _infer_family(ctx):
    """从决策文本推断实验族（供证据适用性判断）；推断不出返回空串。"""
    text = " ".join(str((v or {}).get("value", ""))
                    for v in (ctx.get("decisions") or {}).values()
                    if isinstance(v, dict))
    t = text.lower()
    if any(k in t for k in ("弹性常数", "elastic", "应变", "strain")):
        return "elastic-constants"
    if any(k in t for k in ("液体", "liquid", "rdf", "径向分布")):
        return "liquid-md"
    if any(k in t for k in ("基态", "ground", "晶格常数", "lattice constant")):
        return "ground-state"
    return ""


# ---------- 证据收集 ----------
def _collect_refs(record, slot=None):
    """决策槽的证据引用（支持字符串与 {ref,...} 两种形态）。

    注意：给了 slot 就只取该槽自己的证据，绝不回落到工件全局 evidence_refs——
    否则任意一条全局引用都能替所有决策"凑门槛"（会让 E1 形同虚设）。
    """
    src = slot.get("evidence") if slot is not None else record.get("evidence_refs")
    out = []
    for e in src or []:
        if isinstance(e, dict):
            out.append(dict(e))
        else:
            out.append({"ref": str(e)})
    return out


def _assess_entries(ctx, phase):
    """本任务指定相位的评估记录（按 gate 对应 phase 过滤）。"""
    out = []
    for name, doc in (ctx.get("assessments") or {}).items():
        if str(doc.get("phase")) == phase:
            out.append((name, doc))
    return out


def _critical(doc):
    return [str(x) for x in (doc.get("critical_open") or [])]


# ---------- E1 设计有据 ----------
def _e1(record, ctx, reasons, trace):
    """每项锁定设计决策：达到风险门槛的分级证据；人工覆盖走冲突感知追问。"""
    locked = (ctx.get("state") or {}).get("locked_decisions") or []
    decisions = ctx.get("decisions") or {}
    risk = (ctx.get("state") or {}).get("risk_level") or record.get("risk_level") or "medium"
    # 未启用决策锁定 → 无设计可查（零回归）
    if not locked:
        trace["E1"] = "无锁定决策（依据管理未启用）"
        return
    conflicts = []
    for sid in locked:
        slot = decisions.get(sid) or {}
        basis = slot.get("basis") if isinstance(slot.get("basis"), dict) else {}
        refs = _collect_refs(record, slot)
        # 依据本身也是证据（论文/案例/经验/人工记录），一并分级
        if basis.get("ref"):
            refs.append({"ref": basis.get("ref"), "value": basis.get("value")})
        graded = evidence_grade.grade_all(refs, ctx)
        trace.setdefault("E1", {})[sid] = evidence_grade.summarize(graded) or ["无证据"]

        # 人工覆盖：二次确认 → 最高权限；否则需理由 + 冲突感知追问
        if basis.get("kind") == "user":
            if evidence_grade.is_second_confirmed(slot, ctx):
                trace["E1"][sid].append("人工二次确认→最高权限人工经验")
                continue
            cs0 = evidence_grade.detect_conflicts(sid, slot, graded, ctx)
            fp0 = evidence_grade.fingerprint(sid, slot, cs0)
            asked0 = _asked_before(ctx, sid)
            answered0 = _answers_for(ctx, sid, fp0)
            if answered0 and all(a.get("resolved") for a in answered0):
                trace["E1"][sid].append("冲突已追问并获人工答复（记录异议）")
                continue
            if evidence_grade.needs_reason(slot):
                # 同一冲突已问过还没解决 → 升级人工闸；不再机械地要一次理由
                if any(a.get("fingerprint") == fp0 for a in asked0):
                    reasons.append("E1:override-unresolved:%s" % sid)
                    trace["E1"][sid].append("同一冲突已追问过且未解决→升级人工闸")
                else:
                    reasons.append("E1:override-reason-missing:%s" % sid)
                conflicts.append(evidence_grade.conflict_pack(
                    sid, slot, cs0, ctx, asked0))
                continue
            cs = evidence_grade.detect_conflicts(sid, slot, graded, ctx)
            if cs:
                fp = evidence_grade.fingerprint(sid, slot, cs)
                answered = _answers_for(ctx, sid, fp)
                asked = _asked_before(ctx, sid)
                if answered and all(a.get("resolved") for a in answered):
                    trace["E1"][sid].append("冲突已追问并获人工答复（记录异议）")
                    continue
                if any(a.get("fingerprint") == fp for a in asked):
                    reasons.append("E1:override-unresolved:%s" % sid)
                    trace["E1"][sid].append("同一冲突已追问过且未解决→升级人工闸")
                else:
                    reasons.append("E1:conflict-unresolved:%s" % sid)
                conflicts.append(evidence_grade.conflict_pack(sid, slot, cs, ctx, asked))
                continue
            trace["E1"][sid].append("人工覆盖（有理由，无冲突）")
            continue

        # 常规门槛
        ok, msg = evidence_grade.meets_threshold(risk, "design", graded)
        trace["E1"][sid].append(msg)
        if basis and basis.get("kind") not in evidence_grade.VALID_BASIS_KINDS:
            reasons.append("E1:basis-invalid:%s" % sid)
        elif not ok:
            reasons.append("E1:evidence-insufficient:%s" % sid)
        # 估算型依据必须被评估背书（计划段存在评估记录即可；有显式槽位映射更精确）
        if basis.get("kind") == "physics_estimate":
            if not _estimate_assessed(ctx, sid):
                reasons.append("E1:estimate-unassessed:%s" % sid)
    if conflicts:
        trace["E1_conflicts"] = conflicts


def _estimate_assessed(ctx, slot_id):
    """估算型依据是否被评估背书：该槽位有显式 finding，或计划段已有评估记录。"""
    if (ctx.get("assessment_findings") or {}).get(slot_id):
        return True
    return bool(_assess_entries(ctx, "plan"))


def _asked_before(ctx, slot_id):
    """该决策槽已经问过什么（确认日志里按 fingerprint 去重）。"""
    out = []
    for c in ctx.get("confirmations") or []:
        if str(c.get("slot")) != str(slot_id):
            continue
        if c.get("question") or c.get("fingerprint"):
            out.append({"fingerprint": c.get("fingerprint", ""),
                        "question": c.get("question", "")})
    return out


def _answers_for(ctx, slot_id, fp):
    """同一冲突指纹已获得的人工答复（resolved=true 才算解决）。"""
    return [c for c in (ctx.get("confirmations") or [])
            if str(c.get("slot")) == str(slot_id)
            and str(c.get("fingerprint", "")) == str(fp)
            and str(c.get("answer", "")).strip()]


# ---------- E2 计算有据 ----------
def _e2(record, ctx, reasons, trace):
    """计算可回溯 + 结论数值从本次产物重算（含新鲜性）。"""
    gate = record.get("gate")
    ledger = [r for r in (ctx.get("ledger") or [])
              if str(r.get("case_id", "")) == str(ctx.get("case_id"))]
    if gate == "G-EXEC":
        # 受理前：设计决策 → 输入文件 → 执行请求 的链路必须成立
        locked = (ctx.get("state") or {}).get("locked_decisions") or []
        if locked and not (ctx.get("decisions") or {}):
            reasons.append("E2:design-untraceable")
            trace["E2"] = "无锁定决策记录"
            return
        files = [str(f) for f in record.get("input_files") or []]
        if not files:
            reasons.append("E2:input-untraceable")
            trace["E2"] = "执行工件未声明输入文件"
            return
        trace["E2"] = "链路: %d 项决策 → %d 个输入文件" % (len(locked), len(files))
        return
    # G-ANALYZE：每个数值主张都要能从本次产物重算
    acc = record.get("acceptance_checks") or []
    if not acc:
        reasons.append("E2:no-acceptance-claims")
        trace["E2"] = "无验收项"
        return
    numeric = [a for a in acc if isinstance(a, dict) and
               isinstance(a.get("claimed"), (int, float))]
    if not numeric:
        trace["E2"] = "无数值主张（%d 项验收）" % len(acc)
        return
    missing, failed, stale = [], [], []
    idx = ctx.get("factchecks") or {}
    for a in numeric:
        fc = str(a.get("factcheck") or "")
        if not fc.startswith("factcheck:"):
            missing.append(str(a.get("item", "?"))[:30])
            continue
        graded = evidence_grade.resolve(fc, ctx)
        if not graded.get("verified"):
            failed.append("%s(%s)" % (str(a.get("item", "?"))[:24], graded.get("note")))
            continue
        if not graded.get("fresh"):
            stale.append(str(a.get("item", "?"))[:30])
            continue
        # 记录内容复核：verdict=PASS 但 measured/claimed/tolerance 自相矛盾的
        # 篡改记录不得放行（对抗探针 PR-08 抓出的缺口）
        key = fc.split(":", 1)[1].split("/")[-1]
        doc = (idx.get(key) or idx.get(fc.split(":", 1)[1])
               or idx.get(key if key.endswith(".json") else key + ".json") or {})
        try:
            cl, me, tol = (float(doc.get("claimed")), float(doc.get("measured")),
                           doc.get("tolerance"))
            tol = float(tol) if tol is not None else 0.0
            if abs(cl - me) > tol:
                failed.append("%s(记录自相矛盾:|%.6g-%.6g|>%.3g)" % (
                    str(a.get("item", "?"))[:20], cl, me, tol))
        except (TypeError, ValueError):
            pass  # 缺字段的旧记录由 verdict/新鲜性通道把关
    if missing:
        reasons.append("E2:claim-unevidenced:%s" % ",".join(missing))
    if failed:
        reasons.append("E2:claim-failed:%s" % ",".join(failed))
    if stale:
        reasons.append("E2:stale-evidence:%s" % ",".join(stale))
    trace["E2"] = "数值主张 %d 条：%d 通过、%d 缺对账、%d 不通过、%d 过期" % (
        len(numeric), len(numeric) - len(missing) - len(failed) - len(stale),
        len(missing), len(failed), len(stale))


# ---------- E3 物理合理 ----------
def _e3(record, ctx, reasons, trace):
    """物理评估/方法论维度：critical 未清零不得放行；评估缺位按缺项拒。"""
    gate = record.get("gate")
    phase = {"G-PLAN": "plan", "G-ANALYZE": "results"}.get(gate)
    if phase is None:
        return
    docs = _assess_entries(ctx, phase)
    if not docs:
        reasons.append("E3:assessment-missing:%s" % phase)
        trace["E3"] = "缺 %s 相位评估记录" % phase
        return
    risk = (ctx.get("state") or {}).get("risk_level") or "medium"
    fam_notes = []
    for name, doc in docs:
        fam = str(doc.get("family") or "")
        if fam == "generic":
            if risk == "high":
                reasons.append("E3:family-uncovered-high-risk")
            fam_notes.append("%s=generic(无族内方法论维度)" % name)
    crit = []
    for name, doc in docs:
        crit.extend(_critical(doc))
    if crit:
        reasons.append("E3:critical:%s" % ",".join(sorted(set(crit))))
        trace["E3"] = "critical 未清零: %s" % ",".join(sorted(set(crit)))
        return
    trace["E3"] = "%d 份 %s 评估，critical 清零%s" % (
        len(docs), phase, ("；" + "；".join(fam_notes)) if fam_notes else "")


# ---------- E4 方法学合规 ----------
def _e4(record, ctx, reasons, trace):
    """输入文件与配置组合的方法学约束——门禁直接解析磁盘真实文件独立复算。"""
    entries = load_methodology_entries(ctx.get("data_root"))
    if not entries:
        trace["E4"] = "无方法学约束条目（知识包为空）"
        return
    files = [str(f) for f in record.get("input_files") or []]
    if not files:
        reasons.append("E4:no-input-declared")
        trace["E4"] = "未声明输入文件，无法独立复算"
        return
    import config_check
    workdir = os.path.join(ctx.get("data_root") or ".",
                           "work", str(ctx.get("case_id") or ""))
    paths = [os.path.join(workdir, f) for f in files]
    dtext = " ".join(str((v or {}).get("value", ""))
                     for v in (ctx.get("decisions") or {}).values()
                     if isinstance(v, dict))
    res = config_check.check_case_inputs(entries, paths, dtext)
    if res is None:
        reasons.append("E4:cannot-verify:input-unreadable")
        trace["E4"] = "输入文件不可读（fail-closed）"
        return
    hits = []
    for eid, r in res.items():
        if r.get("cannot_verify"):
            reasons.append("E4:cannot-verify:%s" % eid)
            hits.append("%s=无法核验" % eid)
        elif not r.get("passed", True):
            note = (r.get("violations") or [{}])[0].get("note", "")[:60]
            reasons.append("E4:violation:%s:%s" % (eid, note))
            hits.append("%s=违规" % eid)
    trace["E4"] = "约束 %d 条：%s" % (
        len(res), "；".join(hits) if hits else "全部通过")


# ---------- E5 结论完整 ----------
def _e5(record, ctx, reasons, trace):
    """结论覆盖锁定验收项；不达标必须有回退目标。"""
    crit = _acceptance_criteria(ctx)
    acc = record.get("acceptance_checks") or []
    items = {str(a.get("item", a) if isinstance(a, dict) else a) for a in acc}
    missing = [c for c in crit if not any(c in a or a in c for a in items)]
    if missing:
        reasons.append("E5:acceptance-not-covered:%s" % "|".join(
            m[:30] for m in missing[:3]))
    bad = [a for a in acc if isinstance(a, dict) and a.get("passed") is False]
    if bad and not record.get("rollback_target"):
        reasons.append("E5:no-rollback-on-fail")
    trace["E5"] = "验收 %d 项（锁定标准 %d 条，缺 %d）；不达标 %d 项" % (
        len(acc), len(crit), len(missing), len(bad))


def _acceptance_criteria(ctx):
    """锁定决策中 id 含"验收"的槽位，取其 value 列表。"""
    out = []
    for sid, slot in (ctx.get("decisions") or {}).items():
        if "验收" in sid and isinstance(slot, dict):
            v = slot.get("value")
            if isinstance(v, list):
                out.extend(str(x) for x in v)
            elif v:
                out.append(str(v))
    return out


# ---------- E6 核查面完整 ----------
def _e6(record, ctx, reasons, trace):
    """先枚举再取证：本阶段的核查面必须逐项有据、或 N/A 且写明理由。

    缺项/待取证/证据不够级 → 拒绝，并产出问题包（questions），
    供协调者逐项追问或去找证据；同一项已问未决 → 升级不重复。
    """
    gate = record.get("gate")
    phase = {"G-PLAN": "design", "G-BUILD": "build", "G-CONFIG": "build",
             "G-EXEC": "exec", "G-ANALYZE": "analyze"}.get(gate)
    if phase is None:
        return
    res = surface.evaluate(ctx.get("case_id"), phase, ctx)
    reasons.extend(res["reasons"])
    trace["E6"] = res["summary"]
    if res.get("questions"):
        trace["E6_questions"] = res["questions"]


# ---------- 顶层校验 ----------
def validate_decision_record(record, ctx):
    try:
        return _validate(record, ctx)
    except Exception as e:  # fail-closed：内部错误恒 BLOCK
        try:
            gate = str((record or {}).get("gate", "?"))
        except Exception:
            gate = "?"
        return GateResult(gate=gate, result=BLOCK_RESULT,
                          reasons=["E0:internal-error:%s" % type(e).__name__],
                          downgraded=[], evidence=[], conflicts=[],
                          questions=[], rule_trace={})


def _validate(record, ctx):
    reasons, trace = [], {}
    if not isinstance(record, dict):
        return GateResult(gate="?", result=BLOCK_RESULT,
                          reasons=["E0:record-not-dict"], downgraded=[],
                          evidence=[], conflicts=[], questions=[], rule_trace={})
    gate = record.get("gate")
    decision = record.get("decision")
    if decision not in DECISIONS or not gate:
        # 工件自身不完整属调用方问题：拒绝推进（不是 schema 校验，是无法判定）
        return GateResult(gate=gate or "?", result=BLOCK_RESULT,
                          reasons=["E0:record-unusable"],
                          downgraded=[], evidence=[], conflicts=[],
                          questions=[], rule_trace={})
    if decision == "REVISE":
        # 整改工件：只记录，不推进
        return GateResult(gate=gate, result=HOLD_RESULT, reasons=[],
                          downgraded=[], evidence=[], conflicts=[],
                          questions=[], rule_trace={})
    if decision == "BLOCKED":
        return GateResult(gate=gate, result=BLOCK_RESULT, reasons=["E0:blocked"],
                          downgraded=[], evidence=[], conflicts=[],
                          questions=[], rule_trace={})

    # 本段必须存在的工件（缺项即拒，不再静默跳过）
    for sub, label, code in REQUIRED_ARTIFACTS.get(gate, ()):
        if not _artifact_present(sub, ctx):
            reasons.append("%s(%s)" % (code, label))

    rules = GATE_RULES.get(gate)
    if rules is None:  # 未绑定的闸：不启用证据校验（自定义段集）
        trace["_gate"] = "未绑定证据规则：%s" % gate
        rules = ()
    runners = {"E1": _e1, "E2": _e2, "E3": _e3, "E4": _e4, "E5": _e5,
               "E6": _e6}
    for r in rules:
        runners[r](record, ctx, reasons, trace)

    # 证据清单（每条引用的分级结果，供报告与复核）
    graded = evidence_grade.grade_all(record.get("evidence_refs"), ctx)
    evidence = [{"ref": g["ref"], "grade": g["grade"],
                 "countable": g["countable"], "note": g["note"]}
                for g in graded]
    downgraded = [{"ref": g["ref"], "note": g["note"]}
                  for g in graded if not g["countable"]]
    conflicts = trace.pop("E1_conflicts", []) if isinstance(trace, dict) else []
    questions = trace.pop("E6_questions", []) if isinstance(trace, dict) else []

    # 证据类拒因一律阻断推进：E1-E6 都是"拿不出证据/证据不合格"，
    # 不是"再补充一下"（HOLD 只留给 REVISE 语义）
    blocking = [x for x in reasons if x.startswith(
        ("E0:", "E1:", "E2:", "E3:", "E4:", "E5:", "E6:"))]
    result = BLOCK_RESULT if blocking else (HOLD_RESULT if reasons else GO_RESULT)
    return GateResult(gate=gate, result=result, reasons=reasons,
                      downgraded=downgraded, evidence=evidence,
                      conflicts=conflicts, questions=questions,
                      rule_trace=trace)


def _artifact_present(sub, ctx):
    if sub == "decisions":
        return bool(ctx.get("decisions"))
    if sub == "assessments":
        return bool(ctx.get("assessments"))
    if sub == "inputs":
        return bool(ctx.get("input_files_present"))
    if sub == "state":
        return bool(ctx.get("state")) and not (ctx.get("state") or {}).get("_corrupt")
    return True


def gate_and_log(case_id, record, data_root=None, facts=None, engine=None,
                 manual_gate=False, family=None):
    """校验 + 落闸口账（gate-log）一步完成。只追加，不改状态机。"""
    ctx = build_context(case_id, data_root, facts=facts, engine=engine,
                        family=family, record=record)
    res = validate_decision_record(record, ctx)
    stage_state.append_gate_log(case_id, {
        "case_id": case_id, "gate": res.get("gate"),
        "decision_path": record.get("_path"),
        "result": res["result"], "reasons": res["reasons"],
        "downgraded": res["downgraded"],
        "evidence": res.get("evidence") or [],
        "conflicts": res.get("conflicts") or [],
        "questions": res.get("questions") or [],
        "rule_trace": res.get("rule_trace") or {},
        "manual_gate": bool(manual_gate),
    }, data_root)
    return res
