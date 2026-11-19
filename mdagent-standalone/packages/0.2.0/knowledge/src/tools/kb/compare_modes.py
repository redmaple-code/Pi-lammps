"""Compare the legacy and UKB knowledge views for the same data root.

  python tools/kb/compare_modes.py [--data-root DIR] [--atoms PATH]

Both modes are read in-process (MDAGENT_KB is set per snapshot). Reports key sets and status maps for
exp_status_index / anchors / dimensions / surface and the ids recalled by evidence_v3. Exit 1 on any difference
other than the documented template sample.
"""
import argparse
import importlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))


def _snap(mode, data_root):
    os.environ["MDAGENT_KB"] = mode
    import evolution.stage_gate as sg
    import evolution.assessment as asm
    import evolution.deep_review as dr
    import evolution.surface as sf
    from evolution.evidence_v3.planning import validate_intent
    from evolution.evidence_v3 import retrieval
    for m in (sg, asm, dr, sf, retrieval):
        importlib.reload(m)
    idx = sg.exp_status_index(data_root)
    intent = validate_intent({"action": "consult", "goals": [{"id": "g1", "text": "时间步长 dt"}],
                              "conditions": {}, "unknowns": [], "source_refs": ["request:1"],
                              "queries": ["timestep dt", "时间步长"]})
    recalled = [e["id"] for e in retrieval.search(intent, data_root, limit=5000)["candidates"] if not e["id"].startswith("corpus:")]
    return {
        "idx": {k: v["status"] for k, v in idx.items()},
        "kinds": {k: v["kind"] for k, v in idx.items()},
        "anchors": sorted(asm.load_anchors(data_root)),
        "dims": {k: sorted(e["id"] for e in v) for k, v in dr.load_dimensions(data_root).items()},
        "items": sorted(sf.load_items(data_root)),
        "methodology": len(sg.load_methodology_entries(data_root)),
        "recalled": sorted(recalled),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root")
    ap.add_argument("--ignore", action="append", default=[],
                    help="id to ignore, e.g. the template sample exp-mine-001")
    a = ap.parse_args()
    ignored = set(a.ignore or []) | {"exp-mine-001"}
    legacy = _snap("legacy", a.data_root)
    ukb = _snap("ukb", a.data_root)
    diffs = {}
    for key in legacy:
        lv, uv = legacy[key], ukb[key]
        if isinstance(lv, dict):
            only_l = sorted(set(lv) - set(uv) - ignored)
            only_u = sorted(set(uv) - set(lv) - ignored)
            changed = sorted(k for k in set(lv) & set(uv) if lv[k] != uv[k] and k not in ignored)
            if only_l or only_u or changed:
                diffs[key] = {"legacy_only": only_l[:20], "ukb_only": only_u[:20], "changed": changed[:20]}
        elif isinstance(lv, list):
            only_l = sorted(set(lv) - set(uv) - ignored)
            only_u = sorted(set(uv) - set(lv) - ignored)
            if only_l or only_u:
                diffs[key] = {"legacy_only": only_l[:20], "ukb_only": only_u[:20]}
        elif lv != uv:
            diffs[key] = {"legacy": lv, "ukb": uv}
    report = {"data_root": a.data_root, "counts": {k: (len(v) if hasattr(v, "__len__") else v)
                                                  for k, v in legacy.items()},
              "ukb_counts": {k: (len(v) if hasattr(v, "__len__") else v) for k, v in ukb.items()},
              "differences": diffs, "ok": not diffs}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
