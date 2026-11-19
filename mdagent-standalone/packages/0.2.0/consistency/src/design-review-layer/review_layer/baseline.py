"""Baseline registry: reference dimensions + extension pathway (no auto-merge)."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "baseline" / "registry.json"


def load():
    return json.loads(BASE.read_text(encoding="utf-8"))


def names():
    return [d["name"] for d in load()["dimensions"]]


def add_candidate(name, definition, rationale, source):
    data = load()
    data.setdefault("candidates", []).append({
        "id": "CAND-%03d" % (len(data.get("candidates", [])) + 1),
        "name": name, "definition": definition, "rationale": rationale,
        "source": source, "status": "pending_owner_review",
    })
    BASE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data["candidates"][-1]


def approve_candidate(cid):
    data = load()
    cand = next((c for c in data.get("candidates", []) if c["id"] == cid), None)
    if not cand:
        raise SystemExit("candidate not found: " + cid)
    if any(d["name"] == cand["name"] for d in data["dimensions"]):
        cand["status"] = "already_present"
    else:
        data["dimensions"].append({"id": "D%d" % (len(data["dimensions"]) + 1),
                                   "name": cand["name"], "definition": cand["definition"],
                                   "applies_when": cand.get("rationale", "")})
        cand["status"] = "approved"
        v = float(data.get("version", "1.0")) + 0.1
        data["version"] = "%.1f" % v
        data.setdefault("changelog", []).append({"version": data["version"], "date": "2026-10-30",
                                                 "change": "新增维度：" + cand["name"]})
    BASE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cand
