# -*- coding: utf-8 -*-
"""
surface —— 核查面（confirmation surface）：先枚举，再取证，再判定
================================================================================
定位：解决"写了才查"的根问题。每个仿真任务在推进前，必须对固定的核查面
逐项给出「有据（evidenced）/ 不适用（na+理由）/ 待取证（open）」；
缺项、N/A 无理由、证据不够级 → 门禁拒绝，并产出**问题包**供协调者追问。

数据：mcp/review_experience/confirmation_surface.json（12 域 29 项，按阶段挂载）
工件：tasks/<case>/surface.json（逐项状态 + 证据 + 是否已沉淀）
沉淀：surface.sediment() 走既有 record_atom 通道写入 atoms（不新增知识存储）

纯 stdlib、零 LLM、fail-closed。
"""
import json
import os
import re
import time

try:
    import stage_state
    import evidence_grade
except ImportError:
    from . import stage_state  # noqa: F401
    from . import evidence_grade  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
PACK_NAME = "confirmation_surface.json"


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(fp):
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_write(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, fp)


def load_items(data_root=None):
    """载入核查面条目：种子包 + 运行时注入库（同 id 运行时覆盖种子）。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_items(data_root)
    out = {}
    roots = [os.path.join(MCP_DIR, "review_experience")]
    env = os.environ.get("REVIEW_EXPERIENCE_DIR", "").strip()
    if env:
        roots.append(env)
    dr = stage_state._data_root(data_root)
    roots.append(os.path.join(dr, "memory", "review_experience"))
    for root in roots:
        fp = os.path.join(root, PACK_NAME)
        doc = _read_json(fp)
        if not isinstance(doc, dict):
            continue
        for e in doc.get("entries") or []:
            if isinstance(e, dict) and e.get("id"):
                out[str(e["id"])] = dict(e, _pack=doc.get("pack", PACK_NAME))
    return out


def surface_path(case_id, data_root=None):
    return os.path.join(stage_state.task_dir(case_id, data_root), "surface.json")


def read_coverage(case_id, data_root=None):
    doc = _read_json(surface_path(case_id, data_root)) or {}
    return doc.get("items") or {}


def record_coverage(case_id, item_id, status, evidence=None, reason="",
                    answer="", data_root=None):
    """记录一项核查面的处置。status ∈ evidenced|na|open。原子更新（tmp+replace）。

    answer：这一项到底确认了什么（结论/数值/理由）。evidenced 时必填，
    数值型条目（answer_kind=numeric）必须含数字——否则 E6 判为"只挂了引用没回答"。
    """
    status = str(status or "").strip()
    if status not in ("evidenced", "na", "open"):
        raise ValueError("bad-status:%s" % status)
    if str(item_id) not in load_items(data_root):
        raise ValueError("unknown-item:%s" % item_id)
    fp = surface_path(case_id, data_root)
    doc = _read_json(fp) or {"case_id": case_id, "items": {}}
    doc.setdefault("items", {})
    doc["items"][str(item_id)] = {
        "status": status,
        "evidence": [str(x) for x in (evidence or [])],
        "reason": str(reason or "")[:400],
        "answer": str(answer or "")[:600],
        "updated_at": _now_iso(),
        "sedimented": bool((doc["items"].get(str(item_id)) or {}).get("sedimented")),
        "atom_id": (doc["items"].get(str(item_id)) or {}).get("atom_id", ""),
    }
    doc["updated_at"] = _now_iso()
    _atomic_write(fp, doc)
    return doc["items"][str(item_id)]


def _asked_before(case_id, item_id, data_root=None):
    """门禁账里此前是否问过该项（防机械重复）。"""
    rows = stage_state.read_gate_log(case_id, 200, data_root)
    for r in rows:
        for q in r.get("questions") or []:
            if str(q.get("item_id")) == str(item_id):
                return True
    return False


def _min_rank(min_grade, risk):
    g = (min_grade or {}).get(risk) or (min_grade or {}).get("medium") or "C"
    return evidence_grade.RANK.get(g, 3), g


def evaluate(case_id, phase, ctx):
    """评估某阶段的核查面完整性。

    返回 {ok, reasons, questions, summary}；reasons 以 E6: 开头。
    """
    items = [i for i in load_items(ctx.get("data_root")).values()
             if str(i.get("phase")) == str(phase)]
    items.sort(key=lambda x: str(x.get("id")))
    cov = ctx.get("surface_coverage")
    if not isinstance(cov, dict):
        cov = read_coverage(case_id, ctx.get("data_root"))
    risk = (ctx.get("state") or {}).get("risk_level") or "medium"
    reasons, questions = [], []
    counts = {"evidenced": 0, "na": 0, "open": 0, "missing": 0}
    for it in items:
        iid = str(it["id"])
        c = cov.get(iid) or {}
        st = c.get("status")
        q = {
            "item_id": iid, "domain": it.get("domain", ""),
            "question": it.get("question", ""), "why": it.get("why", ""),
            "min_grade": (it.get("min_grade") or {}).get(risk, "C"),
            "suggested_sources": it.get("suggested_sources") or [],
            "asked_before": _asked_before(case_id, iid, ctx.get("data_root")),
        }
        if not st:
            counts["missing"] += 1
            # 已问过仍未补 → 升级；不重复问同一项
            if q["asked_before"]:
                reasons.append("E6:surface-unresolved:%s" % iid)
            else:
                reasons.append("E6:surface-missing:%s" % iid)
            questions.append(q)
            continue
        if st == "open":
            counts["open"] += 1
            reasons.append("E6:surface-%s:%s" % (
                "unresolved" if q["asked_before"] else "open", iid))
            questions.append(q)
            continue
        if st == "na":
            if not it.get("na_allowed"):
                reasons.append("E6:surface-na-not-allowed:%s" % iid)
                questions.append(q)
            elif not str(c.get("reason") or "").strip():
                reasons.append("E6:surface-na-reason:%s" % iid)
                questions.append(q)
            elif it.get("na_reason_pattern") and not re.search(
                    str(it["na_reason_pattern"]), str(c.get("reason") or "")):
                reasons.append("E6:surface-na-reason-unjustified:%s" % iid)
                questions.append(q)
            else:
                counts["na"] += 1
            continue
        if st == "evidenced":
            graded = evidence_grade.grade_all(c.get("evidence"), ctx)
            need, need_label = _min_rank(it.get("min_grade"), risk)
            got = evidence_grade.best_grade(graded)
            answer = str(c.get("answer") or "").strip()
            if got < need:
                counts["open"] += 1
                reasons.append("E6:surface-evidence-insufficient:%s(need %s)"
                               % (iid, need_label))
                questions.append(q)
            elif not answer:
                counts["open"] += 1
                reasons.append("E6:surface-answer-missing:%s" % iid)
                questions.append(q)
            elif it.get("answer_kind") == "numeric" and not re.search(r"[-+]?\d", answer):
                counts["open"] += 1
                reasons.append("E6:surface-answer-unquantified:%s" % iid)
                questions.append(q)
            else:
                counts["evidenced"] += 1
            continue
        reasons.append("E6:surface-bad-status:%s" % iid)
        questions.append(q)
    return {"ok": not reasons, "reasons": reasons, "questions": questions,
            "summary": "核查面 %s：%d 项（有据 %d / N/A %d / 待取证 %d / 缺项 %d）"
                       % (phase, len(items), counts["evidenced"], counts["na"],
                          counts["open"], counts["missing"]),
            "counts": counts}


def status(case_id, data_root=None):
    """全部条目的当前状态（供 MCP 工具/协调者查看）。"""
    items = load_items(data_root)
    cov = read_coverage(case_id, data_root)
    out = []
    for iid in sorted(items):
        it = items[iid]
        c = cov.get(iid) or {}
        out.append({"item_id": iid, "domain": it.get("domain"),
                    "phase": it.get("phase"), "question": it.get("question"),
                    "status": c.get("status") or "missing",
                    "evidence": c.get("evidence") or [],
                    "reason": c.get("reason") or "",
                    "answer": c.get("answer") or "",
                    "answer_kind": it.get("answer_kind"),
                    "sedimented": bool(c.get("sedimented")),
                    "atom_id": c.get("atom_id") or ""})
    return out


def sediment(case_id, item_id, summary, refs=None, data_root=None):
    """把已取得的证据沉淀为 atoms（走既有 record_atom 通道，零新增存储）。

    返回 atom id 或 None（record_atom 拒绝时）。
    """
    import memory
    refs = [str(x) for x in (refs or [])]
    atom = memory.record_atom(
        "surface-evidence",
        "核查面 %s：%s（证据：%s）" % (item_id, summary, ", ".join(refs[:3]) or "无"),
        refs=refs)
    aid = ""
    if isinstance(atom, dict):
        aid = str(atom.get("id", ""))
    elif isinstance(atom, str):
        aid = atom
    # 标记已沉淀
    fp = surface_path(case_id, data_root)
    doc = _read_json(fp) or {"case_id": case_id, "items": {}}
    doc.setdefault("items", {}).setdefault(item_id, {})
    doc["items"][item_id]["sedimented"] = True
    doc["items"][item_id]["atom_id"] = aid
    doc["updated_at"] = _now_iso()
    _atomic_write(fp, doc)
    return aid
