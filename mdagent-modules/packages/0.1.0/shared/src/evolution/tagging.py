#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""三轴确定性打标(v1.6.0):atom/提案 → {topic, topic_candidates, method, ops, exam_ready}。

零 LLM、零写库——纯函数,词表在 expert_tags.json(受控,新增标签过人工闸)。
record_atom 写入时调用;backfill_tags.py 一次性回填历史原子。

三轴语义(2026-09-18 用户拍板):
  topic  课题轴:known_topics 归并的已认证课题,归宿=课题子agent;
  topic_candidates  case_id 正则直通的候选课题——未过人工闸,毕业资格审查不计数
         (红队ν:自由提取通道可被伪课题名灌水骗毕业);
  method 方法轴:受控词表(控温压/建模/力场/后处理…),归宿=skill;
  ops    运行轴:智能体自身运行问题(flow-gate/friction/安全…),归宿=skill 或子agent。
         纯 ASCII 词用词边界匹配(红队ν:"audit checklist" 这类正常文本误命中 ops/security)。

对抗加固(2026-09-18 三路红队后):
  - 入口截断(正则回溯炸弹,压测实测 10MB 纯 'a' 卡死分钟级);
  - refs 为 str 时包成 [str](红队μ:否则逐字符拆分静默丢溯源);
  - 词表加载校验:空关键词拒绝(红队ξ:塞 "" 即全命中);
  - verify_refs():refs 指向的 L0 文件存在性核验——exam_ready 只是语法检查,
    进考题留出集前必须再过 verify_refs(红队ν:refs 无校验→虚构修复可混进考场)。
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "expert_tags.json")

_REGISTRY = None
_WORD_KW = re.compile(r"^[a-z0-9_]+$")  # 纯词字符才配用词边界

_EMPTY_REG = {"method": {}, "ops": {}, "known_topics": {},
              "case_id_regex": "", "exam_ready_kinds": []}


def _validate_table(tbl):
    """词表校验:剔除非 str/空关键词(空串子串匹配=全命中,红队ξ)。"""
    out = {}
    for tag, kws in (tbl or {}).items():
        if not isinstance(tag, str) or not isinstance(kws, list):
            continue
        clean = [str(k).lower() for k in kws
                 if isinstance(k, str) and k.strip()]
        if clean:
            out[tag] = clean
    return out


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        try:
            with open(REGISTRY_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            _REGISTRY = {
                "method": _validate_table(raw.get("method")),
                "ops": _validate_table(raw.get("ops")),
                "known_topics": _validate_table(raw.get("known_topics")),
                "case_id_regex": str(raw.get("case_id_regex") or ""),
                "topic_stopwords": [str(s).lower() for s in
                                    raw.get("topic_stopwords", [])
                                    if isinstance(s, str) and s.strip()],
                "exam_ready_kinds": [str(k) for k in
                                     raw.get("exam_ready_kinds", [])],
            }
        except (OSError, ValueError):
            _REGISTRY = dict(_EMPTY_REG)
    return _REGISTRY


def reload_registry():
    """测试/词表更新后用:清缓存重读 expert_tags.json。"""
    global _REGISTRY
    _REGISTRY = None


def _kw_hit(kw, text_lower):
    """纯 ASCII 词字符关键词用词边界(防 audit checklist 式误命中);
    含连字符/中文/其他符号的词保持子串匹配。"""
    if _WORD_KW.match(kw):
        return re.search(r"\b%s\b" % re.escape(kw), text_lower) is not None
    return kw in text_lower


def _match_axes(text_lower, table, boundary=False):
    """boundary=True(ops 轴)时纯词关键词走词边界;method 轴保持子串
    (method 关键词常嵌在 case_id/复合词里,如 qeq_instability,边界会漏真命中)。"""
    if boundary:
        return sorted(tag for tag, kws in table.items()
                      if any(_kw_hit(kw, text_lower) for kw in kws))
    return sorted(tag for tag, kws in table.items()
                  if any(kw in text_lower for kw in kws))


def extract_topics(text):
    """课题提取 → (known, candidates)。
    known: known_topics 归并(已认证);candidates: case_id 正则直通(未过闸)。
    stopwords 挡 slug/环境变量/工具名;正则被改坏时两路皆空不拖垮其他轴。"""
    reg = _registry()
    low = text.lower()
    known = [t for t, kws in reg.get("known_topics", {}).items()
             if any(kw in low for kw in kws)]
    cands = []
    stops = tuple(reg.get("topic_stopwords", []))
    rx = reg.get("case_id_regex") or ""
    if rx:
        try:
            found = re.findall(rx, text)
        except re.error:
            found = []
        for m in found:
            ml = m.lower()
            if len(m) >= 8 and m not in cands and not any(s in ml for s in stops):
                cands.append(m)
    return known[:5], cands[:4]


def _norm_refs(refs):
    """refs 归一:str→[str];非可迭代→[];每项 str 截断。上限 8 条。"""
    if not refs:
        return []
    if isinstance(refs, str):
        return [refs[:200]]
    try:
        return [str(r)[:200] for r in list(refs)[:8]]
    except TypeError:
        return []


def tag_atom(kind, fact="", refs=None):
    """一条原子的三轴标签。kind/fact 决定 method/ops;topic 额外看 refs。
    输入截断再匹配:标签语义本就不依赖长文尾部,且挡病态长文回溯爆炸。"""
    reg = _registry()
    kind_s = str(kind or "")[:200]
    fact_s = str(fact or "")[:4000]
    refs_n = _norm_refs(refs)
    refs_s = " ".join(refs_n)[:1000]
    text = (kind_s + " " + fact_s).lower()
    known, cands = extract_topics(kind_s + " " + fact_s + " " + refs_s)
    axes = {
        "topic": known,
        "topic_candidates": cands,
        "method": _match_axes(text, reg.get("method", {})),
        "ops": _match_axes(text, reg.get("ops", {}), boundary=True),
    }
    axes["exam_ready"] = exam_ready({"kind": kind_s, "fact": fact_s,
                                     "refs": refs_n})
    return axes


def exam_ready(atom):
    """可考题化三要素(2026-09-18 用户原则:考题只用真实跑过有结局的):
    ① 有 refs 溯源到 L0 决策点快照;② kind 属于带结局的类别;
    ③ fact 含动作/结局信号(冒号分隔的结构化结局或成败词)。
    注意:这只是语法检查——进留出集前还须 verify_refs 存在性核验。"""
    refs = atom.get("refs")
    if not refs:
        return False
    reg = _registry()
    if str(atom.get("kind", "")) not in reg.get("exam_ready_kinds", []):
        return False
    fact = str(atom.get("fact", ""))
    return (":" in fact) or any(w in fact for w in
                                ("成功", "失败", "fail", "pass", "修复", "验证"))


def verify_refs(atom, base_dir):
    """refs 存在性核验(进考题留出集前的第二道):形如 'path:line' 或 'path'
    的 ref,其文件须真实存在于 base_dir 下(相对/绝对皆可)。非文件形态的
    ref(会话 id 等)不可核验→False。任一 ref 可核验即算通过(其余可能已轮转)。"""
    for r in _norm_refs(atom.get("refs")):
        path = r.split(":")[0].strip()
        if not path or "/" not in path:
            continue
        cand = path if os.path.isabs(path) else os.path.join(base_dir, path)
        try:
            if os.path.isfile(cand):
                return True
        except OSError:
            continue
    return False
