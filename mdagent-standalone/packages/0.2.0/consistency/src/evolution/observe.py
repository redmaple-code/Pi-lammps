#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演化观测层(rsi-loop Observe→Analyze 的最小等价物,stdlib)
==========================================================
订阅既有观测流,做复发检测(窗口内 ≥3 次同类),自动生成提案草稿:

    观测源                          复发模式                    提案方向
    mcp/_flowgate.jsonl             违规 ≥3 次/7天              flow_gate_intent_terms(意图正则复查)
    mcp/_flowgate.jsonl             repeat-question ≥3 次/7天   persona_wording(回答质量,Tier2)
    mcp/_flowgate.jsonl             user-correction ≥3 次/7天   persona_wording(回答质量,Tier2)
    events/pipeline.jsonl           PIPELINE_ERROR ≥3 例/7天    pipeline_steps(流水线健壮性)
    events/pipeline.jsonl           CRITICAL ≥3 个 case/7天     monitor_logic(阈值/判据复查,Tier2)

rsi-os 集成:起草提案时按失效签名在 rsi_knowledge/graph.json(139 节点知识图谱)
里匹配相关 RSI 论文/仓库,引用写进 evidence.md——提案有据可依,不是空改。
TencentDB-Agent-Memory 集成:每条失效同时记入训练记忆(L1 原子层),
并重建 L2 场景/L3 SOP;提案证据里附该签名的历史修复记录(若有)。

注:pipeline 观测源 2026-09-05 升级为追加式事件流(events/pipeline.jsonl,
pipeline_check 每次调用终态追加一条,含 PIPELINE_ERROR/NO_LOG——它们从不写 snap)。
旧覆盖式 snap 快照会把失效冲掉(case 从 CRITICAL 恢复后失效记录即消失),
现已只作迁移期只读兜底(旧 mcp/_pipeline 目录);第 4/5 类统计窗口内
处于该状态的 distinct case 数(面状语义不变),同 case 恢复后再崩跨窗可见。

草稿只生成 proposal.md + evidence.md + meta.json,diff.patch 留 TODO 由人/代理补全;
应用仍须过 apply_proposal.py 的 POLICY 校验与回归门——观测层没有任何直接写权限。

用法: python3 observe.py [--days 7] [--min-occurrences 3]
"""
import difflib
import glob
import hashlib
import json
import math
import os
import sys
import time

import memory
import behavior_strategies as bstrat

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mcp"))
import paths

# 数据根外置(2026-08-29):常量保留为测试猴子补丁入口
FLOWGATE_LOG = paths.flowgate_log()
PIPELINE_DIR = paths.pipeline_scratch_dir()
PIPELINE_EVENTS = paths.pipeline_events_log()
FRICTION_LOG = paths.friction_log()
PROPOSALS = os.path.join(paths.memory_dir(), "proposals")
# rsi_knowledge/graph.json 是随代码分发的静态知识资产(非演化产出),留在代码库
RSI_GRAPH = os.path.join(HERE, "rsi_knowledge", "graph.json")
# 迁移期只读兜底(旧位置,不写入)
LEGACY_MCP_DIR = os.path.join(os.path.dirname(HERE), "mcp")
DAY_SEC = 86400

# 失效签名 → RSI 图谱检索关键词(命中节点的 name/summary)
RSI_KEYWORDS = {
    "flow-gate-violation": ["guardrail", "governance", "self-improv", "gödel", "darwin"],
    "repeat-question": ["memory", "reflection", "self-improv", "meta-learning", "experience"],
    "user-correction": ["feedback", "reward", "self-improv", "reflection", "evaluation"],
    "pipeline_error": ["self-repair", "debug", "verify", "recovery"],
    "monitor_critical": ["evaluation", "benchmark", "judge", "verifier"],
    "recovery_consecutive_fail": ["self-repair", "recovery", "debug", "verify"],
    "friction-interrupt": ["reflection", "self-improv", "planning", "clarification"],
    "friction-tool-error": ["self-repair", "debug", "verify", "tool"],
    "friction-denial": ["guardrail", "governance", "feedback", "approval"],
}

# kit 策略库接入(2026-08-30):同一 (策略,根因) 尾部连败 ≥ 此值即起草压制提案
KIT_FAIL_STREAK = 2
# 连胜 ≥ 此值且仍有 penalty 即起草减压/解除提案(四轮对抗 M1:压制须有退出通道)
KIT_SUCCESS_STREAK = 2
# 连败 penalty 步进与上限(写入 kit db 的 strategy_adjustments 段)
KIT_PENALTY_STEP = 0.15
KIT_PENALTY_MAX = 0.6
# 连败/连胜只看近 N 天的已结账记录:更早的失败不应永久压制策略(四轮对抗 M1)
KIT_STREAK_WINDOW_DAYS = 14
# POLICY 路径提成模块常量:测试可猴子补丁重定向(L2 证据类读取同文件)
POLICY_PATH = os.path.join(HERE, "POLICY.json")


def _kit_db_path():
    """kit 策略 db 路径(POLICY roots.kit 解析;测试猴子补丁本函数重定向)。"""
    pol = json.load(open(POLICY_PATH, encoding="utf-8"))
    return os.path.join(pol["roots"]["kit"]["path"], ".v3.3_strategy_db.json")


def _kit_inbox_path():
    """dsh→kit 用户闸收件箱(append-only;dsh 只投请求行,队列单写者是 kit orchestrator)。"""
    pol = json.load(open(POLICY_PATH, encoding="utf-8"))
    return os.path.join(pol["roots"]["kit"]["path"], "logs", "user_gate_requests.jsonl")


# 测试猴子补丁重定向;None=按 POLICY 解析
KIT_GATE_INBOX = None
OBSERVE_GATE_FILE = None


def _append_gate_request(req):
    """向 kit 收件箱追加一条闸请求。任何失败只告警——提案起草永不被桥阻塞。"""
    try:
        path = KIT_GATE_INBOX or _kit_inbox_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        req = dict(req)
        req.setdefault("ts", time.time())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[OBSERVE] warn: 用户闸请求投递失败: %s" % e, file=sys.stderr)


def _observe_gate_cfg():
    try:
        pol = json.load(open(POLICY_PATH, encoding="utf-8"))
        cfg = pol.get("observe_gate") or {}
        # float(24 轮审计):int(0.5)→0 会静默关停节流
        return {"min_interval_minutes": max(0.0, float(cfg.get("min_interval_minutes", 30)))}
    except Exception:
        return {"min_interval_minutes": 30}


def _should_observe(now=None):
    """observe 全局 should-run 闸(2026-09-07,借鉴 LoopX quota should-run):
    距上轮 < min_interval 跳过;kit 卡 FAILED 且有 pending 用户闸 → 等人工不空转。
    kit state 缺失=无闸可守→放行(observe_kit 本就容忍 kit 缺席);
    存在但损坏 → fail-closed 跳过。返回 (ok, verdict_dict)。"""
    now = now if now is not None else time.time()
    cfg = _observe_gate_cfg()
    gate_file = OBSERVE_GATE_FILE or os.path.join(paths.state_dir(), "observe_gate.json")
    last = 0.0
    try:
        with open(gate_file, encoding="utf-8") as f:
            last = float((json.load(f) or {}).get("last_run_ts", 0) or 0)
    except Exception:
        last = 0.0
    if now - last < cfg["min_interval_minutes"] * 60:
        return False, {"verdict": "SKIP", "reason": "too_soon",
                       "min_interval_minutes": cfg["min_interval_minutes"],
                       "last_run_ts": last}
    try:
        kit_root = os.path.dirname(_kit_db_path())
        kstate_path = os.path.join(kit_root, ".v3.3_state.json")
        if os.path.exists(kstate_path):
            try:
                ks = json.load(open(kstate_path, encoding="utf-8"))
            except Exception:
                return False, {"verdict": "SKIP", "reason": "kit_state_unreadable"}
            pending_n = 0
            gates_path = os.path.join(kit_root, ".v3.3_user_gates.json")
            if os.path.exists(gates_path):
                try:
                    gq = json.load(open(gates_path, encoding="utf-8"))
                    pending_n = len([g for g in gq.get("gates", [])
                                     if isinstance(g, dict) and g.get("status") == "pending"])
                except Exception:
                    # 24 轮审计:FAILED 态下闸文件损坏必须 fail-closed,
                    # 不得按 pending_n=0 放行空转
                    if ks.get("phase") == "FAILED":
                        return False, {"verdict": "SKIP",
                                       "reason": "kit_gates_unreadable"}
                    pending_n = 0
            if ks.get("phase") == "FAILED" and pending_n > 0:
                return False, {"verdict": "SKIP", "reason": "kit_failed_pending_gates",
                               "pending_gates": pending_n}
    except Exception as e:
        return False, {"verdict": "SKIP", "reason": "kit_check_error: %s" % e}
    return True, {"verdict": "RUN", "reason": "ok"}


def _emit_gate_event(verdict):
    """闸裁决追加进 events/pipeline.jsonl(与 pipeline_check 事件流同文件);
    任何失败静默,绝不阻断 observe 主链。"""
    try:
        os.makedirs(os.path.dirname(PIPELINE_EVENTS), exist_ok=True)
        # ts 用浮点 epoch(24 轮审计):与 pipeline_check 事件流同 schema,
        # 字符串 ts 会让按数值过滤的消费者静默跳过
        row = {"ts": time.time(),
               "case_id": "_observe_gate", "status": "OBSERVE_GATE"}
        row.update({k: verdict[k] for k in ("verdict", "reason") if k in verdict})
        with open(PIPELINE_EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)  # 合法 JSON 但非对象(裸字符串/数字)同样丢弃(二轮对抗)
    return rows


_rsi_nodes = None


def rsi_references(signature, limit=3):
    """按失效签名在 rsi-os 知识图谱里找相关论文/仓库,返回引用行。"""
    global _rsi_nodes
    if _rsi_nodes is None:
        try:
            _rsi_nodes = json.load(open(RSI_GRAPH, encoding="utf-8")).get("nodes", [])
        except Exception:
            _rsi_nodes = []
    keywords = RSI_KEYWORDS.get(signature, [])
    if not keywords:
        return []
    hits = []
    for node in _rsi_nodes:
        if not isinstance(node, dict):
            continue  # 畸形 graph.json 节点不降级崩溃(二轮对抗)
        if node.get("type") not in ("paper", "repo"):
            continue
        hay = (str(node.get("name", "")) + " " + str(node.get("summary", ""))).lower()
        if any(k in hay for k in keywords):
            hits.append(node)

    def _cites(n):
        c = n.get("citations", 0)
        return c if isinstance(c, (int, float)) and not isinstance(c, bool) else 0

    hits.sort(key=lambda n: -_cites(n))
    out = []
    for n in hits[:limit]:
        links = n.get("links")
        link = str(links[0]) if isinstance(links, list) and links else ""
        out.append("RSI 参考 [%s] %s — %s (%s)" % (
            n.get("type"), n.get("name", "?"),
            str(n.get("summary", "") or "")[:80], link))
    return out


def _proposal_dir_applied(pdir_name):
    """提案目录是否已应用(LEDGER 有该目录名的 APPLIED 记录)——已应用的问题允许
    再次生成提案(可能是真复发), 未应用的不许堆积(深挖修复 2026-09-15)。
    注意: 不得命名 _proposal_applied——553 行有同名旧函数会遮蔽本函数。"""
    try:
        for line in open(memory.LEDGER, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("slug") == pdir_name and e.get("result") in (
                    "AUTO_APPLIED", "APPLIED"):
                return True
    except OSError:
        pass
    return False


def _route_after_draft(tier, signature, title, pdir, metric_count):
    """提案落草稿(新建或合并)后的统一路由。
    auto+签名且未被翻案/冷却 → 自动蒸馏;auto+签名被翻案/冷却 → 人工闸;
    auto 无签名(传统 auto_fix 类) → 静默(旧语义,2026-09-17 回归实修);
    human → 人工闸。"""
    # v1.6.0:提案带三轴标签进闸,人工裁决时看得到归属领域
    axes = {}
    try:
        import tagging
        axes = tagging.tag_atom(signature or "", title)
    except Exception:
        pass
    if tier == "auto" and signature:
        if bstrat.route(signature) == "auto":
            # 行为复查类去人工化(2026-09-17 用户拍板):自动蒸馏进行为策略库,
            # 规则渲染进 persona 两处;后验不达标自动撤回,撤回才开人工闸。
            bstrat.auto_distill(signature, title, pdir, metric_count)
            return
        _append_gate_request({"op": "enqueue", "kind": "evolution_proposal",
                              "ref": os.path.basename(pdir), "question": title,
                              "evidence_refs": [pdir], "source": "dsh",
                              "axes": axes})
        return
    if tier == "human":
        # 桥(2026-09-07):human_review 提案投进 kit 用户闸收件箱,
        # orchestrator 扫箱入队+飞书——提案不再只埋在 proposals/ 目录
        _append_gate_request({"op": "enqueue", "kind": "evolution_proposal",
                              "ref": os.path.basename(pdir), "question": title,
                              "evidence_refs": [pdir], "source": "dsh",
                              "axes": axes})


def draft_proposal(slug, cls, tier, root, title, evidence_lines, signature=None,
                   metric_count=None):
    date = time.strftime("%Y-%m-%d")
    pdir = os.path.join(PROPOSALS, "%s-%s" % (date, slug))
    if signature:
        for line in rsi_references(signature):
            evidence_lines.append(line)
        past = memory.find_scenario(signature)
        if past:
            # 损坏的 scenarios.json(缺 atom_ids/count、id 非 str)不得打崩起草(五轮对抗)
            atom_ids = [str(a) for a in (past.get("atom_ids") or [])[-5:]]
            evidence_lines.append(
                "训练记忆:该签名已复发 %d 次,溯源 atoms %s" % (
                    past.get("count", 0), ",".join(atom_ids)))
    # v8深挖修复(2026-09-15): 提案合并——同 slug 已有未过期未应用的提案时,
    # 不重复生成, 新证据追加进旧提案(证据随复发累积, 提案不堆积。
    # 此前同一问题每天一份 TODO 提案: 09-11~09-15 五份 flowgate-intent-review)
    for old in sorted(glob.glob(os.path.join(PROPOSALS, "*-%s" % slug))):
        if old == pdir:
            break  # 同日同名, 走 makedirs 竞态路径
        if os.path.exists(os.path.join(old, "expired.json")):
            continue  # 已过期(>7天无人理), 允许重新生成提醒
        if _proposal_dir_applied(os.path.basename(old)):
            continue  # 已应用闭环, 复发是真新问题
        try:
            with open(os.path.join(old, "evidence.md"), "a", encoding="utf-8") as f:
                f.write("\n## %s 追加(复发仍在持续)\n" % date)
                f.write("\n".join("- %s" % e for e in evidence_lines) + "\n")
        except OSError:
            pass
        # 合并路径只补蒸馏不补闸(2026-09-17):旧提案是人工闸时代留下的,
        # 不补蒸馏 auto 通道永远不点火;人工闸不重复开(撤回时已开过/旧闸还在)。
        if tier == "auto" and signature and bstrat.route(signature) == "auto":
            bstrat.auto_distill(signature, title, old, metric_count)
        return None
    try:
        os.makedirs(pdir)
    except FileExistsError:
        # 同日同名已有草稿:不重复生成,但 auto 通道仍须幂等补蒸馏
        # (草稿可能是人工闸时代/cron 旧代码生成的,2026-09-17)
        if tier == "auto" and signature and bstrat.route(signature) == "auto":
            bstrat.auto_distill(signature, title, pdir, metric_count)
        return None  # 同日同类草稿只生成一份(exists 检查与 makedirs 间竞态,八轮对抗)
    open(os.path.join(pdir, "proposal.md"), "w", encoding="utf-8").write(
        "# %s\n\n观测层复发检测自动生成的草稿。动机: 同一模式在窗口内反复出现,"
        "提示现有逻辑需要演化。\n\n## 待办\n- [ ] 人工/代理审查 evidence.md,"
        "确认根因\n- [ ] 编写 diff.patch(TODO)\n- [ ] 过 apply_proposal.py 门禁\n" % title)
    open(os.path.join(pdir, "evidence.md"), "w", encoding="utf-8").write(
        "# 观测证据\n\n" + "\n".join("- %s" % e for e in evidence_lines) + "\n")
    open(os.path.join(pdir, "diff.patch"), "w", encoding="utf-8").write(
        "# TODO: 在此编写统一 diff(路径相对 root=%s)\n" % root)
    meta = {"root": root, "class": cls, "tier": tier}
    try:
        import tagging  # v1.6.0:提案 meta 带三轴标签(课题/方法/运行)
        meta["axes"] = tagging.tag_atom(signature or cls or "", title)
    except Exception:
        pass
    json.dump(meta,
              open(os.path.join(pdir, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    _route_after_draft(tier, signature, title, pdir, metric_count)
    return pdir


def observe(days=7, min_occurrences=3):
    min_occurrences = max(1, min_occurrences)  # 直接函数调用也须钳位
    days = max(1, days)  # days=0 时窗口为空连当前事件都被排除,与"今天"语义不符(五轮对抗)
    window_start = time.time() - days * DAY_SEC
    drafted = []
    summary = {"window_days": days, "min_occurrences": min_occurrences, "patterns": []}

    # 1. flow-gate 全部信号(违规 + 会话失效:重复追问/否定纠正)
    flowgate_path = FLOWGATE_LOG
    if not os.path.exists(flowgate_path):
        flowgate_path = os.path.join(LEGACY_MCP_DIR, "_flowgate.jsonl")
    now = time.time()
    flowlog = []
    for v in read_jsonl(flowgate_path):
        try:
            t = float(v.get("t", 0)) / 1000.0  # 毫秒时间戳;字符串/None 等坏行跳过
        except (TypeError, ValueError):
            continue
        if window_start <= t <= now + 3600:  # 未来时间戳(>1h 容差)不计入窗口
            # 原始 t(毫秒整数)不动:refs 幂等键要与旧版格式一致,浮点回写会让同一事件重复入账(二轮对抗)
            v["t_sec"] = t
            flowlog.append(v)
    violations = [v for v in flowlog if v.get("kind") == "flow-gate-violation"]
    repeats = [v for v in flowlog if v.get("kind") == "repeat-question"]
    corrections = [v for v in flowlog if v.get("kind") == "user-correction"]

    # 接缝4修复(2026-09-15): 提案前校验问题是否仍然存在——7 天窗口内攒够次数但
    # 最近 24h 已无新发生的, 视为已修复/已消失, 不再对它起草提案(防对老账空转提案)。
    def _is_still_relevant(kind):
        cutoff = time.time() - 86400
        return any(v.get("kind") == kind and v.get("t_sec", 0) > cutoff
                   for v in flowlog)

    for kind, rows in (("flow-gate-violation", violations),
                       ("repeat-question", repeats),
                       ("user-correction", corrections)):
        summary["patterns"].append({"kind": kind, "count": len(rows)})
        for v in rows:
            memory.record_atom(kind, v.get("userText", ""),
                               refs=["mcp/_flowgate.jsonl#%s" % v.get("t", "?")])

    if len(violations) >= min_occurrences and _is_still_relevant("flow-gate-violation"):
        p = draft_proposal(
            "flowgate-intent-review", "flow_gate_intent_terms", "auto", "dsh_agent",
            "flow-gate 违规复发:意图正则或证据清单需复查",
            ["%s: %s" % (time.strftime("%m-%d %H:%M", time.localtime(v["t_sec"])),
                          v.get("userText", "")) for v in violations[-10:]],
            signature="flow-gate-violation")
        if p:
            drafted.append(p)
    if len(repeats) >= min_occurrences and _is_still_relevant("repeat-question"):
        p = draft_proposal(
            "repeat-question-review", "persona_wording", "auto", "dsh_agent",
            "同题换措辞反复追问:回答未解决用户问题,路由/叙事需复查",
            ["%s: %s ⟲ %s" % (time.strftime("%m-%d %H:%M", time.localtime(v["t_sec"])),
                               str(v.get("previousText", ""))[:60],
                               str(v.get("userText", ""))[:60])
             for v in repeats[-10:]],
            signature="repeat-question", metric_count=len(repeats))
        if p:
            drafted.append(p)
    if len(corrections) >= min_occurrences and _is_still_relevant("user-correction"):
        p = draft_proposal(
            "user-correction-review", "persona_wording", "auto", "dsh_agent",
            "用户否定纠正复发:回答质量/事实性需复查",
            ["%s: %s" % (time.strftime("%m-%d %H:%M", time.localtime(v["t_sec"])),
                          v.get("userText", "")) for v in corrections[-10:]],
            signature="user-correction", metric_count=len(corrections))
        if p:
            drafted.append(p)

    # 2. pipeline 事件流(2026-09-05 升级):读追加式 jsonl;旧覆盖式 snap 只作迁移期兜底
    # distinct case 计数(面状语义不变):同 case 窗内反复 CRITICAL 计一例,证据取最新一条
    errors, criticals = {}, {}
    for ev in read_jsonl(PIPELINE_EVENTS):
        ets = ev.get("ts")
        if not isinstance(ets, (int, float)) or ets < window_start:
            continue
        cid = str(ev.get("case_id") or "unknown")
        status = ev.get("status")
        if status == "PIPELINE_ERROR":
            errors[cid] = "%s: %s" % (cid, str(ev.get("error", ""))[:120])
        elif status == "CRITICAL":
            criticals[cid] = "%s: risk=%s factors=%s" % (
                cid, ev.get("risk_score"), ev.get("risk_factors"))
    for snap_path in glob.glob(os.path.join(LEGACY_MCP_DIR, "_pipeline", "snap_*.json")):
        if os.path.getmtime(snap_path) < window_start:
            continue
        try:
            snap = json.load(open(snap_path, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(snap, dict):
            continue  # 合法 JSON 但非 dict(如 [])会击穿 .get(四轮对抗)
        cid = str(snap.get("case_id") or "unknown")
        if snap.get("status") == "PIPELINE_ERROR":
            errors.setdefault(cid, "%s: %s" % (cid, str(snap.get("error", ""))[:120]))
        elif snap.get("status") == "CRITICAL":
            criticals.setdefault(cid, "%s: risk=%s factors=%s" % (
                cid, snap.get("risk_score"), snap.get("risk_factors")))
    summary["patterns"].append({"kind": "pipeline_error", "count": len(errors)})
    summary["patterns"].append({"kind": "monitor_critical", "count": len(criticals)})
    if len(errors) >= min_occurrences:
        p = draft_proposal(
            "pipeline-error-review", "pipeline_steps", "human", "dsh_agent",
            "巡检流水线 PIPELINE_ERROR 复发:健壮性需复查",
            [errors[k] for k in sorted(errors)][-10:],
            signature="pipeline_error")
        if p:
            drafted.append(p)
    if len(criticals) >= min_occurrences:
        p = draft_proposal(
            "monitor-critical-review", "monitor_logic", "human", "kit",
            "monitor CRITICAL 复发:阈值/判据需复查",
            [criticals[k] for k in sorted(criticals)][-10:],
            signature="monitor_critical")
        if p:
            drafted.append(p)

    # 3. friction 软失败信号(2026-09-09 经验沉淀):用户打断/工具错误/审批否决。
    # 轮级行由 dsh-md-friction 插件写 events/friction.jsonl,毫秒时间戳同 flowgate;
    # 这类信号此前完全无落点——回答方向错了被 Esc、工具反复报错、ops-gate 被否决,
    # 都是"静默质量失败",攒够 min_occurrences 即晋升提案。
    def _fcnt(row, key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    friction = []
    for row in read_jsonl(FRICTION_LOG):
        if row.get("kind") != "turn-friction":
            continue
        try:
            t = float(row.get("t", 0)) / 1000.0  # 毫秒时间戳;坏行跳过
        except (TypeError, ValueError):
            continue
        if window_start <= t <= now + 3600:
            row["t_sec"] = t
            friction.append(row)
    interrupt_turns = [r for r in friction if _fcnt(r, "interrupts") > 0]
    toolerr_turns = [r for r in friction if _fcnt(r, "tool_errors") > 0]
    denial_turns = [r for r in friction if _fcnt(r, "denials") > 0]
    summary["patterns"].append({"kind": "friction-interrupt", "count": len(interrupt_turns)})
    summary["patterns"].append({"kind": "friction-tool-error", "count": len(toolerr_turns)})
    summary["patterns"].append({"kind": "friction-denial", "count": len(denial_turns)})
    for kind, rows in (("friction-interrupt", interrupt_turns),
                       ("friction-tool-error", toolerr_turns),
                       ("friction-denial", denial_turns)):
        for r in rows:
            memory.record_atom(kind, "turn %s session %s" % (r.get("turn"), r.get("session")),
                               refs=["events/friction.jsonl#%s" % r.get("t", "?")])
    if len(interrupt_turns) >= min_occurrences:
        p = draft_proposal(
            "friction-interrupt-review", "persona_wording", "auto", "dsh_agent",
            "用户打断频发:回答方向/详略未命中意图,交互策略需复查",
            ["%s: turn %s aborted(tools=%s err=%s)" % (
                time.strftime("%m-%d %H:%M", time.localtime(r["t_sec"])),
                r.get("turn"), _fcnt(r, "tools"), _fcnt(r, "tool_errors"))
             for r in interrupt_turns[-10:]],
            signature="friction-interrupt", metric_count=len(interrupt_turns))
        if p:
            drafted.append(p)
    if len(toolerr_turns) >= min_occurrences:
        p = draft_proposal(
            "friction-tool-error-review", "pipeline_steps", "auto", "dsh_agent",
            "工具错误轮频发:工具用法/参数生成或工具健壮性需复查",
            ["%s: turn %s err=%s codes=%s" % (
                time.strftime("%m-%d %H:%M", time.localtime(r["t_sec"])),
                r.get("turn"), _fcnt(r, "tool_errors"),
                ",".join(str(c) for c in (r.get("failed_tools") or [])[:5]))
             for r in toolerr_turns[-10:]],
            signature="friction-tool-error", metric_count=len(toolerr_turns))
        if p:
            drafted.append(p)
    if len(denial_turns) >= min_occurrences:
        p = draft_proposal(
            "friction-denial-review", "tool_permissions", "auto", "dsh_agent",
            "审批否决频发:工具权限边界或请求理由说明需复查",
            ["%s: turn %s denials=%s" % (
                time.strftime("%m-%d %H:%M", time.localtime(r["t_sec"])),
                r.get("turn"), _fcnt(r, "denials"))
             for r in denial_turns[-10:]],
            signature="friction-denial", metric_count=len(denial_turns))
        if p:
            drafted.append(p)

    # 4. kit 恢复策略库:结果入账 + 连败压制提案
    # 单源故障隔离:kit db 异常绝不影响其他观测源的 rebuild/summary(一轮对抗 B1)
    try:
        observe_kit(summary, drafted)
    except Exception as e:
        print("[OBSERVE] warn: observe_kit 跳过: %s" % e, file=sys.stderr)

    # 训练记忆:重建 L2 场景层与 L3 SOP(确定性全量)
    memory.rebuild()    # 健康分:违规与错误越少越健康(1.0=无症状)
    # 行为策略后验(2026-09-17):active 策略连续 EVAL_KEEP 轮窗口计数不降→
    # 自动撤回+移出 persona+开人工闸。单源故障隔离同 observe_kit。
    try:
        bstrat.verify({"repeat-question": len(repeats),
                       "user-correction": len(corrections),
                       "friction-interrupt": len(interrupt_turns),
                       "friction-tool-error": len(toolerr_turns),
                       "friction-denial": len(denial_turns)},
                      enqueue_gate=_append_gate_request)
    except Exception as e:
        print("[OBSERVE] warn: 行为策略后验跳过: %s" % e, file=sys.stderr)
    total = sum(p["count"] for p in summary["patterns"])
    summary["health"] = round(1.0 / (1.0 + total), 3)
    summary["drafted"] = drafted
    return summary


def _slug_safe(text):
    """slug 只留 [A-Za-z0-9_-]:防 db 里畸形 strategy/cause 名带 / 或 .. 逃逸目录(一轮对抗 B3)。
    py3.6 兼容:str.isascii() 是 3.7+,系统 python3=3.6 会 AttributeError(二轮对抗)。
    超长名截断+哈希尾巴:否则提案目录名爆 ENAMETOOLONG 被静默吞掉,该连败永久失声(六轮对抗)。"""
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    s = "".join(c if c in safe else "_" for c in str(text)) or "x"
    if len(s) > 64:
        s = "%s-%s" % (s[:48], hashlib.sha256(s.encode("utf-8")).hexdigest()[:12])
    return s


def _pair_slug(sid, cause):
    """(sid, cause) → 提案 slug。白名单保留 '-'/'_' 使裸拼接有二义
    ((A,B-C) 与 (A-B,C) 同 slug,后者每轮刷新覆盖前者,前者连败永不落地,
    九轮对抗):附原始对( NUL 分隔)哈希尾巴保唯一。"""
    pair = hashlib.sha256(("%s\x00%s" % (sid, cause)).encode("utf-8")).hexdigest()[:8]
    return "strategy-weight-%s-%s-%s" % (_slug_safe(sid), _slug_safe(cause), pair)


def _penalty_value(entry):
    """strategy_adjustments 条目 → 有效 penalty。bool 不入 float(True)=1.0 的漏,
    负值(变相给策略加分)按 0 计,nan/inf 可穿透 max() 造成"标题上调实为删条目"的
    误标 diff,一概按 0 计(五轮对抗);与 kit recover_agent._penalty 同口径。"""
    if not isinstance(entry, dict):
        return 0.0
    v = entry.get("penalty", 0)
    if isinstance(v, bool):
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return max(f, 0.0)


def _proposal_applied(slug, date=None):
    """LEDGER 里指定日(默认今日)同名提案是否已应用(草稿刷新防线:已应用的绝不动)。"""
    date_slug = "%s-%s" % (date or time.strftime("%Y-%m-%d"), slug)
    try:
        for line in open(os.path.join(paths.memory_dir(), "LEDGER.jsonl"),
                         encoding="utf-8", errors="replace"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("slug") == date_slug \
                    and row.get("result") in ("APPLIED", "AUTO_APPLIED"):
                return True
    except OSError:
        pass
    return False


def _recent_outcomes(history, sid, cause):
    """(sid,cause) 在近 KIT_STREAK_WINDOW_DAYS 天内的已结账 outcome 序列(ts 升序)。
    未来时间戳(>1h 容差)排除,口径与连败窗口一致;畸形条目/容器静默跳过。"""
    if not isinstance(history, list):
        return []
    cutoff = time.time() - KIT_STREAK_WINDOW_DAYS * DAY_SEC
    now = time.time()
    out = []
    for h in history:
        if not isinstance(h, dict) or h.get("outcome") not in ("success", "fail"):
            continue
        if h.get("strategy") != sid or h.get("root_cause") != cause:
            continue
        try:
            ts = time.mktime(time.strptime(str(h.get("ts", "")), "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            continue
        if cutoff <= ts <= now + 3600:
            out.append((str(h.get("ts", "")), h["outcome"]))
    out.sort(key=lambda x: x[0])  # 只按 ts 排:稳定排序保文件序,outcome 不得当次序键
    return [o for _, o in out]


def _tail_streak(outcomes):
    """尾部同向连续计数:("fail"|"success"|None, n)。"""
    kind, n = None, 0
    for o in reversed(outcomes):
        if kind is None:
            kind = o
        if o != kind:
            break
        n += 1
    return kind, n


def _invalidate_unapplied_draft(slug, date=None):
    """作废指定日(默认今日)未应用草稿:清空 diff.patch。证据反转/出窗/被驱逐后,旧 diff 的行
    上下文可能仍干净可应用,会把已不成立的压制/减压打进去(七轮对抗 d2)。
    apply 端对空 diff fail-closed("diff.patch 为空" REJECTED 入 LEDGER);
    已应用(APPLIED/AUTO_APPLIED)的绝不动(四轮 H2 审计留痕)。"""
    if _proposal_applied(slug, date):
        return
    patch = os.path.join(PROPOSALS, "%s-%s" % (date or time.strftime("%Y-%m-%d"), slug),
                         "diff.patch")
    try:
        if os.path.isfile(patch) and os.path.getsize(patch) > 0:
            with open(patch, "w", encoding="utf-8"):
                pass
    except OSError:
        pass


def _sweep_stale_drafts(touched):
    """收尾横扫:今日 strategy-weight 草稿凡不在 touched(本轮拿到新 diff)的一律
    作废(清空 diff.patch)。证据反转/出窗/整对蒸发/db 畸形致证据不可确认时
    fail-closed(七轮 d2/八轮);下一轮证据恢复经同日刷新通道自愈回填。"""
    today = time.strftime("%Y-%m-%d")
    try:
        names = os.listdir(PROPOSALS)
    except OSError:
        return
    for name in names:
        if name.startswith(today + "-strategy-weight-"):
            slug = name[len(today) + 1:]
            if slug not in touched:
                _invalidate_unapplied_draft(slug)


def _strategy_db_diff(db_path, sid, cause, direction, streak):
    """对 .v3.3_strategy_db.json 生成 unified diff,只动 strategy_adjustments 段。

    direction="up":penalty 步进上调(连败压制);"down":步进下调,降到 0 摘除条目
    (连胜减压,四轮对抗 M1)。步进/钳制按磁盘新快照现算,不用调用方旧快照。

    新增时插在 "history" 键之前:strategies 段行数恒定(值原地变),锚点位置
    不随 history 增长漂移;上下文过期则 apply 端 REJECTED(fail-closed),
    下轮观测按新快照重新起草。

    TOCTOU 防线(一轮对抗 C1):db 在 diff 生成前重新从磁盘读,绝不用调用方的
    旧快照——否则 kit 在观测期间结的账会以 +/- 行混进 diff,破坏单写字段纪律。
    非规范化落盘(手改/重复键,round-trip 不一致)直接放弃本轮:整体重序列化
    会把段外改动带进 diff(B5)。
    """
    try:
        old_text = open(db_path, encoding="utf-8").read()
        db = json.loads(old_text)
    except (OSError, ValueError):
        return None
    if not isinstance(db, dict):
        return None
    canonical = json.dumps(db, indent=2)
    if old_text not in (canonical, canonical + "\n"):
        print("[OBSERVE] warn: kit db 落盘非规范化(手改/CRLF/重复键),"
              "本轮不起草策略提案: %s" % db_path, file=sys.stderr)
        return None
    fresh_adj = db.get("strategy_adjustments") or {}
    if not isinstance(fresh_adj, dict):
        return None  # 畸形 adjustments 段(list 等):不猜,放弃本轮(二轮对抗)
    # 连败/连胜证据也按磁盘新快照复核:observe 初读与 diff 落笔之间若尾部已转向
    # (kit 同期结出新账),旧证据起草的压制/减压就是错的(六轮对抗 history 侧 TOCTOU)
    kind, tail = _tail_streak(_recent_outcomes(db.get("history"), sid, cause))
    if direction == "up" and not (kind == "fail" and tail >= KIT_FAIL_STREAK):
        return None
    if direction == "down" and not (kind == "success" and tail >= KIT_SUCCESS_STREAK):
        return None
    key = "%s:%s" % (sid, cause)
    old_pen = _penalty_value(fresh_adj.get(key, {}))
    if direction == "up":
        penalty = round(min(old_pen + KIT_PENALTY_STEP, KIT_PENALTY_MAX), 2)
        if penalty <= old_pen:
            return None  # 已封顶或期间已被应用
        reason = "连败%d次,演化层压制" % streak
    else:
        penalty = round(max(old_pen - KIT_PENALTY_STEP, 0.0), 2)
        if penalty >= old_pen:
            return None  # 无压制可减或期间已被解除
        reason = "连胜%d次,演化层减压" % streak
    adj = dict(fresh_adj)
    if penalty > 0:
        adj[key] = {"penalty": penalty, "reason": reason,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    else:
        adj.pop(key, None)  # 回降到 0:摘除条目,恢复自由竞争
    new_db = {}
    done = False
    for k, v in db.items():
        if k == "history" and "strategy_adjustments" not in db:
            new_db["strategy_adjustments"] = adj
            done = True
        new_db[k] = adj if k == "strategy_adjustments" else v
        if k == "strategy_adjustments":
            done = True
    if not done:
        new_db["strategy_adjustments"] = adj
    # ensure_ascii 必须跟 kit save_db 同口径(默认 True):否则中文 desc 行
    # 整行进 diff,改动行数暴涨超 auto 上界(E2E 实证 19 行被拒)
    new_text = json.dumps(new_db, indent=2) + "\n"
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                fromfile="a/.v3.3_strategy_db.json",
                                tofile="b/.v3.3_strategy_db.json", n=3, lineterm="")
    return "\n".join(diff) + "\n"


# ---------------------------------------------------------------------------
# L2 结构自进化(2026-09-03):耗尽根因自动起草新恢复策略(install 金丝雀),
# 金丝雀实账达标自动晋升(promote)/连败或饿死自动降级(demote+墓碑)。
# 演化通道只写数据(策略库 JSON);模板/执行器/沙盒为人工交付的裁判文件,
# 在 POLICY forbidden_paths 内,通道永不可碰。
# ---------------------------------------------------------------------------


def _evidence_spec():
    """POLICY evidence_classes.new_specialist_script.evidence。
    无该类/畸形 → None:整条 L2 通道 fail-closed 全关(总开关)。"""
    try:
        pol = json.load(open(POLICY_PATH, encoding="utf-8"))
        cls = pol.get("evidence_classes", {}).get("new_specialist_script")
        if not isinstance(cls, dict):
            return None
        ev = cls.get("evidence")
        return ev if isinstance(ev, dict) else None
    except Exception:
        return None


def _specialist_templates():
    """模板注册表(人工交付的考题与答案)。加载失败 → None(fail-closed)。"""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import specialist_templates
        return specialist_templates
    except Exception:
        return None


def _specialist_swarm():
    """蜂群研究模块(耗尽触发的联网研判)。加载失败 → None:起草回退盲选,
    行为与蜂群接入前逐字节等价(蜂群是增强,不是依赖)。"""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import specialist_swarm
        return specialist_swarm
    except Exception:
        return None


def _canary_stats(history, sid):
    """金丝雀实账:(settled, wins, fails, 尾部连败, 最早 ts 秒)。
    口径与 apply_proposal._canary_stats 严格一致(可解析、非未来 >1h、ts 升序)。"""
    if not isinstance(history, list):
        return 0, 0, 0, 0, None
    now = time.time()
    rows = []
    for h in history:
        if not isinstance(h, dict) or h.get("strategy") != sid \
                or h.get("outcome") not in ("success", "fail"):
            continue
        try:
            ts = time.mktime(time.strptime(str(h.get("ts", "")), "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            continue
        if ts > now + 3600:
            continue
        rows.append((ts, h["outcome"]))
    rows.sort(key=lambda x: x[0])
    wins = fails = 0
    for _, o in rows:
        if o == "success":
            wins += 1
        else:
            fails += 1
    streak = 0
    for _, o in reversed(rows):
        if o != "fail":
            break
        streak += 1
    return wins + fails, wins, fails, streak, (rows[0][0] if rows else None)


def _canary_install_ts(sid):
    """LEDGER 里该 sid 最早一笔 install AUTO_APPLIED 的 ts(秒,或 None)。
    僵尸金丝雀(零实账)的饿死锚点:history 无账可查时以安装时刻计龄,
    防永生 canary(20 轮对抗;与 apply_proposal._canary_install_ts 同口径)。"""
    best = None
    now = time.time()
    try:
        fh = open(memory.LEDGER, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) \
                    or row.get("result") != "AUTO_APPLIED" \
                    or row.get("op") != "install" \
                    or row.get("specialist_id") != sid:
                continue
            try:
                ts = time.mktime(time.strptime(str(row.get("ts", "")), "%Y-%m-%dT%H:%M:%S"))
            except (TypeError, ValueError, OverflowError):
                continue
            if ts > now + 3600:
                continue
            if best is None or ts < best:
                best = ts
    return best


def _canary_starved(ev, settled, anchor_ts):
    """饿死判定:超 canary_max_age_days 仍未攒满 canary_attempts 笔实账。
    anchor_ts 为最早实账 ts 或(零账时)LEDGER 安装 ts;皆无 → 不饿死。"""
    return anchor_ts is not None and settled < ev.get("canary_attempts", 4) \
        and (time.time() - anchor_ts) >= ev.get("canary_max_age_days", 30) * DAY_SEC


_LINEAGE_KEYS = ("name", "desc", "for", "canary", "rule", "template", "params")


def _lineage_ok(entry, sid, st):
    """血统(与 apply_proposal._lineage_ok 同口径):稳定字段须与模板产物一致;
    success_rate/uses 随 kit 结账合法漂移,不参与比对。血统不正的条目
    promote/demote 都不起草——证据门也会拒,不浪费草稿位(20 轮对抗)。"""
    if not isinstance(entry, dict):
        return False
    expect = st.instantiate(entry.get("template"), sid, entry.get("params"))
    if expect is None:
        return False
    return all(entry.get(k) == expect.get(k) for k in _LINEAGE_KEYS)


def _live_canary_causes(strategies, st):
    """活金丝雀覆盖的根因集合(抑制同根因重起草)。"""
    causes = set()
    if not isinstance(strategies, dict):
        return causes
    for entry in strategies.values():
        if isinstance(entry, dict) and entry.get("canary") is True:
            tpl = st.TEMPLATES.get(entry.get("template"))
            if tpl:
                causes.update(tpl.get("root_causes", []))
    return causes


def _fresh_tombstone_causes(adjustments, ev):
    """未过期墓碑覆盖的根因集合(demote_cooldown_days 内抑制重起草)。
    fail-closed(20 轮对抗):ts 解析失败/未来 ts 一律按未过期处理——
    畸形墓碑不该成为重起草许可证。"""
    causes = set()
    if not isinstance(adjustments, dict):
        return causes
    cooldown = ev.get("demote_cooldown_days", 7) * DAY_SEC
    now = time.time()
    for key, adj in adjustments.items():
        if not isinstance(adj, dict) or adj.get("tombstone") is not True:
            continue
        try:
            ts = time.mktime(time.strptime(str(adj.get("ts", "")), "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            ts = now  # 解析失败按未过期处理(fail-closed)
        if now - ts <= cooldown and ":" in str(key):  # 未来 ts(负龄)同样未过期
            causes.add(str(key).split(":", 1)[1])
    return causes


def _exhausted_causes(db, history, adjustments, ev, st):
    """耗尽根因:14d 窗内非金丝雀 settled fail ≥ canary_attempts,且窗内试过的
    非金丝雀策略全被压制(penalty>0 或连败尾 ≥ KIT_FAIL_STREAK),
    且无活金丝雀、无未过期墓碑。全部按传入快照现算。"""
    if not isinstance(history, list):
        return []
    strategies = db.get("strategies") if isinstance(db, dict) else None
    if not isinstance(strategies, dict):
        return []
    need = ev.get("canary_attempts", 4)
    cutoff = time.time() - KIT_STREAK_WINDOW_DAYS * DAY_SEC
    now = time.time()
    fails = {}
    tried = {}
    for h in history:
        if not isinstance(h, dict) or h.get("outcome") not in ("success", "fail"):
            continue
        try:
            ts = time.mktime(time.strptime(str(h.get("ts", "")), "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not (cutoff <= ts <= now + 3600):
            continue
        cause = h.get("root_cause")
        sid = h.get("strategy")
        if not isinstance(cause, str) or not isinstance(sid, str):
            continue
        entry = strategies.get(sid)
        if isinstance(entry, dict) and entry.get("canary") is True:
            continue  # 金丝雀自己的账不算"标准工具箱耗尽"
        tried.setdefault(cause, set()).add(sid)
        if h["outcome"] == "fail":
            fails[cause] = fails.get(cause, 0) + 1
    canary_causes = _live_canary_causes(strategies, st)
    tomb_causes = _fresh_tombstone_causes(adjustments, ev)
    out = []
    for cause in sorted(fails):
        if fails[cause] < need or cause in canary_causes or cause in tomb_causes:
            continue
        sids = tried.get(cause) or set()
        if not sids:
            continue
        suppressed = True
        for sid in sids:
            key = "%s:%s" % (sid, cause)
            kind, tail = _tail_streak(_recent_outcomes(history, sid, cause))
            if _penalty_value(adjustments.get(key, {})) <= 0 \
                    and not (kind == "fail" and tail >= KIT_FAIL_STREAK):
                suppressed = False
                break
        if suppressed:
            out.append(cause)
    return out


def _next_free_sid(strategies):
    """下一个空闲单字母策略 id(白名单 ^[A-Z][A-Z0-9_]{0,15}$ 的最保守子集)。"""
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if c not in strategies:
            return c
    return None


def _specialist_db_diff(db_path, op, sid, cause, template_id, params, ev):
    """install/promote/demote 的 unified diff,只动 strategies 段。

    纪律同 _strategy_db_diff:落笔前新读盘(TOCTOU)、规范化回环校验、
    证据按磁盘新快照复核(与 apply 门 _check_evidence 同口径同阈值)、
    ensure_ascii 默认。复核不过 → None(烧掉本轮,不烧草稿去重位)。
    """
    try:
        old_text = open(db_path, encoding="utf-8").read()
        db = json.loads(old_text)
    except (OSError, ValueError):
        return None
    if not isinstance(db, dict):
        return None
    canonical = json.dumps(db, indent=2)
    if old_text not in (canonical, canonical + "\n"):
        print("[OBSERVE] warn: kit db 落盘非规范化,本轮不起草 specialist 提案: %s"
              % db_path, file=sys.stderr)
        return None
    strategies = db.get("strategies")
    if not isinstance(strategies, dict):
        return None
    st = _specialist_templates()
    if st is None:
        return None
    new_strategies = dict(strategies)
    if op == "install":
        if sid in strategies:
            return None
        adj = db.get("strategy_adjustments") or {}
        if not isinstance(adj, dict):
            return None
        if cause not in _exhausted_causes(db, db.get("history"), adj, ev, st):
            return None  # 新快照下该根因已不再耗尽(期间结了胜账/墓碑生效等)
        entry = st.instantiate(template_id, sid, params)
        if entry is None:
            return None
        new_strategies[sid] = entry
    elif op == "promote":
        old_entry = strategies.get(sid)
        if not isinstance(old_entry, dict) or old_entry.get("canary") is not True:
            return None
        if not _lineage_ok(old_entry, sid, st):
            return None
        settled, wins, fails, streak, _ = _canary_stats(db.get("history"), sid)
        posterior = (wins + 1.0) / (wins + fails + 2.0)
        if settled < ev.get("canary_attempts", 4) \
                or wins < ev.get("canary_min_wins", 3) \
                or posterior < ev.get("min_posterior", 0.5):
            return None
        if streak >= ev.get("demote_fail_streak", 3):
            return None  # 统计反转(尾部连败触降级线):与 apply 门同口径拒(20 轮)
        new_strategies[sid] = {k: v for k, v in old_entry.items() if k != "canary"}
    elif op == "demote":
        old_entry = strategies.get(sid)
        if not isinstance(old_entry, dict) or old_entry.get("canary") is not True:
            return None
        if not _lineage_ok(old_entry, sid, st):
            return None
        settled, _, _, streak, first_ts = _canary_stats(db.get("history"), sid)
        anchor = first_ts or _canary_install_ts(sid)
        if streak < ev.get("demote_fail_streak", 3) \
                and not _canary_starved(ev, settled, anchor):
            return None
        del new_strategies[sid]
    else:
        return None
    new_db = dict(db)
    new_db["strategies"] = new_strategies
    new_text = json.dumps(new_db, indent=2) + "\n"
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                fromfile="a/.v3.3_strategy_db.json",
                                tofile="b/.v3.3_strategy_db.json", n=3, lineterm="")
    return "\n".join(diff) + "\n"


def _tombstone_db_diff(db_path, sid, cause):
    """降级配套墓碑(auto 类 strategy_weight_tuning,只写 strategy_adjustments 段)。
    纪律同 _strategy_db_diff;已有墓碑 → None(不重复起草)。"""
    try:
        old_text = open(db_path, encoding="utf-8").read()
        db = json.loads(old_text)
    except (OSError, ValueError):
        return None
    if not isinstance(db, dict):
        return None
    canonical = json.dumps(db, indent=2)
    if old_text not in (canonical, canonical + "\n"):
        return None
    fresh_adj = db.get("strategy_adjustments") or {}
    if not isinstance(fresh_adj, dict):
        return None
    key = "%s:%s" % (sid, cause)
    if isinstance(fresh_adj.get(key), dict) and fresh_adj[key].get("tombstone") is True:
        return None
    adj = dict(fresh_adj)
    adj[key] = {"penalty": KIT_PENALTY_MAX, "reason": "金丝雀降级墓碑",
                "tombstone": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    new_db = {}
    done = False
    for k, v in db.items():
        if k == "history" and "strategy_adjustments" not in db:
            new_db["strategy_adjustments"] = adj
            done = True
        new_db[k] = adj if k == "strategy_adjustments" else v
        if k == "strategy_adjustments":
            done = True
    if not done:
        new_db["strategy_adjustments"] = adj
    new_text = json.dumps(new_db, indent=2) + "\n"
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                fromfile="a/.v3.3_strategy_db.json",
                                tofile="b/.v3.3_strategy_db.json", n=3, lineterm="")
    return "\n".join(diff) + "\n"


def _write_specialist_draft(slug, op, sid, title, evidence_lines, diff, drafted):
    """起草或同日刷新 specialist 提案(meta 带 op/specialist_id 供证据门校验)。
    同日未应用即刷新(diff+meta+标题同步重写);已应用(APPLIED/AUTO_APPLIED)绝不动。"""
    p = draft_proposal(slug, "new_specialist_script", "evidence", "kit", title,
                       evidence_lines, signature="recovery_consecutive_fail")
    if p:
        open(os.path.join(p, "diff.patch"), "w", encoding="utf-8").write(diff)
        meta_path = os.path.join(p, "meta.json")
        meta = json.load(open(meta_path, encoding="utf-8"))
        meta["op"] = op
        meta["specialist_id"] = sid
        json.dump(meta, open(meta_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        drafted.append(p)
    elif not _proposal_applied(slug):
        pdir = os.path.join(PROPOSALS, "%s-%s" % (time.strftime("%Y-%m-%d"), slug))
        try:
            with open(os.path.join(pdir, "diff.patch"), "w", encoding="utf-8") as f:
                f.write(diff)
            meta_path = os.path.join(pdir, "meta.json")
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            meta["op"] = op
            meta["specialist_id"] = sid
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            pmd = os.path.join(pdir, "proposal.md")
            with open(pmd, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines and lines[0].rstrip("\n") != "# " + title:
                lines[0] = "# %s\n" % title
                with open(pmd, "w", encoding="utf-8") as f:
                    f.writelines(lines)
        except (OSError, ValueError):
            pass  # 手改坏的草稿目录不得打崩观测;收尾横扫兜底作废


def _sweep_specialist_drafts(touched):
    """specialist-* 草稿凡本轮没拿到新 diff 的一律作废(清空 diff.patch)。
    与 _sweep_stale_drafts 同纪律:证据反转/出窗/条件不再成立时 fail-closed。
    旧日期草稿同样作废(20 轮对抗):隔夜未应用的 diff 基于过期快照,不允许
    滞留待应用;已应用(APPLIED/AUTO_APPLIED)的绝不动。"""
    today = time.strftime("%Y-%m-%d")
    try:
        names = os.listdir(PROPOSALS)
    except OSError:
        return
    for name in names:
        if len(name) <= 11 or name[4] != "-" or name[7] != "-" or name[10] != "-":
            continue
        date, slug = name[:10], name[11:]
        if not slug.startswith("specialist-"):
            continue
        if date == today:
            if slug not in touched:
                _invalidate_unapplied_draft(slug)
        else:
            _invalidate_unapplied_draft(slug, date)


def _observe_specialist(summary, drafted, db, db_path, ev):
    """L2 观测:金丝雀横扫(promote/demote+墓碑)+ 耗尽根因起草 install。
    返回本轮拿到新 diff 的 specialist slug 集合(收尾横扫用)。"""
    touched = set()
    st = _specialist_templates()
    if st is None:
        return touched
    history = db.get("history")
    strategies = db.get("strategies")
    if not isinstance(history, list) or not isinstance(strategies, dict):
        return touched
    adjustments = db.get("strategy_adjustments") or {}
    if not isinstance(adjustments, dict):
        return touched
    allowed = ev.get("allowed_templates", [])
    need = ev.get("canary_attempts", 4)
    min_wins = ev.get("canary_min_wins", 3)
    demote_streak = ev.get("demote_fail_streak", 3)

    # 1) 金丝雀横扫:逐 canary 策略算 settled 账(口径同 apply 门)。
    # 降级优先于晋升(20 轮对抗):连败尾触线即降级,哪怕累计账面仍达晋升线——
    # 3 胜后 3 连败的反转序列绝不允许洗白晋升。血统不正两者都不起草。
    for sid in sorted(strategies):
        entry = strategies[sid]
        if not isinstance(entry, dict) or entry.get("canary") is not True:
            continue
        if not _lineage_ok(entry, sid, st):
            continue
        settled, wins, fails, streak, first_ts = _canary_stats(history, sid)
        posterior = (wins + 1.0) / (wins + fails + 2.0)
        anchor = first_ts or _canary_install_ts(sid)
        if streak >= demote_streak or _canary_starved(ev, settled, anchor):
            op = "demote"
            reason = ("连败尾 %d 次" % streak if streak >= demote_streak
                      else "超 %d 天未满 %d 账(饿死)" % (
                          ev.get("canary_max_age_days", 30), need))
            slug = "specialist-demote-%s" % _slug_safe(sid)
            title = "金丝雀 %s %s:降级移除" % (sid, reason)
        elif settled >= need and wins >= min_wins \
                and posterior >= ev.get("min_posterior", 0.5):
            op = "promote"
            slug = "specialist-promote-%s" % _slug_safe(sid)
            title = "金丝雀 %s 实账达标(%d 账 %d 胜,后验 %.2f):晋升正式策略" % (
                sid, settled, wins, posterior)
        else:
            continue
        diff = _specialist_db_diff(db_path, op, sid, None, None, None, ev)
        if not diff:
            continue  # 新快照复核拒绝:落入收尾横扫
        touched.add(slug)
        evlines = ["金丝雀 %s 实账:settled=%d wins=%d fails=%d 连败尾=%d 后验=%.3f"
                   % (sid, settled, wins, fails, streak, posterior)]
        _write_specialist_draft(slug, op, sid, title, evlines, diff, drafted)
        # 知识库闭环:金丝雀结局回灌 repair_kb,后续蜂群 question 带历史结局
        sw = _specialist_swarm()
        if sw is not None:
            tpl_causes = st.TEMPLATES.get(entry.get("template"), {}).get(
                "root_causes", [])
            sw.append_episode({"type": "canary_outcome", "sid": sid, "op": op,
                               "root_cause": tpl_causes[0] if tpl_causes else None,
                               "settled": settled, "wins": wins, "fails": fails,
                               "fail_streak": streak,
                               "posterior": round(posterior, 3)})
        if op != "demote":
            continue
        # 降级配套墓碑(auto 类):模板覆盖的每个根因写一条,冷却期抑制重起草
        tpl = st.TEMPLATES.get(entry.get("template"), {})
        for tcause in tpl.get("root_causes", []):
            tslug = "specialist-tombstone-%s-%s" % (_slug_safe(sid), _slug_safe(tcause))
            tdiff = _tombstone_db_diff(db_path, sid, tcause)
            if not tdiff:
                continue
            touched.add(tslug)
            ttitle = "金丝雀 %s 降级墓碑:%s 冷却 %d 天抑制重起草" % (
                sid, tcause, ev.get("demote_cooldown_days", 7))
            p = draft_proposal(tslug, "strategy_weight_tuning", "auto", "kit", ttitle,
                               evlines, signature="recovery_consecutive_fail")
            if p:
                open(os.path.join(p, "diff.patch"), "w", encoding="utf-8").write(tdiff)
                drafted.append(p)
            elif not _proposal_applied(tslug):
                pdir = os.path.join(PROPOSALS, "%s-%s" % (
                    time.strftime("%Y-%m-%d"), tslug))
                try:
                    with open(os.path.join(pdir, "diff.patch"), "w",
                              encoding="utf-8") as f:
                        f.write(tdiff)
                except OSError:
                    pass

    # 2) 耗尽根因起草 install(一因一模板;slug 按 (模板,根因) 去重)。
    # sid 记账(20 轮对抗):一轮多根因耗尽时逐次占位,不再给两份草稿分同一 sid。
    # 蜂群增强(2026-09-05):首轮耗尽根因先跑联网研究,产物确定性映射到模板空间
    # (选模板+网格参数);禁用/节流/失败 → swarm=None,盲选首参与接入前逐字节等价。
    allocated = set()
    swarm_used = False  # 单轮最多一次蜂群(POLICY 另有冷却+日额度双闸)
    for cause in _exhausted_causes(db, history, adjustments, ev, st):
        tids = [t for t in st.templates_for_cause(cause) if t in allowed]
        if not tids:
            continue
        template_id = tids[0]
        params = st.first_params(template_id)
        swarm = None
        if not swarm_used:
            sw = _specialist_swarm()
            if sw is not None:
                swarm = sw.swarm_pick(cause, ev, st, allowed)
            if swarm is not None:
                swarm_used = True
                template_id = swarm["template_id"]
                params = swarm["params"]
        sid = _next_free_sid(set(strategies) | allocated)
        if sid is None:
            break  # 26 字母用尽:不再起草,等人工清理
        pair = hashlib.sha256(("%s\x00%s" % (template_id, cause)
                               ).encode("utf-8")).hexdigest()[:8]
        slug = "specialist-install-%s-%s" % (_slug_safe(cause), pair)
        diff = _specialist_db_diff(db_path, "install", sid, cause,
                                   template_id, params, ev)
        if not diff:
            continue
        allocated.add(sid)
        touched.add(slug)
        title = "根因 %s 标准工具箱耗尽:安装金丝雀策略 %s(模板 %s)" % (
            cause, sid, template_id)
        evlines = ["根因 %s 近 %d 天非金丝雀 settled fail ≥ %d,全部可选策略被压制"
                   % (cause, KIT_STREAK_WINDOW_DAYS, need)]
        if swarm is not None:
            r = swarm.get("research") or {}
            evlines.append("蜂群研判: %s" % str(r.get("verdict", ""))[:160])
            evlines.append("蜂群证据: 候选策略 %d 个,独立来源 %d 个%s" % (
                len(r.get("strategies") or []), len(r.get("sources") or []),
                "(低置信,回退模板首参)" if swarm.get("fallback") else ""))
        _write_specialist_draft(slug, "install", sid, title, evlines, diff, drafted)
        if swarm is not None and swarm.get("novel"):
            _draft_swarm_novel(cause, swarm, drafted)
    return touched


def _draft_swarm_novel(cause, swarm, drafted):
    """蜂群研判指向新机制(对全部人工模板零命中)→ human_review 提案。
    模板永远人工交付:本草稿只携带研究报告与候选,不带可应用 diff;
    slug 用 swarm-research- 前缀,不属于 specialist-* 收尾横扫范围。"""
    r = swarm.get("research") or {}
    slug = "swarm-research-%s" % _slug_safe(cause)
    title = ("根因 %s 工具箱耗尽:蜂群研判指向新机制(现有模板零覆盖),"
             "请人工评审并交付新模板" % cause)
    evlines = ["蜂群 verdict: %s" % str(r.get("verdict", ""))[:300]]
    for s in (r.get("strategies") or [])[:4]:
        if isinstance(s, dict):
            evlines.append("候选: %s(score=%s verified=%s) 证据: %s" % (
                s.get("name"), s.get("score"), s.get("verified"),
                "; ".join(str(u) for u in (s.get("evidence") or []))[:200]))
    srcs = [str(x.get("url")) for x in (r.get("sources") or [])
            if isinstance(x, dict) and x.get("url")]
    if srcs:
        evlines.append("来源: %s" % "; ".join(srcs[:8]))
    p = draft_proposal(slug, "new_specialist_script", "human", "kit", title,
                       evlines, signature="recovery_consecutive_fail")
    if p:
        drafted.append(p)


def observe_kit(summary, drafted):
    """kit 策略库观测:已结账结果记 atoms;同 (策略,根因) 连败起压制提案。

    单写字段纪律:success_rate/history 由 kit 自己结账户后验回写,本函数
    的提案只许写 strategy_adjustments 段(与 POLICY auto 类声明同口径)。
    kit 文件缺失/损坏时静默跳过,绝不影响其他观测源。
    """
    try:
        db_path = _kit_db_path()
        db = json.load(open(db_path, encoding="utf-8"))
    except Exception:
        _sweep_stale_drafts(set())  # 证据不可确认:fail-closed 作废同日草稿(八轮)
        _sweep_specialist_drafts(set())
        return
    if not isinstance(db, dict):
        _sweep_stale_drafts(set())
        _sweep_specialist_drafts(set())
        return
    # 损坏但合法的 db(history 为 null、条目缺字段/非串)不得打崩整轮观测(一轮对抗 B1)
    raw_history = db.get("history")
    if not isinstance(raw_history, list):
        _sweep_stale_drafts(set())
        _sweep_specialist_drafts(set())
        return
    history = [h for h in raw_history if isinstance(h, dict)
               and isinstance(h.get("strategy"), str)
               and isinstance(h.get("root_cause"), str)]
    settled = [h for h in history if h.get("outcome") in ("success", "fail")]
    # 手改/还原过的 db 文件序可能乱:按 ts 排,连败"尾部"才真实(B7)
    settled.sort(key=lambda h: str(h.get("ts", "")))
    for h in settled:
        memory.record_atom(
            "recovery-outcome",
            "%s:%s:%s@%s" % (h.get("strategy"), h.get("root_cause"),
                             h.get("outcome"), h.get("case_id", "?")),
            refs=["strategy_db#%s#%s#%s" % (h.get("ts", "?"), h.get("strategy", "?"),
                                            h.get("case_id", "?"))])  # case_id 入 refs:同秒异 case 不去重(B2)
    summary["patterns"].append({"kind": "recovery-outcome", "count": len(settled)})

    # 连败/连胜检测:同一 (策略,根因) 近 KIT_STREAK_WINDOW_DAYS 天已结账记录的
    # 尾部同向连续;更老的账不参与——几周前的失败不应永久压制策略(四轮对抗 M1)。
    # 窗口过滤/未来 ts 排除(五轮)/排序统一走 _recent_outcomes,与 _strategy_db_diff
    # 落笔前的证据复核严格同口径(六轮对抗)
    # 金丝雀策略排除在本循环外:其奖惩由 L2 specialist 横扫(promote/demote+墓碑)
    # 全权管辖,普通压制提案会和 demote 删键打架(L2 试点纪律)
    strategies = db.get("strategies", {})
    canary_sids = {s for s, e in strategies.items()
                   if isinstance(e, dict) and e.get("canary") is True} \
        if isinstance(strategies, dict) else set()
    pairs = sorted({(h["strategy"], h["root_cause"]) for h in settled
                    if h["strategy"] not in canary_sids})
    adjustments = db.get("strategy_adjustments", {})
    if not isinstance(adjustments, dict):
        _sweep_stale_drafts(set())
        _sweep_specialist_drafts(set())
        return
    touched = set()  # 本轮拿到新 diff 的 slug(新起草或刷新)
    for sid, cause in pairs:
        tail_kind, tail = _tail_streak(_recent_outcomes(history, sid, cause))
        key = "%s:%s" % (sid, cause)
        old_pen = _penalty_value(adjustments.get(key, {}))
        # slug 提前算:证据不成立的路径也要凭它作废同日残留草稿(七轮对抗 d2)
        slug = _pair_slug(sid, cause)
        if tail_kind == "fail" and tail >= KIT_FAIL_STREAK:
            direction = "up"
        elif tail_kind == "success" and tail >= KIT_SUCCESS_STREAK and old_pen > 0:
            direction = "down"  # 连胜减压:压制须有退出通道,否则学习只进不退(四轮 M1)
        else:
            continue  # 证据不成立:落入收尾横扫作废旧草稿
        # 先生成 diff(内部按磁盘新快照现算步进/钳制+复核尾部证据,不符 → None),
        # 拿不到干净 diff 就不起草——避免烧掉同日同名草稿的去重位(一轮对抗 C1/C3)
        diff = _strategy_db_diff(db_path, sid, cause, direction, tail)
        if not diff:
            continue  # 复核拒绝/快照不干净:同样落入收尾横扫
        touched.add(slug)
        title = ("策略 %s 对 %s 连败 %d 次:penalty 上调" % (sid, cause, tail)
                 if direction == "up" else
                 "策略 %s 对 %s 连胜 %d 次:penalty 下调/解除" % (sid, cause, tail))
        p = draft_proposal(
            slug, "strategy_weight_tuning", "auto", "kit", title,
            ["%s case=%s 策略%s 根因=%s → %s" % (h.get("ts"), h.get("case_id"),
                                                 h.get("strategy"), h.get("root_cause"),
                                                 h.get("outcome"))
             for h in settled if h.get("strategy") == sid][-10:],
            signature="recovery_consecutive_fail")
        if p:
            open(os.path.join(p, "diff.patch"), "w", encoding="utf-8").write(diff)
            drafted.append(p)
        elif not _proposal_applied(slug):
            # 同日草稿存在且未应用:刷新 diff 到最新快照,防上下文过期被
            # REJECTED 后同日无法重起草而卡死(四轮对抗 H2);已应用的绝不动。
            # 方向同日可翻转(连胜↔连败):标题随 diff 同步重写,防"标题减压
            # 实为压制"误导审计/人工回看(八轮对抗)
            pdir = os.path.join(PROPOSALS, "%s-%s" % (time.strftime("%Y-%m-%d"), slug))
            try:
                with open(os.path.join(pdir, "diff.patch"), "w", encoding="utf-8") as f:
                    f.write(diff)
                pmd = os.path.join(pdir, "proposal.md")
                with open(pmd, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if lines and lines[0].rstrip("\n") != "# " + title:
                    lines[0] = "# %s\n" % title
                    with open(pmd, "w", encoding="utf-8") as f:
                        f.writelines(lines)
            except (OSError, ValueError):
                # ValueError 含 UnicodeDecodeError:手改成非法 UTF-8 的 proposal.md
                # 不得逃逸本分支——逃逸会跳过收尾横扫,stale diff 多活一天(九轮对抗)
                pass
    # 收尾横扫:凡本轮没拿到新 diff 的同日未应用 strategy-weight 草稿一律作废——
    # 证据反转/出窗/整对(策略,根因)被驱逐出 history 后,旧 diff 的行上下文可能
    # 仍干净可应用,会把已不成立的压制/减压打进去(七轮对抗 d2)
    _sweep_stale_drafts(touched)
    # L2 结构自进化:金丝雀横扫 + 耗尽根因起草(无 evidence 类 → 全关,只横扫)
    stouched = set()
    ev = _evidence_spec()
    if ev is not None:
        try:
            stouched = _observe_specialist(summary, drafted, db, db_path, ev)
        except Exception as e:
            print("[OBSERVE] warn: specialist 观测跳过: %s" % e, file=sys.stderr)
    _sweep_specialist_drafts(stouched)


def main():
    argv = sys.argv[1:]
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
    min_occ = (int(argv[argv.index("--min-occurrences") + 1])
               if "--min-occurrences" in argv else 3)
    min_occ = max(1, min_occ)  # 0/负数会让空证据也起草提案,钳到 ≥1
    ok, verdict = _should_observe()
    _emit_gate_event(verdict)
    if not ok and "--force" not in argv:
        print(json.dumps({"observe_gate": verdict}, ensure_ascii=False, indent=2))
        return 0
    try:
        summary = observe(days, min_occ)
    except Exception:
        # 24 轮审计:崩溃必须留 FAIL 裁决且不吃掉节流窗
        _emit_gate_event({"verdict": "FAIL", "reason": "observe_crashed"})
        raise
    try:
        # 成功后才盖章(原为先盖章后跑,崩溃会白占一个节流窗);
        # 原子写防撕裂文件把节流闸撬开
        gate_file = OBSERVE_GATE_FILE or os.path.join(paths.state_dir(), "observe_gate.json")
        os.makedirs(os.path.dirname(gate_file), exist_ok=True)
        tmp = "%s.tmp.%d" % (gate_file, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_run_ts": time.time()}, f)
        os.replace(tmp, gate_file)
    except Exception:
        pass
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
