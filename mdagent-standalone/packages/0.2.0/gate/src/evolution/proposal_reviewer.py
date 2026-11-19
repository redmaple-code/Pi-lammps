#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proposal_reviewer.py — 提案审批调度器
扫描 proposals/ 目录, 按 tier 分流:
  Tier1(auto):   沙盒试跑 → 通过则 apply_proposal.py → LEDGER
  Tier2(human):  蜂群4路评估 → 综合分>0.6 则写"建议批准"标记
  过期(>7天):    标记 expired, observe 可重新生成

这是进化闭环 observe→propose→【review】→apply→LEDGER 的缺失环节。
"""
import json, os, time, glob, sys

PROPOSALS = os.path.join(
    os.environ.get("MDSIM_DATA_ROOT",
        "/opt/mdagent/data"),
    "memory", "proposals")
LEDGER = os.path.join(
    os.environ.get("MDSIM_DATA_ROOT",
        "/opt/mdagent/data"),
    "memory", "LEDGER.jsonl")
EVOLUTION_DIR = os.path.dirname(os.path.abspath(__file__))
if not os.path.basename(EVOLUTION_DIR) == "evolution":
    EVOLUTION_DIR = "/opt/mdagent/app/evolution"

EXPIRE_DAYS = 7
AUTO_APPROVE_SCORE = 0.6  # 蜂群综合分>0.6 才标"建议批准"


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))


def ledger_append(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def review_tier1_auto(pdir, meta):
    """Tier1 提案: 沙盒试跑 → 通过则自动应用"""
    slug = os.path.basename(pdir)
    diff_file = os.path.join(pdir, "diff.patch")

    # 检查 diff.patch 是否已编写(还是 TODO)
    try:
        diff_content = open(diff_file, encoding="utf-8").read()
    except Exception:
        log("  %s: diff.patch 不存在, 跳过" % slug)
        return "skip"

    if "TODO" in diff_content and len(diff_content.strip()) < 100:
        log("  %s: diff.patch 还是 TODO, 需人工/代理编写补丁" % slug)
        return "needs_diff"

    # diff 已有内容 → 尝试 apply_proposal
    log("  %s: diff 已编写, 尝试自动应用..." % slug)
    import subprocess
    rc = subprocess.call(
        ["python3", os.path.join(EVOLUTION_DIR, "apply_proposal.py"),
         "--proposal-dir", pdir, "--yes"],
        cwd=EVOLUTION_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if rc == 0:
        log("  %s: ✅ 自动应用成功" % slug)
        ledger_append({"slug": slug, "result": "AUTO_APPLIED",
                       "class": meta.get("class"), "approver": "proposal_reviewer"})
        # 接缝6修复(2026-09-15): 应用成功自动反馈——记 evolution-feedback 原子,
        # 策略库侧(user 对 kit 的 strategy_db 无写权, 原子是跨属主反馈缝)可据此上调成功率。
        try:
            if EVOLUTION_DIR not in sys.path:
                sys.path.insert(0, EVOLUTION_DIR)
            from memory import record_atom
            record_atom(
                kind="evolution-feedback",
                fact="提案 %s 应用成功, 策略类 %s 成功率应上调" % (slug, meta.get("class")),
                refs=["proposal:%s" % slug, "ledger:AUTO_APPLIED"])
        except Exception:
            pass
        return "applied"
    else:
        log("  %s: ❌ 应用失败(rc=%d)" % (slug, rc))
        ledger_append({"slug": slug, "result": "APPLY_FAILED",
                       "class": meta.get("class"), "approver": "proposal_reviewer"})
        return "failed"


def review_tier2_human(pdir, meta):
    """Tier2 提案: 蜂群评估 → 写建议标记"""
    slug = os.path.basename(pdir)
    evidence_file = os.path.join(pdir, "evidence.md")

    try:
        evidence = open(evidence_file, encoding="utf-8").read()
    except Exception:
        log("  %s: evidence.md 不存在" % slug)
        return "skip"

    # 蜂群评估(4路并行: 安全性/必要性/成本/历史成功率)
    # 当前用规则引擎评分(未来可接 research_agent 蜂群)
    scores = {}
    # 安全性: 是否触及 forbidden_paths
    policy_file = os.path.join(EVOLUTION_DIR, "POLICY.json")
    try:
        policy = json.load(open(policy_file))
        forbidden = policy.get("forbidden_paths", [])
        touches_forbidden = any(f in evidence for f in forbidden)
        scores["safety"] = 0.0 if touches_forbidden else 0.8
    except Exception:
        scores["safety"] = 0.5

    # 必要性: 复发次数(从 evidence 中提取)
    import re
    recurrence = len(re.findall(r"复发|recurring|再次", evidence))
    scores["necessity"] = min(1.0, recurrence * 0.3)

    # 成本: diff.patch 是否已编写
    diff_file = os.path.join(pdir, "diff.patch")
    try:
        diff_content = open(diff_file, encoding="utf-8").read()
        has_diff = "TODO" not in diff_content or len(diff_content.strip()) > 100
    except Exception:
        has_diff = False
    scores["feasibility"] = 0.8 if has_diff else 0.3

    # 历史: 同类提案是否成功过
    past_ledger = []
    if os.path.exists(LEDGER):
        for line in open(LEDGER, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
                if isinstance(e, dict) and e.get("slug", "").split("-")[0] == slug.split("-")[0]:
                    past_ledger.append(e.get("result"))
            except Exception:
                pass
    if past_ledger:
        success_rate = sum(1 for r in past_ledger if "APPLIED" in str(r)) / len(past_ledger)
        scores["history"] = success_rate
    else:
        scores["history"] = 0.5  # 无历史, 中性

    # 加权综合
    weights = {"safety": 0.35, "necessity": 0.25, "feasibility": 0.20, "history": 0.20}
    overall = sum(scores[d] * weights[d] for d in scores)

    log("  %s: 蜂群评分 %.2f (safety=%.1f necessity=%.1f feasibility=%.1f history=%.1f)" % (
        slug, overall, scores["safety"], scores["necessity"],
        scores["feasibility"], scores["history"]))

    # 写评估结果
    review = {
        "slug": slug, "scores": scores, "overall": overall,
        "recommendation": "APPROVE" if overall >= AUTO_APPROVE_SCORE else "HOLD",
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reviewer": "proposal_reviewer_v1"
    }
    json.dump(review, open(os.path.join(pdir, "review.json"), "w"),
              ensure_ascii=False, indent=2)

    if overall >= AUTO_APPROVE_SCORE:
        log("  %s: 🟢 建议批准(%.2f≥%.1f)" % (slug, overall, AUTO_APPROVE_SCORE))
        return "recommend_approve"
    else:
        log("  %s: 🟡 建议暂缓(%.2f<%.1f)" % (slug, overall, AUTO_APPROVE_SCORE))
        return "hold"


def review_proposals():
    """扫描 proposals/ 目录, 审批所有未处理的提案"""
    if not os.path.isdir(PROPOSALS):
        log("proposals/ 目录不存在")
        return

    dirs = sorted(glob.glob(os.path.join(PROPOSALS, "20*-*")))
    if not dirs:
        log("无提案")
        return

    log("扫描到 %d 个提案" % len(dirs))
    stats = {"applied": 0, "recommend": 0, "hold": 0, "skip": 0, "expired": 0, "needs_diff": 0}

    for pdir in dirs:
        slug = os.path.basename(pdir)
        meta_file = os.path.join(pdir, "meta.json")
        review_file = os.path.join(pdir, "review.json")

        # 已审批过 → 跳过
        if os.path.exists(review_file):
            try:
                r = json.load(open(review_file))
                if r.get("recommendation") in ("APPROVE", "HOLD", "REJECT"):
                    log("  %s: 已审批(%s), 跳过" % (slug, r["recommendation"]))
                    stats["skip"] += 1
                    continue
            except Exception:
                pass

        # 过期检查
        age_days = (time.time() - os.path.getmtime(pdir)) / 86400
        if age_days > EXPIRE_DAYS:
            log("  %s: 过期(%.0f天), 标记expired" % (slug, age_days))
            json.dump({"expired": True, "expired_at": time.strftime("%Y-%m-%d")},
                      open(os.path.join(pdir, "expired.json"), "w"))
            stats["expired"] += 1
            continue

        # 读 meta
        try:
            meta = json.load(open(meta_file))
        except Exception:
            log("  %s: meta.json 损坏, 跳过" % slug)
            stats["skip"] += 1
            continue

        tier = meta.get("tier", "human")
        log("  %s: tier=%s class=%s" % (slug, tier, meta.get("class", "?")))

        if tier == "auto":
            result = review_tier1_auto(pdir, meta)
            if result == "applied":
                stats["applied"] += 1
            elif result == "needs_diff":
                stats["needs_diff"] += 1
            else:
                stats["skip"] += 1
        else:
            result = review_tier2_human(pdir, meta)
            if result == "recommend_approve":
                stats["recommend"] += 1
            elif result == "hold":
                stats["hold"] += 1
            else:
                stats["skip"] += 1

    log("审批完成: 应用%d 建议批准%d 暂缓%d 跳过%d 过期%d 待写diff%d" % (
        stats["applied"], stats["recommend"], stats["hold"],
        stats["skip"], stats["expired"], stats["needs_diff"]))
    return stats


if __name__ == "__main__":
    review_proposals()
