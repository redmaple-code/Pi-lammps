# -*- coding: utf-8 -*-
"""
design_review —— AI 审阅流水线:代码度量、AI 判断、经验供智
================================================================================
定位(2026-10-04 架构定稿):仿真方案的完整审阅与补全。判断由审阅 LLM 基于
可注入经验库(review_experience/)做出——经验注入落盘即生效,不改代码;
本模块的代码只做四件事(与 model_research 同款纪律):

    FACTS      physics_gate.extract_facts/structural_scan(纯度量与存在性)
    EXPERIENCE 热加载经验库,匹配注入(坏包隔离)
    BRIEF      组装审阅简报(方案+事实+结构缺口+经验条目)
    REVIEW     审阅 LLM 按量规输出 findings/completions/verdict(AI 判断)
    ARBITRATE  代码仲裁:schema 校验/cited_fields 路径核验(幻觉剔除)/
               evidence_refs 核验(伪造→unverified 且 hard 降 soft)/
               结构缺口确定性合并(缺必填字段=hard,不依赖 AI)/补全建议合并

纪律:永不抛(REVIEW_ERROR/DEGRADED 如实返回,fail-closed 不放行);
canary 经验只能作 soft 依据(新经验降落伞);引用以简报实际内容为准。
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import model_research  # 复用供应商链/_post_anthropic/_parse_blocks(不复制)
import physics_gate

REVIEW_TIMEOUT_SEC = 300
# 审阅输出是结构化八层 findings,预算须给足;GLM 系思考链长(实测 4000 tokens
# 全被 reasoning 吃掉、正文为空 → DEGRADED),_post_anthropic 的 2.5x 放大后
# 仍需 ≥10000 正文预算——2026-10-14 五案例实跑发现(端点 GLM-5.3 行为)。
REVIEW_MAX_TOKENS = 6000
MAX_ENTRIES_IN_BRIEF = 24  # 简报注入上限(按匹配分排序取 top-N);核验面=展示面(引用不在简报=伪造)。
                           # 2026-10-14 五案例实跑:60 条全量注入使简报 17K,长审核请求在
                           # 端点易 504 超时——按相关性注入 top-24 兼顾覆盖与稳定。

REVIEW_RUBRIC = """你是分子模拟方案审阅官。对下面这份仿真方案(intent)做【完整审阅与补全】——目标不是'仿真能否复刻实验数值',而是'在仿真合理的条件下,这份设计能否对实验给出可辩护的解释'。按八个维度逐层审:

【A 目标与解释角色】仿真在论文里承担什么角色(复现/机理解释/预测/筛选)?结论范围声明了吗?若声称对实验的解释:该声称与模型能力匹配吗(声明-能力匹配)?无解释角色又无独立价值论证=为了仿真而仿真。
【B 材料模型保真】结构类型/晶格/固溶方式/缺陷/取向/交联度/组分:与实验材料状态(含表征证据)对应吗?声明与构建产物一致吗(看【度量事实】的 composition/nn_dist 指纹)?结构对目标物理够用吗?
【C 抽象映射保真】实验条件→仿真条件逐条:有公式/依据/假设吗?假设置信吗(如 Hertz 换算进入塑性即失效)?简化假设的损失项声明了吗?不可比量是否显式标注?
【D 物理可表达性与尺度】引擎/力场/尺度能否表达目标物理(断键/长程静电/扩散时间尺度/微结构统计)?时间-空间尺度桥接正当吗?
【E 设定自洽】步长/系综链/电荷/单位/元素覆盖/温压程序等参数自洽(结合事实与经验)。
【F 数值与有限尺度】PBC 伪影、有限尺寸效应、盒/截断自洽、体系尺寸 vs 微结构代表性。
【G 统计与收敛充分性】收敛判据、重复/样本量、误差棒/噪声地板方案(设计期=声明级)。
【H 对照协议与可比性】每个目标性质归四档(直接对应/指示量代理/趋势序关系/不可比)了吗?代理量有逻辑关系说明吗?参照值/出处/容差/噪声地板/欠约束项的裁决实验齐吗?结论所依赖的实验量都有档位吗?
【I 完整性(横切)】对照【结构缺口】与【完整性素材】补充缺口与补法。

判定三档:hard(判定为错,有依据)/ uncertain(判不了:信息缺/依据不足)/ soft(成立但属风险或惯例偏离)。
依据纪律:结合【度量事实】(代码算好的比值/结构指纹,勿自行心算推翻)与【经验条目】(引用须回指条目 id 或事实名)。

严格输出 JSON(不要输出任何其他文字):
{"findings":[{"layer":"A|B|C|D|E|F|G|H|I","aspect":"...","severity":"hard|uncertain|soft","title":"一句话",
  "detail":"具体到数值与字段","rationale":"机理/依据",
  "cited_fields":["intent里真实存在的字段路径"],"evidence_refs":["exp-... 条目id 或 $事实名"],
  "remediation":["整改选项"]}],
 "completions":[{"layer":"A|...|I","aspect":"缺的方面","why":"为什么需要","proposal":"典型补法/取值"}],
 "verdict":"一句话总评(按层小结)"}
rules:
- hard/uncertain 必须有依据:引用简报中出现过的经验条目 id 或 $事实名;经验没覆盖但存疑的用 uncertain 并说清缺什么;
- 覆盖有依据的全部问题,3-10 条;completions 0-6 条;
- 标注 experimental/canary 的经验条目只能支撑 soft;
- cited_fields 必须是 intent JSON 里真实存在的字段路径,不得编造;evidence_refs 不得引用简报中没出现过的 id/事实名——宁缺勿造;
- 若提供【输入文件】:它们是审阅对象的一部分——逐项核对脚本与方案声明的一致性(单位/步长/系综与控温命令/最小化先后/变形或加载设置/截断),不一致=hard,脚本自身缺关键命令按所在层报告;cited_fields 仍写 intent 字段路径,脚本行号与原文引证写进 detail。

【intent 方案】
%s

【度量事实(代码实测,含衍生比值与结构指纹)】
%s

【结构缺口(完整性清单存在性扫描,缺即事实)】
%s

【经验条目(本项目积累,引用须回指 id)】
%s

【完整性素材(补全建议参考)】
%s

【输入文件(in 脚本——与方案声明逐项做一致性核对)】
%s"""


# ---------- 经验库加载(热加载,mtime 缓存,坏包隔离) ----------
_LOAD_CACHE = {}


def _experience_dirs():
    """加载顺序=优先级:运行时注入库在前(项目特定/人工注入优先于基线种子,
    也保证 top-N 截断时新注入条目不被种子库挤掉),种子库垫后。"""
    dirs = []
    env = os.environ.get("REVIEW_EXPERIENCE_DIR", "").strip()
    if env:
        dirs.append(env)
    else:
        try:
            import paths
            dirs.append(os.path.join(paths.memory_dir(make=False), "review_experience"))
        except Exception:
            pass
    dirs.append(os.path.join(HERE, "review_experience"))
    return dirs


def _read_packs(dirs):
    """读全部包(带 mtime 缓存)。返回 (packs, quarantined, cache_key)。"""
    sig = []
    for d in dirs:
        try:
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json") and not fn.startswith("_"):
                    p = os.path.join(d, fn)
                    st = os.stat(p)
                    sig.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    key = json.dumps(sig)
    if key in _LOAD_CACHE:
        return _LOAD_CACHE[key]
    packs, quarantined = [], []
    for p, _mt, _sz in sig:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError("schema_version != 1")
            packs.append(data)
        except Exception as ex:
            quarantined.append({"file": os.path.basename(p), "error": str(ex)[:120]})
    _LOAD_CACHE.clear()  # 只留最新一份(防缓存随注入文件数膨胀)
    _LOAD_CACHE[key] = (packs, quarantined, key)
    return packs, quarantined, key


_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_STATUSES = ("active", "canary", "revoked")
# kind 词表(2026-10-17 收编 methodology:methodology_seed 条目带 statement 且可设计期
# 判定——如各向同性 NPT 与变形互斥,在 intent.ensemble_sequence 层即可审,不必等运行期)。
_KINDS = ("boundary", "lesson", "convention", "capability", "completeness",
          "methodology")


def _entry_ok(e):
    return (isinstance(e, dict) and _ID_RE.match(str(e.get("id") or ""))
            and str(e.get("statement") or "").strip()
            and str(e.get("status") or "active") in _STATUSES
            and (not e.get("kind") or e.get("kind") in _KINDS))


def _match_score(entry, intent_blob, et):
    """条目与本 intent 的相关度(纯检索启发,非判断):conditions 各键值命中
    intent 文本/实验类型即加分——family 子串命中实验类型权重最高。"""
    cond = entry.get("conditions") or {}
    s = 0
    for k, v in cond.items():
        v = str(v or "").strip()
        if not v or v == "*":
            continue
        if k == "family":
            if et and v.lower() in et:
                s += 4
        elif v.lower() in intent_blob:
            s += 1
    return s


def load_experience(intent):
    """加载+校验+匹配。返回 {entries, checklists, quarantined, packs}。"""
    intent = intent if isinstance(intent, dict) else {}
    blob = json.dumps(intent, ensure_ascii=False).lower()
    ed = intent.get("experiment_design")
    et = str((ed if isinstance(ed, dict) else {}).get("experiment_type") or "").lower()
    packs, quarantined, _key = _read_packs(_experience_dirs())
    entries, checklists, pack_meta, q_rules = [], [], [], 0
    for pk in packs:
        # 共享目录纪律(2026-10-17):review_experience/ 同时存放证据系统(evidence-v2)
        # 的包(锚点/审阅维度,自家 schema 无 statement);包级 "consumer" 字段声明归属,
        # 外来包整包跳过——既不注入也不计入"非法条目"(此前 16 条跨消费方条目被误计)。
        if str(pk.get("consumer") or "").strip() not in ("", "design-review"):
            continue
        n_ok = 0
        for e in pk.get("entries") or []:
            if not _entry_ok(e):
                q_rules += 1
                continue
            if e.get("status") == "revoked":
                n_ok += 1
                continue  # 保留溯源,不注入
            e2 = dict(e)
            e2["_pack"] = pk.get("pack")
            e2["_score"] = _match_score(e2, blob, et)
            entries.append(e2)
            n_ok += 1
        if pk.get("kind") == "completeness":
            fam = str(pk.get("family") or "*")
            req = [str(x) for x in (pk.get("required_fields") or []) if str(x).strip()]
            checklists.append({"family": fam, "required_fields": req,
                                "guidance": pk.get("guidance") or []})
        pack_meta.append({"pack": pk.get("pack"), "entries_ok": n_ok})
    entries.sort(key=lambda e: -e["_score"])
    return {"entries": entries, "checklists": checklists,
            "quarantined": quarantined + [{"file": "(rules)", "error": "非法条目数: %d" % q_rules}]
            if q_rules else quarantined,
            "packs": pack_meta}


# ---------- 简报组装 ----------
def _guidance_text(checklists):
    out = []
    for cl in checklists:
        for g in cl.get("guidance") or []:
            out.append("- [%s] %s | 补法: %s" % (g.get("aspect"), g.get("why"), g.get("proposal")))
    return "\n".join(out[:24]) or "(无)"


def _entries_text(entries):
    lines = []
    for e in entries[:MAX_ENTRIES_IN_BRIEF]:
        tag = " (experimental/canary)" if e.get("status") == "canary" else ""
        cond = ";".join("%s=%s" % kv for kv in (e.get("conditions") or {}).items() if kv[1])
        lines.append("- %s[%s|%s]%s %s | 依据: %s%s" % (
            e["id"], e.get("kind", ""), e.get("severity_hint", "soft"), tag,
            e["statement"], e.get("rationale") or "",
            (" | 条件: " + cond) if cond else ""))
    return "\n".join(lines) or "(无)"


def build_brief(intent, facts, gaps, exp, grade="exploratory", grade_reasons=None,
                input_texts=None):
    gap_text = "\n".join("- %s (清单族:%s)" % (g["path"], g["family"]) for g in gaps) or "(无)"
    grade_text = "任务形态判定: %s%s" % (
        grade, ("(依据: %s)" % "; ".join(grade_reasons)) if grade_reasons else "")
    # 输入文件进审阅面(2026-10-23):in 脚本与方案声明的一致性核对;限 2 文件/各 3000 字符
    if input_texts:
        in_text = "\n\n".join(
            "--- %s ---\n%s" % (t.get("name"), str(t.get("content") or "")[:3000])
            for t in input_texts[:2])
    else:
        in_text = "(无——本方案尚无 in 脚本,按方案级审阅)"
    return REVIEW_RUBRIC % (
        grade_text + "\n\n" + json.dumps(intent, ensure_ascii=False, indent=1)[:6000],
        json.dumps(facts, ensure_ascii=False, indent=1, default=str),
        gap_text, _entries_text(exp["entries"]), _guidance_text(exp["checklists"]),
        in_text)


# ---------- 审阅 LLM(带重试) ----------
def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _review_llm(brief_text):
    """调审阅 LLM。返回 (parsed|None, tokens, provider, error, infra_fail)。
    infra_fail=基础设施失败(无凭证/断网)→REVIEW_ERROR;输出不可解析→DEGRADED。永不抛。"""
    provider, last_err, infra = "", None, False
    for strict in ("", "\n\n再次强调:只输出 JSON,第一个字符必须是 {。"):
        try:
            payload, provider = model_research._post_anthropic({
                "max_tokens": REVIEW_MAX_TOKENS,
                "messages": [{"role": "user", "content": brief_text + strict}],
            }, REVIEW_TIMEOUT_SEC)
            text, _src, tokens = model_research._parse_blocks(payload)
            parsed = _extract_json(text)
            if isinstance(parsed, dict):
                return parsed, tokens, provider, None, False
            last_err, infra = "输出非合法 JSON(两次)", False
        except Exception as ex:
            last_err, infra = "%s: %s" % (type(ex).__name__, ex), True
    return None, 0, provider, last_err, infra


# ---------- 仲裁(代码纪律:核验/降级/合并) ----------
def _path_exists(obj, path):
    cur = obj
    for k in str(path).split("."):
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


_LAYERS = ("A", "B", "C", "D", "E", "F", "G", "H", "I")
_SEVERITIES = ("hard", "uncertain", "soft")
_LAYER_NAMES = {"A": "目标与解释角色", "B": "材料模型保真", "C": "抽象映射保真",
                "D": "物理可表达性与尺度", "E": "设定自洽", "F": "数值与有限尺度",
                "G": "统计与收敛", "H": "对照协议与可比性", "I": "完整性"}


def _finding(layer, aspect, severity, title, detail, rationale, cited, refs,
             remediation, unverified=False, canary=False):
    return {"layer": layer if layer in _LAYERS else "I",
            "gate": "REVIEW", "aspect": aspect, "severity": severity, "title": title,
            "detail": detail, "rationale": rationale, "cited_fields": cited,
            "evidence_refs": refs, "remediation_hints": remediation,
            "unverified": unverified, "canary_experience": canary}


def arbitrate(parsed, intent, facts, gaps, entries, checklists):
    """代码仲裁:幻觉字段剔除/伪造引用降级(三档)/结构缺口合并/补全合并。永不抛。"""
    exp_ids = {e["id"]: e for e in entries}
    fact_keys = {"$" + k for k in facts}
    hard, uncertain, soft = [], [], []

    for raw in (parsed.get("findings") or [])[:16]:
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        sev = str(raw.get("severity") or "soft")
        severity = sev if sev in _SEVERITIES else "soft"
        cited = [str(c) for c in raw.get("cited_fields") or []
                 if _path_exists(intent, str(c))][:6]
        refs = [str(r) for r in raw.get("evidence_refs") or []][:6]
        valid_refs = [r for r in refs
                      if r in exp_ids or r in fact_keys or r == "structural"]
        canary_only = bool(valid_refs) and all(
            r in exp_ids and exp_ids[r].get("status") == "canary" for r in valid_refs)
        unverified = not valid_refs
        # 三档纪律:hard 无真凭据或仅凭 canary → 降 uncertain(判不了,比降 soft 更诚实);
        # uncertain 本义=判不了,不再降级;canary 只能撑 soft
        if severity == "hard" and (unverified or canary_only):
            severity = "uncertain"
        if severity == "soft" and canary_only:
            pass
        item = _finding(str(raw.get("layer") or "I"), str(raw.get("aspect") or "方案"),
                        severity, str(raw["title"])[:120], str(raw.get("detail") or "")[:600],
                        str(raw.get("rationale") or "")[:300], cited, valid_refs,
                        [str(x) for x in raw.get("remediation") or []][:5],
                        unverified=unverified, canary=canary_only)
        {"hard": hard, "uncertain": uncertain, "soft": soft}[severity].append(item)

    # 结构缺口确定性合并:缺必填字段=hard,不依赖 AI
    guidance = {}
    for cl in checklists:
        for g in cl.get("guidance") or []:
            guidance[str(g.get("aspect"))] = g
    completions = []
    seen_aspects = set()
    for g in gaps:
        item = _finding("I", "completeness", "hard", "缺失必填字段: %s" % g["path"],
                        "完整性清单(族 %s)存在性扫描:该字段缺失。完整方案的必填项不齐,无法可靠审阅物理内容。" % g["family"],
                        "代码存在性扫描(确定性)", [g["path"]], ["structural"],
                        ["按补全建议补齐该字段后再终审"])
        item["gate"] = "S-FIELD"  # 结构缺口的 gate 名与 AI findings 区分
        hard.append(item)
        gd = guidance.get(g["path"])
        if gd:
            completions.append({"layer": "I", "aspect": g["path"], "why": gd.get("why"),
                                 "proposal": gd.get("proposal")})
            seen_aspects.add(g["path"])

    for raw in (parsed.get("completions") or [])[:12]:
        if not isinstance(raw, dict) or not str(raw.get("aspect") or "").strip():
            continue
        aspect = str(raw["aspect"]).strip()
        if aspect in seen_aspects:
            continue
        seen_aspects.add(aspect)
        completions.append({"layer": str(raw.get("layer") or "I"),
                             "aspect": aspect,
                             "why": str(raw.get("why") or "")[:200],
                             "proposal": str(raw.get("proposal") or "")[:300]})

    return hard, uncertain, soft, completions


# ---------- 主入口 ----------
def run(intent, ctx=None):
    """审阅一份 intent → 报告。永不抛;AI 不可用=REVIEW_ERROR(fail-closed)。"""
    started = time.time()
    try:
        facts = physics_gate.extract_facts(intent, ctx)
        grade, grade_reasons = physics_gate.effective_grade(intent)
        exp = load_experience(intent)
        # 展示面=核验面:注入简报的条目就是可引用的条目(截断后统一用 exp_view)
        exp_view = dict(exp)
        exp_view["entries"] = exp["entries"][:MAX_ENTRIES_IN_BRIEF]
        gaps = physics_gate.structural_scan(intent, exp["checklists"], grade=grade)
        brief = build_brief(intent, facts, gaps, exp_view, grade, grade_reasons,
                            input_texts=ctx.get("input_texts"))

        parsed, tokens, provider, err, infra = _review_llm(brief)
        base = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": "design_review",
            "task_grade": grade,
            "grade_reasons": grade_reasons,
            "facts": facts,
            "structural_gaps": [{"path": g["path"], "family": g["family"]} for g in gaps],
            "experience": {"packs": exp["packs"],
                            "used_ids": [e["id"] for e in exp_view["entries"]],
                            "quarantined": exp["quarantined"]},
            "deferred_to_runtime": list(physics_gate.DEFERRED_TO_RUNTIME),
            "accepted_warnings": [],
            "skipped_gates": [],
            "cost": {"llm_tokens": tokens, "provider": provider,
                     "elapsed_sec": round(time.time() - started, 1)},
        }
        if parsed is None:
            base.update({"status": "REVIEW_ERROR" if infra else "DEGRADED",
                         "error": err or "审阅器输出不可用",
                         "passed": False,
                         "hard_violations": [], "uncertain_findings": [],
                         "soft_warnings": [], "completion_suggestions": [],
                         "brief_digest": brief[:800]})
            # 结构缺口不依赖 AI:即使审阅器失败,缺必填字段仍如实报 hard(fail-closed 但不隐瞒事实)
            for g in gaps:
                item = _finding("I", "completeness", "hard", "缺失必填字段: %s" % g["path"],
                                "完整性清单(族 %s)存在性扫描:该字段缺失。" % g["family"],
                                "代码存在性扫描(确定性)", [g["path"]], ["structural"],
                                ["按补全建议补齐"])
                item["gate"] = "S-FIELD"
                base["hard_violations"].append(item)
            return base

        hard, uncertain, soft, completions = arbitrate(parsed, intent, facts, gaps,
                                                        exp_view["entries"], exp["checklists"])
        base.update({"status": "OK", "passed": not hard and not uncertain,
                     "hard_violations": hard, "uncertain_findings": uncertain,
                     "soft_warnings": soft,
                     "completion_suggestions": completions,
                     "layer_summary": _layer_summary(hard, uncertain, soft),
                     "verdict": str(parsed.get("verdict") or "")[:300]})
        return base
    except Exception as ex:  # noqa: BLE001 —— 流水线不能让 MCP 层崩
        return {"status": "REVIEW_ERROR", "passed": False,
                "error": "%s: %s" % (type(ex).__name__, ex),
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "hard_violations": [], "uncertain_findings": [], "soft_warnings": [],
                "completion_suggestions": [], "deferred_to_runtime":
                    list(physics_gate.DEFERRED_TO_RUNTIME), "accepted_warnings": [],
                "skipped_gates": []}


def _layer_summary(hard, uncertain, soft):
    """按层小结(报告归组):{layer: {name, hard:N, uncertain:N, soft:N}}。"""
    out = {}
    for items, key in ((hard, "hard"), (uncertain, "uncertain"), (soft, "soft")):
        for it in items:
            lay = it.get("layer", "I")
            row = out.setdefault(lay, {"name": _LAYER_NAMES.get(lay, lay),
                                        "hard": 0, "uncertain": 0, "soft": 0})
            row[key] += 1
    return dict(sorted(out.items()))
