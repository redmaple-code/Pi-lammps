"""Common-issue registry: built-in explanations for known limitations/standard practice.

Points matching an entry are annotated with disposition:
  explained       -> default collapsed; requires a declaration, not an answer
  record_required -> default collapsed; one-line record required
  open            -> stays visible (must answer)
"""
import json
from pathlib import Path

REG = Path(__file__).resolve().parents[1] / "common_issues" / "registry.json"


def load():
    try:
        from . import ukb_store
        if ukb_store.enabled():
            d = ukb_store.load_common_issues()
            if d:
                return d
    except Exception:
        pass
    return json.loads(REG.read_text(encoding="utf-8"))


def match(point):
    text = " ".join([
        point.get("title", ""), point.get("dimension_guess", ""),
        point.get("why_it_matters", ""), point.get("physical_question", ""),
    ]).lower()
    best, best_score = None, 0
    for e in load()["entries"]:
        score = sum(1 for t in e.get("match_terms", []) if t.lower() in text)
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= 1 else None


def annotate(points):
    counts = {"explained": 0, "record_required": 0, "open": 0}
    for p in points:
        e = match(p)
        if e:
            p["common_issue_id"] = e["id"]
            p["common_issue_name"] = e["name"]
            p["disposition"] = e["disposition"]
            p["builtin_explanation"] = e["explanation"]
            p["required_declaration"] = e["required_declaration"]
        else:
            p["common_issue_id"] = None
            p["common_issue_name"] = None
            p["disposition"] = "open"
            p["builtin_explanation"] = ""
            p["required_declaration"] = ""
        counts[p["disposition"]] = counts.get(p["disposition"], 0) + 1
    return counts


def aggregate(points):
    """Collapse explained/record points into one group per common issue."""
    open_points = [p for p in points if p.get("disposition") == "open"]
    groups = {}
    for p in points:
        if p.get("disposition") == "open":
            continue
        key = p.get("common_issue_id") or "CI-UNMATCHED"
        g = groups.setdefault(key, {
            "common_issue_id": key,
            "name": p.get("common_issue_name") or "未分类",
            "disposition": p.get("disposition"),
            "occurrences": 0,
            "point_ids": [],
            "titles": [],
        })
        g["occurrences"] += 1
        g["point_ids"].append(p.get("point_id"))
        g["titles"].append(p.get("title"))
    return {"open_points": open_points, "common_groups": list(groups.values())}


def match_trace(point):
    """Return retrieval trace: query text, candidates (with matched terms/score), selected."""
    text = " ".join([
        point.get("title", ""), point.get("dimension_guess", ""),
        point.get("why_it_matters", ""), point.get("physical_question", ""),
    ]).lower()
    entries = load()["entries"]
    cands = []
    for e in entries:
        matched = [t for t in e.get("match_terms", []) if t.lower() in text]
        if matched:
            cands.append({
                "id": e.get("id"), "name": e.get("name"),
                "ukb_ref": e.get("ukb_ref"), "ukb_type": e.get("ukb_type"),
                "score": len(matched), "matched_terms": matched,
                "disposition": e.get("disposition"),
                "explanation": e.get("explanation", ""),
                "required_declaration": e.get("required_declaration", ""),
            })
    cands.sort(key=lambda x: (-x["score"], str(x["id"])))
    selected = cands[0] if cands else None
    for c in cands:
        c["selected"] = bool(selected and c["id"] == selected["id"])
    return {"query": text, "candidates": cands, "selected": selected,
            "candidate_count": len(cands)}
