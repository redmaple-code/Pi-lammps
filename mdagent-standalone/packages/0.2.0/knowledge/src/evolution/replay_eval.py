#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""回放裁判 replay_eval(v1.6.0 ②,L3 考场,纯代码)
==================================================
铁原则(用户 2026-09-18 拍板):
  - 考题只用真实跑过且有结局的事件,绝不重跑 LAMMPS;
  - 无真实结局不成考题(从未被执行的机制只登记"untested",不判分);
  - 合成题禁止;本模块零 LLM、零网络、零写 atoms/strategy_db 本体。

考题来源:kit .v3.3_strategy_db.json 的 history(每次策略执行+结局)。
考卷校验:atoms.jsonl 的 exam_ready 原子 refs(strategy_db#ts#strategy#case_id)
必须能解析回 history 行——这是 tagging.verify_refs 之后的第二道存在性核验,
解析不出的原子剔除出考卷并计数(虚构修复混不进考场)。

考生:(根因 R, 策略 S) 推荐对。history 里真实跑过的组合才判分;
specialist_templates 声明了 root_causes 但从未执行 → "untested" 只登记。

判分(每个考生分 seen/holdout 两集,按结局时间 80/20 时序切):
  fires      该考生在真实事件中被执行次数
  hits       fires 中 outcome==success 的次数
  hit_rate   hits / 该根因真实成功总数(对可证成功的覆盖;根因无成功→None)
  fp_rate    (fires-hits)/fires(出手未成的比例;fires=0→None)

成绩册:evolution/replay_scores.json(tmp+replace 原子写)。
毕业门槛(命中≥0.8 误报≤0.1)是否采信由 ③graduation.py 裁定,
此处只在 graduation_hint 里如实给出对照,不做毕业动作。

用法:
  python3.11 evolution/replay_eval.py run [--db PATH] [--atoms PATH] [--out PATH]
  python3.11 evolution/replay_eval.py report [--out PATH]
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory  # noqa: E402  (ATOMS 路径常量;测试可注入沙盒)
import specialist_templates  # noqa: E402

POLICY_PATH = os.path.join(HERE, "POLICY.json")
SCORES_PATH = os.path.join(HERE, "replay_scores.json")

MAX_HISTORY = 20000
MAX_ATOMS = 200000
HOLDOUT_FRAC = 0.2
GRAD_HIT = 0.8
GRAD_FP = 0.1

_REF_RX = re.compile(r"^strategy_db#([^#]+)#([^#]+)#([^#]+)$")


def _kit_db_path():
    try:
        pol = json.load(open(POLICY_PATH, encoding="utf-8"))
        return os.path.join(pol["roots"]["kit"]["path"], ".v3.3_strategy_db.json")
    except Exception:
        return None


def load_history(db_path):
    """读 strategy_db history → 时序事件列表。只收 outcome∈{success,fail};
    坏行/缺字段跳过并计数,绝不抛。"""
    stats = {"rows": 0, "kept": 0, "skipped": 0}
    events = []
    try:
        db = json.load(open(db_path, encoding="utf-8"))
    except Exception:
        return events, stats
    hist = db.get("history") or []
    if not isinstance(hist, list):
        return events, stats
    for row in hist[:MAX_HISTORY]:
        stats["rows"] += 1
        if not isinstance(row, dict):
            stats["skipped"] += 1
            continue
        oc = row.get("outcome")
        if oc not in ("success", "fail"):
            stats["skipped"] += 1
            continue
        s, r, ts = row.get("strategy"), row.get("root_cause"), row.get("ts")
        if not all(isinstance(x, str) and x for x in (s, r, ts)):
            stats["skipped"] += 1
            continue
        events.append({"strategy": s, "root_cause": r, "ts": ts,
                       "settled_ts": str(row.get("settled_ts") or ts),
                       "case_id": str(row.get("case_id") or ""),
                       "outcome": oc})
        stats["kept"] += 1
    events.sort(key=lambda e: e["settled_ts"])
    return events, stats


def validate_atoms(atoms_path, events):
    """exam_ready 原子的 strategy_db refs 存在性核验(第二道)。
    返回 (resolved, unresolved_refs, stats)。"""
    index = {(e["ts"], e["strategy"], e["case_id"]) for e in events}
    resolved, unresolved = [], []
    stats = {"exam_ready": 0, "refs_ok": 0, "refs_bad": 0, "refs_na": 0}
    try:
        f = open(atoms_path, encoding="utf-8", errors="replace")
    except OSError:
        return resolved, unresolved, stats
    with f:
        for i, line in enumerate(f):
            if i >= MAX_ATOMS:
                break
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
            except ValueError:
                continue
            if not isinstance(a, dict) or not (a.get("axes") or {}).get("exam_ready"):
                continue
            stats["exam_ready"] += 1
            refs = a.get("refs") or []
            if isinstance(refs, str):
                refs = [refs]
            ok = False
            shaped = False
            for ref in refs[:8]:
                m = _REF_RX.match(str(ref)[:200])
                if not m:
                    continue
                shaped = True
                if (m.group(1), m.group(2), m.group(3)) in index:
                    ok = True
                    break
            if ok:
                stats["refs_ok"] += 1
                resolved.append(str(a.get("id") or ""))
            elif shaped:
                stats["refs_bad"] += 1
                if len(unresolved) < 50:
                    unresolved.append(str(a.get("id") or "?"))
            else:
                stats["refs_na"] += 1  # 非本考场题型(refs 不指 strategy_db)
    return resolved, unresolved, stats


def _score(fires, hits, successes_total):
    return {
        "fires": fires,
        "hits": hits,
        "hit_rate": (round(hits / successes_total, 4)
                     if successes_total > 0 else None),
        "fp_rate": round((fires - hits) / fires, 4) if fires > 0 else None,
    }


def judge(events):
    """对全部 (根因,策略) 考生判分。返回 candidates 列表(含 untested 模板)。"""
    split = max(1, int(len(events) * (1 - HOLDOUT_FRAC))) if events else 0
    seen, holdout = events[:split], events[split:]

    def _metrics(subset):
        succ_by_cause = {}
        for e in subset:
            if e["outcome"] == "success":
                succ_by_cause[e["root_cause"]] = \
                    succ_by_cause.get(e["root_cause"], 0) + 1
        agg = {}
        for e in subset:
            key = (e["root_cause"], e["strategy"])
            f, h = agg.get(key, (0, 0))
            agg[key] = (f + 1, h + (1 if e["outcome"] == "success" else 0))
        return succ_by_cause, agg

    succ_seen, agg_seen = _metrics(seen)
    succ_hold, agg_hold = _metrics(holdout)

    candidates = []
    keys = sorted(set(agg_seen) | set(agg_hold))
    for cause, strat in keys:
        f_s, h_s = agg_seen.get((cause, strat), (0, 0))
        f_h, h_h = agg_hold.get((cause, strat), (0, 0))
        hold = _score(f_h, h_h, succ_hold.get(cause, 0))
        entry = {
            "root_cause": cause, "strategy": strat, "status": "scored",
            "seen": _score(f_s, h_s, succ_seen.get(cause, 0)),
            "holdout": hold,
        }
        entry["graduation_hint"] = {
            "hit_ok": hold["hit_rate"] is not None and hold["hit_rate"] >= GRAD_HIT,
            "fp_ok": hold["fp_rate"] is not None and hold["fp_rate"] <= GRAD_FP,
            "note": "仅供参考,毕业裁定在 graduation.py",
        }
        candidates.append(entry)

    scored = {(c["root_cause"], c["strategy"]) for c in candidates}
    for tid, tpl in specialist_templates.TEMPLATES.items():
        for cause in tpl.get("root_causes", []):
            name = "spec_%s" % tid
            if (cause, name) in scored:
                continue
            candidates.append({
                "root_cause": cause, "strategy": name, "status": "untested",
                "seen": _score(0, 0, 0), "holdout": _score(0, 0, 0),
                "graduation_hint": {"hit_ok": False, "fp_ok": False,
                                    "note": "从未真实执行,无结局不成考题"},
            })
    return candidates


def run(db_path=None, atoms_path=None, out_path=SCORES_PATH):
    db_path = db_path or _kit_db_path()
    atoms_path = atoms_path or memory.ATOMS
    events, hstats = load_history(db_path)
    resolved, unresolved, astats = validate_atoms(atoms_path, events)
    candidates = judge(events)
    sheet = {
        "generated_ts": time.time(),
        "db_path": db_path, "atoms_path": atoms_path,
        "history": hstats, "atoms": astats,
        "atom_ids_verified": len(resolved),
        "atom_ids_unresolved": unresolved,
        "holdout_frac": HOLDOUT_FRAC,
        "graduation_line": {"hit_rate": GRAD_HIT, "fp_rate": GRAD_FP},
        "candidates": candidates,
    }
    tmp = out_path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sheet, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    return sheet


def report(out_path=SCORES_PATH):
    try:
        sheet = json.load(open(out_path, encoding="utf-8"))
    except Exception:
        print("成绩册不存在或不可读: %s(先跑 run)" % out_path)
        return 1
    print("回放成绩册 %s" % time.strftime(
        "%F %T", time.localtime(sheet.get("generated_ts", 0))))
    print("考题: history %d 条(剔除 %d);exam_ready 原子 %d,refs 核验过 %d/失效 %d/非本考场 %d"
          % (sheet["history"]["kept"], sheet["history"]["skipped"],
             sheet["atoms"]["exam_ready"], sheet["atoms"]["refs_ok"],
             sheet["atoms"]["refs_bad"], sheet["atoms"].get("refs_na", 0)))
    line = sheet.get("graduation_line", {})
    print("毕业线(参考): 命中≥%.1f 误报≤%.1f(留出集 %.0f%%)"
          % (line.get("hit_rate", GRAD_HIT), line.get("fp_rate", GRAD_FP),
             sheet.get("holdout_frac", HOLDOUT_FRAC) * 100))
    print("%-22s %-24s %6s %6s | holdout 命中/误报" % ("root_cause", "strategy", "fires", "hits"))
    for c in sheet.get("candidates", []):
        s, h = c["seen"], c["holdout"]
        fires = s["fires"] + h["fires"]
        hits = s["hits"] + h["hits"]
        hr = "-" if h["hit_rate"] is None else "%.2f" % h["hit_rate"]
        fp = "-" if h["fp_rate"] is None else "%.2f" % h["fp_rate"]
        tag = "" if c["status"] == "scored" else " [untested]"
        g = c.get("graduation_hint", {})
        mark = "✓" if (g.get("hit_ok") and g.get("fp_ok")) else "✗"
        print("%-22s %-24s %6d %6d | %5s/%5s %s%s"
              % (c["root_cause"][:22], c["strategy"][:24], fires, hits,
                 hr, fp, mark, tag))
    return 0


def main():
    ap = argparse.ArgumentParser(description="回放裁判: 真实结局考题,纯代码判分")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="出题+判分+写成绩册")
    r.add_argument("--db", default=None, help="strategy_db 路径(缺省 POLICY kit)")
    r.add_argument("--atoms", default=None, help="atoms.jsonl 路径")
    r.add_argument("--out", default=SCORES_PATH, help="成绩册输出路径")
    p = sub.add_parser("report", help="打印成绩册")
    p.add_argument("--out", default=SCORES_PATH)
    args = ap.parse_args()
    if args.cmd == "run":
        sheet = run(db_path=args.db, atoms_path=args.atoms, out_path=args.out)
        n_scored = sum(1 for c in sheet["candidates"] if c["status"] == "scored")
        print("考生 %d(判分 %d / untested %d)→ %s"
              % (len(sheet["candidates"]), n_scored,
                 len(sheet["candidates"]) - n_scored, args.out))
        return report(args.out)
    return report(args.out)


if __name__ == "__main__":
    sys.exit(main())
