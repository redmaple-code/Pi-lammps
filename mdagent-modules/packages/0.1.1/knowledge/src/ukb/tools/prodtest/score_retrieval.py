"""Score the blind retrieval run.

Inputs in <dir>: eval_questions.json (gold), eval_answers_A/B.json (agent answers), eval_calls_retrieverA/B.jsonl
(tool call logs). Metrics, per in-scope question:
  - route recall: share of primary gold entries that appeared in anything the agent read (ukb_read results)
  - answer recall: share of primary gold entries the agent listed in used_entries
  - answer precision: share of used_entries that are primary or related gold
  - hit@any: at least one primary entry used
Out-of-scope questions: correct if the agent answered NOT_FOUND (or used no entries).
Also reports reading cost: chars read and tool calls per question (calls are attributed to questions by order).
"""
import json
import os
import sys


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def read_entries(calls):
    seen = set()
    chars = 0
    for c in calls:
        if c["tool"] in ("ukb_read", "ukb_must_read") and c["ok"] and isinstance(c["result"], dict):
            for it in c["result"].get("items", []):
                seen.add(it["entry_id"])
        if c["ok"]:
            chars += c.get("chars", len(json.dumps(c["result"], ensure_ascii=False)))
        if c["tool"] == "ukb_read_original" and c["ok"]:
            chars += len(c["result"].get("text") or "")
    return seen, chars


def main(d, suffix=""):
    gold = {q["qid"]: q for q in load(os.path.join(d, "eval_questions.json"))["questions"]}
    answers = {}
    for part in ("A", "B"):
        p = os.path.join(d, "eval_answers_%s.json" % part)
        hp = os.path.join(d, "eval_seen_retriever%s%s.json" % (part, suffix))
        handles = []
        if os.path.exists(hp):
            st = load(hp)
            handles = st.get("handles", []) if isinstance(st, dict) else []
        if os.path.exists(p):
            for a in load(p):
                used = []
                for u in a.get("used_entries", []):
                    u = str(u).strip()
                    if u.startswith("#") and u[1:].isdigit() and 0 < int(u[1:]) <= len(handles):
                        u = handles[int(u[1:]) - 1]
                    used.append(u)
                a["used_entries"] = used
                answers[a["qid"]] = a
    calls = []
    for part in ("A", "B"):
        p = os.path.join(d, "eval_calls_retriever%s%s.jsonl" % (part, suffix))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                calls += [json.loads(x) for x in f if x.strip()]
    all_read, total_chars = read_entries(calls)
    rows, oos = [], []
    for qid, g in sorted(gold.items()):
        a = answers.get(qid)
        if a is None:
            rows.append({"qid": qid, "missing": True})
            continue
        used = {u.split("@")[0] for u in a.get("used_entries", [])}
        prim, rel = set(g["primary"]), set(g.get("related", []))
        if not prim:
            ok = a.get("answer", "").strip().upper().startswith("NOT_FOUND") or not used
            oos.append({"qid": qid, "correct_not_found": ok, "used": sorted(used), "answer": a.get("answer", "")[:120]})
            continue
        rows.append({
            "qid": qid, "style": g["style"], "n_primary": len(prim),
            "answer_recall": len(used & prim) / len(prim),
            "answer_precision": (len(used & (prim | rel)) / len(used)) if used else 0.0,
            "hit": bool(used & prim),
            "missed": sorted(prim - used), "extra": sorted(used - prim - rel),
            "said_not_found": a.get("answer", "").strip().upper().startswith("NOT_FOUND"),
            "confidence": a.get("confidence"),
        })
    scored = [r for r in rows if not r.get("missing")]
    n = len(scored)
    def avg(k, rs=scored):
        return round(sum(r[k] for r in rs) / len(rs), 3) if rs else None
    by_style = {}
    for s in ("symptom", "howto", "fact"):
        rs = [r for r in scored if r["style"] == s]
        by_style[s] = {"n": len(rs), "hit": avg("hit", rs), "answer_recall": avg("answer_recall", rs),
                       "answer_precision": avg("answer_precision", rs)}
    gold_primary = set().union(*[set(g["primary"]) for g in gold.values()])
    out = {
        "in_scope_questions": n, "missing_answers": [r["qid"] for r in rows if r.get("missing")],
        "hit_rate": avg("hit"), "answer_recall": avg("answer_recall"), "answer_precision": avg("answer_precision"),
        "multi_entry": {"n": len([r for r in scored if r["n_primary"] > 1]),
                        "answer_recall": avg("answer_recall", [r for r in scored if r["n_primary"] > 1])},
        "by_style": by_style,
        "out_of_scope": {"n": len(oos), "correct": sum(1 for o in oos if o["correct_not_found"]), "detail": oos},
        "false_not_found": [r["qid"] for r in scored if r["said_not_found"]],
        "route_coverage": {"gold_primary_entries": len(gold_primary),
                           "ever_read": len(gold_primary & all_read)},
        "cost": {"tool_calls": len(calls), "calls_per_question": round(len(calls) / max(1, len(gold)), 1),
                 "chars_read_total": total_chars, "chars_per_question": round(total_chars / max(1, len(gold)))},
        "tool_mix": {t: sum(1 for c in calls if c["tool"] == t) for t in sorted({c["tool"] for c in calls})},
        "disclosed_chars_by_tool": {t: sum(c.get("chars", len(json.dumps(c["result"], ensure_ascii=False)))
                                           for c in calls if c["tool"] == t) for t in sorted({c["tool"] for c in calls})},
        "full_entries_read": sum((len(c["result"].get("items", [])) if isinstance(c["result"], dict)
                                  else len(c["args"].get("entry_refs", [])))
                                 for c in calls if c["tool"] == "ukb_read" and c["ok"]),
        "refused_calls": {e: sum(1 for c in calls if not c["ok"] and c["result"].get("error") == e)
                          for e in sorted({c["result"].get("error") for c in calls if not c["ok"]})},
        "misses": [{"qid": r["qid"], "missed": r["missed"], "extra": r["extra"]} for r in scored
                   if r["missed"] or r["extra"]],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
