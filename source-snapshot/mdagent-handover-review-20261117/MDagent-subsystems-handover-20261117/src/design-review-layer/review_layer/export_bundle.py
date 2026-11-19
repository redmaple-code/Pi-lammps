"""Export a static bundle for the MD Exp design-review router (read-only)."""
import json
from pathlib import Path

LAYER = Path(__file__).resolve().parents[1]
CASES = LAYER / "cases"
DISCOVERY = LAYER / "reports" / "discovery"


def _md_cases():
    ws = LAYER.parent
    src = ws / "MDagent-src"
    if not src.exists():
        # Inside the repository the repo root itself carries case_runs/.
        src = ws
    out = {}
    spec = {
        "rt_a3_al_npt": ("case_runs/rt_a3/proj-root/cases/rt_a3_al_npt_20261022_013637/intent.json",
                          "case_runs/rt/a3_npt/in.case", "case_runs/rt/rt_a3_validate.json"),
        "rt_a1_ni_lattice": ("case_runs/rt_a1/proj-root/cases/rt_a1_ni_lattice_20261022_011810/intent.json",
                              "case_runs/rt/a1_scan/in.case", "case_runs/rt/rt_a1_validate.json"),
    }
    for cid, (i, f, g) in spec.items():
        pi, pf, pg = src / i, src / f, src / g
        if pi.exists() and pf.exists():
            out[cid] = {"intent": pi, "infile": pf, "gate": pg if pg.exists() else None}
    return out


def build():
    import sys
    sys.path.insert(0, str(LAYER))
    from review_layer import common_issues, ukb_store
    from review_layer.consistency import build_consistency
    from review_layer.full_report import parse_infile, sim_sections
    from review_layer.discovery import map_dimension

    bundle = {"cases": {}}
    sources = {}
    for cid, cfg in _md_cases().items():
        sources[cid] = (cfg["intent"], cfg["infile"], cfg["gate"], "mdagent")
    for i in range(1, 6):
        cid = "case%d" % i
        p = CASES / cid
        if p.exists():
            sources[cid] = (p / "intent.json", p / "in.case", None, "workspace")

    for cid, (ip, fp, gp, source) in sources.items():
        intent = json.loads(ip.read_text(encoding="utf-8"))
        text = fp.read_text(encoding="utf-8")
        gate = json.loads(gp.read_text(encoding="utf-8")) if gp else {}
        info = parse_infile(text)
        exp = (intent.get("experiment_plan") or {}).get("sections") or []
        sim = sim_sections(intent, info)
        items = build_consistency(intent, text)
        vmap = {"ok": "ok", "questionable": "questionable", "unproven": "unproven",
                "invalid": "invalid", "plan_conflict": "plan_conflict"}
        plan = {
            "case_id": cid,
            "title": intent.get("objective", {}).get("simulation_goal") or cid,
            "experiment_plan": {"sections": exp},
            "simulation_plan": {"sections": sim},
            "comparisons": [{"id": it.id, "dim": it.element, "exp": it.claim,
                             "sim": it.detail, "diff": "", "verdict": vmap.get(it.verdict, "questionable")}
                            for it in items],
            "gate": {"status": gate.get("status") or "UNKNOWN",
                     "hard": len(gate.get("hard_violations") or []),
                     "uncertain": len(gate.get("uncertain_findings") or []),
                     "soft": len(gate.get("soft_warnings") or [])},
            "provenance": {"intent": str(ip.name), "infile": str(fp.name)},
        }
        lines = text.splitlines()
        check = {"status": "FAIL" if any(i.verdict == "invalid" for i in items) else "PASS",
                 "items": [{"id": it.id, "element": it.element, "claim": it.claim,
                            "detail": it.detail, "anchors": it.anchors,
                            "verdict": {"ok": "match", "plan_conflict": "warn", "questionable": "warn",
                                        "unproven": "warn", "invalid": "fail"}.get(it.verdict, "warn"),
                            "anchor": "L" + ",L".join(map(str, it.anchors)) if it.anchors else "—"}
                           for it in items]}
        infile = {"name": fp.name, "lines": lines}

        disc = {"available": False, "points": [], "counts": {}}
        dp = DISCOVERY / ("%s_points.json" % cid)
        ukb = {}
        if dp.exists():
            data = json.loads(dp.read_text(encoding="utf-8"))
            points = data.get("points", [])
            counts = common_issues.annotate(points)
            disc = {"available": True, "provider": data.get("provider", ""),
                    "counts": counts,
                    "points": [{"id": x["point_id"], "title": x["title"], "dimension": x["dimension_guess"],
                                "severity": x["severity_hint"], "disposition": x.get("disposition"),
                                "common_issue_id": x.get("common_issue_id")} for x in points]}
            baseline = ukb_store.load_baseline() if ukb_store.enabled() else []
            stats = dict(ukb_store.stats()) if ukb_store.enabled() else {}
            root = str(ukb_store.root()) if ukb_store.enabled() else ""
            for pt in points:
                common_issues.annotate([pt])
                tr = common_issues.match_trace(pt)
                dim = map_dimension(pt)
                ukb[pt["point_id"]] = {
                    "ok": True, "case": cid,
                    "point": {"id": pt["point_id"], "title": pt["title"],
                              "dimension": pt["dimension_guess"],
                              "question": pt["physical_question"],
                              "severity": pt["severity_hint"], "disposition": pt.get("disposition")},
                    "trace": tr,
                    "baseline": {"mapped_dimension": dim, "in_baseline": bool(dim and dim in baseline),
                                 "dimensions": baseline},
                    "ukb": {"root": root, "stats": stats},
                    "retrieval_path": ["UKB catalog(profile=agent, types=[lesson])",
                                       "UKB read(entry_refs=[...])",
                                       "keyword match: title+dimension+why+question",
                                       "selected: " + (tr["selected"]["id"] if tr["selected"] else "none")],
                }
        bundle["cases"][cid] = {"source": source, "plan": plan, "check": check,
                                "infile": infile, "discovery": disc, "ukbdebug": ukb}
    bundle["generated_by"] = "design-review-layer/export_bundle.py"
    return bundle


if __name__ == "__main__":
    out = LAYER / "reports" / "design_review_export.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("cases:", len(data["cases"]), "size:", out.stat().st_size)
