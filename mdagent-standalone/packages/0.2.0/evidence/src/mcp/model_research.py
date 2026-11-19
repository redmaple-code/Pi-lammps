# -*- coding: utf-8 -*-
"""
sim_model_research 的确定性研讨流水线(流程写死在代码里,不靠模型自觉)
==================================================================
模型/力场选型问题的一步式研究流水线:

    PLAN(纯代码拆题) → COLLECT(N 路并行"收集智能体",各自带联网搜索)
    → EVALUATE(一个"衡量智能体"按固定量规打分) → ARBITRATE(纯代码终裁)

设计纪律(与 pipeline_check.py 一致):
- 任何异常都返回 {"status": "RESEARCH_ERROR", ...},绝不抛给 MCP 层;
- 收集器/评测器内部的简报与量规是代码常量,不是会话提示词——
  会话模型无法看到也无法改写它们,只能拿到终裁后的结构化结果;
- 零引用结论必须标注 unverified 并压分,绝不把没有证据的策略当成结论;
- 评测器输出非法时重试一次,再坏则 DEGRADED 返回原始笔记,绝不编造。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths

# ---- 端点与额度(演化面:见 evolution/POLICY.json model_research_queries) ----
KIMI_ENDPOINT = "https://api.kimi.com/coding/v1/messages"  # Anthropic Messages API
KIMI_MODEL = "k3"
# 供应商链(2026-09-14,与 knowledge_cards._providers 同款纪律): Kimi 401/失效自动落
# BigModel glm-4.6(实测同支持 web_search 服务端工具,但 tool_result.content 是纯
# 字符串而非结构化列表——_parse_blocks 两侧兼容)。glm 思考链吃输出预算,
# max_tokens 按供应商放大约 2.5 倍(对齐 card_council 8000→16000 的实测教训)。
BIGMODEL_ENDPOINT = "https://open.bigmodel.cn/api/anthropic/v1/messages"
BIGMODEL_MODEL = "glm-4.6"
# 第三家(2026-11-15): 本机可用的 DeepSeek 中转站, Anthropic 兼容端点。
# 采纳原因: Kimi 无凭证、智谱 key 于 2026-11-15 实测已用满周/月额度(429, 2026-11-18 重置),
# 两路都不可用时调研链路整条失效, 而本机确实有一个可用的 Messages 兼容端点。
# 顺序放在最后, 不改变既有 Kimi→glm 的优先关系。
RELAY_ENDPOINT = "https://aispot.swj0227.icu/v1/messages"
RELAY_MODEL = "deepseek-v4.1-flash"
MAX_COLLECTORS = 6
COLLECTOR_TIMEOUT_SEC = 90
EVALUATOR_TIMEOUT_SEC = 150
COLLECTOR_MAX_TOKENS = 2048
EVALUATOR_MAX_TOKENS = 3072
SEARCH_MAX_USES = 3
UNVERIFIED_SCORE_CAP = 40  # 无引用策略的最高分

# ---- PLAN:子查询模板(纯代码;改这里要走演化提案,不许会话模型碰) ----
SUBQUERY_TEMPLATES = [
    "{q} —— 候选力场/参数集清单:各自适用的元素体系与物理场景",
    "{q} —— 精度 benchmark:与 DFT/实验对比的定量误差数据",
    "{q} —— 已知失效模式与适用域边界:什么情况会算错",
    "{q} —— 近期文献(近两年)与维护状态:是否还在更新、社区采用度",
]
REPAIR_SUBQUERY_TEMPLATES = [
    "{q} —— 官方病因:LAMMPS 手册/文档对该报错或异常现象的解释",
    "{q} —— 社区同症状案例:邮件列表/论坛里同样的异常是怎么修好的",
    "{q} —— 本体系与力场的已知坑:该材料/参数集下此类异常的失效模式",
    "{q} —— 候选修复策略的副作用:降 timestep/换系综/预平衡/结构重整各自的代价与风险",
]
# design 模式(2026-09-30 前置双门 C2e):实验设计合理性外部论证——
# 下压深度/磨头几何/载荷/滑速/样品尺度等设计参数须文献锚定,对仿真完整负责
DESIGN_SUBQUERY_TEMPLATES = [
    "{q} —— 文献基准:同类体系/工况已发表 MD 工作里这些设计参数的典型取值范围",
    "{q} —— 经典判据:该类实验设计的无量纲规则与经验公式(压深比/接触力学/热扩散长度/边界影响区)",
    "{q} —— 已知设计缺陷:哪些设计选择会导致非物理结果(边界效应/控温伪影/尺度效应/速率外推)",
    "{q} —— 实验对照:同类实验的测量值与实验条件,供设计参数锚定与换算",
]
MODE_TEMPLATES = {"select": SUBQUERY_TEMPLATES, "repair": REPAIR_SUBQUERY_TEMPLATES,
                  "design": DESIGN_SUBQUERY_TEMPLATES}

# ---- COLLECT / EVALUATE 的固定简报(代码常量,非会话提示词) ----
COLLECTOR_BRIEF = """你是分子模拟资料收集器。针对给定子课题联网检索,输出紧凑笔记:
1) 3-6 条关键事实(每条带数字/条件,注明来源 URL);
2) 关键定量数据(误差、温度/压力范围、体系);
3) 注意事项与争议点。
只写有来源支撑的内容;没有来源的判断标 [无来源]。用中文,不超过 600 字。
子课题: %s"""

EVALUATOR_RUBRIC = """你是分子模拟选型评审。下面是多路收集器带回的调研笔记(含来源 URL)。
按量规给每个候选策略打分并给出最终建议。量规(各 0-25 分,总分 0-100):
- 证据强度(有定量 benchmark 得分高)
- 来源独立性(多个互不同源的出处得分高)
- 时效性(近两年仍在维护/引用得分高)
- 与用户体系的匹配度(元素覆盖、物理场景吻合得分高)

严格输出 JSON(不要输出任何其他文字):
{"strategies":[{"name":"...","score":0,"evidence":["url1"],"risks":["..."],"when_to_use":"..."}],
 "verdict":"一句话最终建议"}
rules: score 必须 0-100 整数;evidence 里的 URL 必须来自笔记中出现过的来源;
没有来源支撑的策略 evidence 给空数组。候选策略 2-4 个。

用户问题: %s
调研笔记:
%s"""

EVALUATOR_RUBRIC_REPAIR = """你是分子模拟故障修复评审。下面是多路收集器带回的调研笔记(含来源 URL)。
按量规给每个候选修复策略打分并给出最终建议。量规(各 0-25 分,总分 0-100):
- 机理对症(针对该 case 的系综/结构/参数与异常根因得分高)
- 证据强度(有同症状修复成功案例或官方文档依据得分高)
- 来源置信度(官方文档 > 邮件列表 > 论坛经验帖)
- 副作用可控(改动可回滚、不动化学模型与力场文件的得分高)

严格输出 JSON(不要输出任何其他文字):
{"strategies":[{"name":"...","score":0,"evidence":["url1"],"risks":["..."],"when_to_use":"..."}],
 "verdict":"一句话最终建议"}
rules: score 必须 0-100 整数;evidence 里的 URL 必须来自笔记中出现过的来源;
没有来源支撑的策略 evidence 给空数组。候选策略 2-4 个;一次只改一个变量的策略优先。

用户问题: %s
调研笔记:
%s"""
EVALUATOR_RUBRIC_DESIGN = """你是分子模拟实验设计评审。用户给出一份仿真实验设计(如下),
下面是多路收集器带回的调研笔记(含来源 URL)。逐个设计参数给出合理性判定与最终建议。
量规(各 0-25 分,总分 0-100):
- 文献覆盖(有同类 MD 工作的具体取值范围支撑得分高)
- 判据明确(有无量纲规则/经验公式可依,如压深比、接触半径、热扩散长度)
- 与本设计匹配(体系/工况/尺度对得上,不是泛泛而谈)
- 风险识别(边界效应/控温伪影/尺度效应/速率外推等坑是否指出)

严格输出 JSON(不要输出任何其他文字):
{"strategies":[{"name":"<设计参数名,如 indentation_depth_A>","score":0,
  "evidence":["url1"],"risks":["非物理风险"],"when_to_use":"文献典型范围与建议取值"}],
 "verdict":"一句话设计合理性总评"}
rules: score 必须 0-100 整数;evidence 里的 URL 必须来自笔记中出现过的来源;
没有来源支撑的判定 evidence 给空数组。strategies 覆盖设计里的关键参数,2-6 个。
判定存疑的参数在 risks 里写清风险与文献范围。

用户设计:
%s
调研笔记:
%s"""
MODE_RUBRICS = {"select": EVALUATOR_RUBRIC, "repair": EVALUATOR_RUBRIC_REPAIR,
                "design": EVALUATOR_RUBRIC_DESIGN}

# 凭证随 DSH_HOME 外置(2026-08-29 阶段5):dsh-home/.credentials.yaml;惰性解析(对抗审计 F6)
def _credentials_yaml():
    """凭证库位置。

    DSH_HOME 优先：本地部署时 DSH 的 home 未必等于 <data_root>/dsh-home
    （2026-11-15 实测：paths.dsh_home() 解析到 data_root 下的子目录，而真实
    凭证库在 C:\\Users\\<user>\\.dsh）。只认 dsh_home 会让所有凭证读不到，
    从而把「凭证缺失」误报成「调研全部失败」。回退到 dsh_home 保持服务器行为不变。
    """
    home = os.environ.get("DSH_HOME", "").strip()
    if home:
        return os.path.join(home, ".credentials.yaml")
    return os.path.join(paths.dsh_home(make=False), ".credentials.yaml")


def _cred(name):
    """单个凭证: env 优先, 其次 credentials.yaml。缺失返回空串。

    文档版本 1 把凭证放在 `refs:` 下的缩进子项里, 旧的正则要求行首匹配,
    于是带 refs 段的凭证库一个也读不到(2026-11-15 实测: 两个键都 NO MATCH)。
    这里同时接受顶层与 refs 子项两种写法, 不解析 YAML 结构以免把整份文档读进内存。
    """
    v = os.environ.get(name, "").strip()
    if v:
        return v
    try:
        for line in open(_credentials_yaml(), encoding="utf-8"):
            m = re.match(r'^\s*(?:-\s*)?%s:\s*"?([^"\n]+)"?\s*$' % name, line)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return ""


def _providers():
    """供应商链: Kimi 优先(同源默认), 401/失效自动落 BigModel glm, 再落本机中转站。"""
    chain = []
    k = _cred("KIMI_API_KEY")
    if k:
        chain.append(("kimi-k3", KIMI_ENDPOINT, KIMI_MODEL, k))
    g = _cred("BIGMODEL_ANTHROPIC_API_KEY")
    if g:
        chain.append(("glm-4.6", BIGMODEL_ENDPOINT, BIGMODEL_MODEL, g))
    r = _cred("DEEPSEEK_API_KEY")
    if r:
        chain.append(("deepseek-v4.1-flash", RELAY_ENDPOINT, RELAY_MODEL, r))
    return chain


def _post_anthropic(body, timeout):
    """POST Anthropic Messages 请求(供应商链逐个试), 返回 (响应 dict, 供应商名)。
    401/403 换下一家; 429 也要换——限流是上游的临时状态, 不是本请求的错,
    继续挂在同一家只会让整条调研链路恒失败。其他网络/HTTP 错误由调用方兜。"""
    chain = _providers()
    if not chain:
        raise RuntimeError("找不到任何 LLM 凭证(KIMI_API_KEY/BIGMODEL_ANTHROPIC_API_KEY)")
    last_err = "no-provider"
    for pname, endpoint, model, key in chain:
        b = dict(body)
        b["model"] = model
        if pname.startswith("glm"):
            b["max_tokens"] = max(int(b.get("max_tokens", 0)) * 5 // 2, 3000)
        req = urllib.request.Request(
            endpoint, data=json.dumps(b).encode(),
            headers={"x-api-key": key, "authorization": "Bearer " + key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode()), pname
        except urllib.error.HTTPError as e:
            last_err = "http%s@%s" % (e.code, pname)
            if e.code in (401, 403, 429):
                continue  # 凭证失效或上游限流 → 换下一家
            raise
    raise RuntimeError("LLM 供应商链全部失败: %s" % last_err)


def _parse_blocks(payload):
    """从 Anthropic 响应抽 (正文文本, 来源列表, token 用量)。
    来源两侧兼容: Kimi=web_search_tool_result(结构化列表);
    BigModel=tool_result(content 为纯字符串,正则收割 URL)。"""
    texts, sources, seen = [], [], set()

    def _add(url, title="", published=""):
        url = (url or "").strip().rstrip(".,;:!?。，；：、）)】]》>\"'")
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            sources.append({"url": url, "title": title, "published": published})

    for block in payload.get("content", []):
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "web_search_tool_result":
            items = block.get("content")
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    _add(item.get("url", ""), item.get("title", ""),
                         item.get("page_age", ""))
        elif btype == "tool_result":
            c = block.get("content")
            if isinstance(c, str):
                for m in re.finditer(r"https?://[^\s\"'<>)\]}]+", c):
                    _add(m.group(0))
            elif isinstance(c, list):
                for item in c:
                    if isinstance(item, dict):
                        _add(item.get("url", ""), item.get("title", ""),
                             item.get("page_age", ""))
    usage = payload.get("usage") or {}
    tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    return "\n".join(t for t in texts if t).strip(), sources, tokens


def _collect(idx, sub_query):
    """单路收集智能体:联网检索 + 结构化笔记。失败只标记,不中断流水线。"""
    out = {"idx": idx, "sub_query": sub_query, "ok": False,
           "note": "", "sources": [], "tokens": 0, "error": None, "provider": ""}
    try:
        payload, out["provider"] = _post_anthropic({
            "max_tokens": COLLECTOR_MAX_TOKENS,
            "messages": [{"role": "user", "content": COLLECTOR_BRIEF % sub_query}],
            "tools": [{"type": "web_search_20250305", "name": "web_search",
                       "max_uses": SEARCH_MAX_USES}],
        }, COLLECTOR_TIMEOUT_SEC)
        note, sources, tokens = _parse_blocks(payload)
        if not note:
            out["error"] = "收集器返回空笔记"
            return out
        out.update(ok=True, note=note, sources=sources, tokens=tokens)
    except Exception as ex:  # noqa: BLE001 —— 单路失败不得拖垮整批
        out["error"] = "%s: %s" % (type(ex).__name__, ex)
    return out


def _evaluate(question, notes_text, mode="select"):
    """衡量智能体:按量规打分。返回 (dict|None, tokens, provider);非法输出重试一次。"""
    rubric = MODE_RUBRICS.get(mode, EVALUATOR_RUBRIC)
    provider = ""
    for attempt, strict in enumerate(("", "\n\n再次强调:只输出 JSON,第一个字符必须是 {。")):
        try:
            payload, provider = _post_anthropic({
                "max_tokens": EVALUATOR_MAX_TOKENS,
                "messages": [{"role": "user",
                              "content": rubric % (question, notes_text) + strict}],
            }, EVALUATOR_TIMEOUT_SEC)
            text, _, tokens = _parse_blocks(payload)
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                parsed = json.loads(m.group(0))
                if isinstance(parsed.get("strategies"), list):
                    return parsed, tokens, provider
        except Exception:  # noqa: BLE001 —— 重试路径统一兜底
            pass
    return None, 0, provider


def _arbitrate(parsed, known_urls):
    """纯代码终裁:校验 schema、钳分、零引用压分、来源去重。永不抛。"""
    strategies = []
    for raw in parsed.get("strategies", []):
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        try:
            score = max(0, min(100, int(raw.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        evidence = [u for u in raw.get("evidence", [])
                    if isinstance(u, str) and u.startswith("http")]
        cited = [u for u in evidence if u in known_urls]
        verified = len(cited) > 0
        if not verified:
            score = min(score, UNVERIFIED_SCORE_CAP)
        strategies.append({
            "name": str(raw["name"]), "score": score, "verified": verified,
            "evidence": cited if cited else evidence,
            "risks": [str(r) for r in raw.get("risks", [])][:5],
            "when_to_use": str(raw.get("when_to_use", "")),
        })
    strategies.sort(key=lambda s: (not s["verified"], -s["score"]))
    verdict = str(parsed.get("verdict", "")).strip()
    if not verdict and strategies:
        verdict = "优先: %s" % strategies[0]["name"]
    return strategies, verdict


def run(question, context=None, collectors=4, mode="select"):
    """跑一遍研讨流水线。mode: select(选型) | repair(故障修复)。永不抛异常。"""
    started = time.time()
    try:
        if mode not in MODE_TEMPLATES:
            mode = "select"
        templates = MODE_TEMPLATES[mode]
        if not question or not str(question).strip():
            return {"status": "RESEARCH_ERROR", "error": "缺少 question"}
        question = str(question).strip()
        if context:
            question = "%s(背景: %s)" % (question, str(context).strip()[:300])

        # PLAN
        n = max(1, min(int(collectors), MAX_COLLECTORS, len(templates)))
        sub_queries = [t.format(q=question) for t in templates[:n]]

        # COLLECT(并行)
        with ThreadPoolExecutor(max_workers=n) as pool:
            notes = list(pool.map(lambda a: _collect(*a), enumerate(sub_queries)))
        ok_notes = [x for x in notes if x["ok"]]
        degraded = [{"sub_query": x["sub_query"], "error": x["error"]}
                    for x in notes if not x["ok"]]
        if not ok_notes:
            return {"status": "RESEARCH_ERROR",
                    "error": "所有收集器均失败: %s" % (degraded[0]["error"] if degraded else "?"),
                    "question": question, "sub_queries": sub_queries,
                    "capability": "web_search_unavailable",
                    "verified": False,
                    "elapsed_sec": round(time.time() - started, 1)}

        # EVALUATE
        notes_text = "\n\n".join(
            "[子课题 %d] %s\n%s\n来源: %s" % (
                x["idx"] + 1, x["sub_query"], x["note"],
                "; ".join(s["url"] for s in x["sources"]) or "(无)")
            for x in ok_notes)
        parsed, eval_tokens, eval_provider = _evaluate(question, notes_text, mode)

        sources, seen = [], set()
        for x in ok_notes:
            for s in x["sources"]:
                if s["url"] not in seen:
                    seen.add(s["url"])
                    sources.append(s)

        total_tokens = sum(x["tokens"] for x in notes) + eval_tokens
        providers = sorted({x["provider"] for x in ok_notes
                            if x.get("provider")} |
                           ({eval_provider} if eval_provider else set()))
        base = {"question": question, "sub_queries": sub_queries, "mode": mode,
                "sources": sources, "degraded": degraded,
                "elapsed_sec": round(time.time() - started, 1),
                "cost": {"llm_calls": n + (1 if eval_tokens else 0),
                         "total_tokens": total_tokens, "providers": providers},
                "source": "model_research"}
        if not sources:
            # 端点接受了 web_search 工具定义却不执行搜索时,只能给出无来源结论。
            base["capability"] = "web_search_unavailable"
            base["verified"] = False
            base["reason"] = ("provider accepted the web_search tool but returned no "
                              "web_search_tool_result block")

        if parsed is None:
            base.update(status="DEGRADED",
                        error="评测器两次输出均非合法 JSON,返回原始笔记",
                        notes=[{"sub_query": x["sub_query"], "note": x["note"],
                                "sources": [s["url"] for s in x["sources"]]}
                               for x in ok_notes])
            return base

        # ARBITRATE
        strategies, verdict = _arbitrate(parsed, seen)
        if not strategies:
            base.update(status="DEGRADED",
                        error="评测器输出无任何策略,返回原始笔记",
                        notes=[{"sub_query": x["sub_query"], "note": x["note"]}
                               for x in ok_notes])
            return base
        base.update(status="OK", strategies=strategies, verdict=verdict,
                    notes=[{"sub_query": x["sub_query"], "note": x["note"]}
                           for x in ok_notes])
        return base
    except Exception as ex:  # noqa: BLE001 —— 流水线不能让 MCP 层崩
        return {"status": "RESEARCH_ERROR",
                "error": "%s: %s" % (type(ex).__name__, ex),
                "elapsed_sec": round(time.time() - started, 1)}


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "ReaxFF CHO 体系哪个参数集最稳"
    print(json.dumps(run(q), ensure_ascii=False, indent=2))
