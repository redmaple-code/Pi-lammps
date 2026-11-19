"""Persist the knowledge-base mode for this data root.

  python tools/kb/activate.py ukb      # switch MDagent to the UKB store
  python tools/kb/activate.py legacy   # roll back to the legacy files
  python tools/kb/activate.py status

The file is <data_root>/kb_mode.json. MDAGENT_KB still overrides it, so a single run can be forced either way.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    root = adapter.data_root()
    path = os.path.join(root, "kb_mode.json")
    if mode == "status":
        print(json.dumps({"data_root": root, "mode_file": path if os.path.exists(path) else None,
                          "mode": "ukb" if adapter.enabled() else "legacy",
                          "ukb_store": adapter.ukb_root(),
                          "store_exists": os.path.exists(os.path.join(adapter.ukb_root(), "ukb.sqlite"))},
                         ensure_ascii=False, indent=1))
        return 0
    if mode not in ("ukb", "legacy"):
        print("usage: activate.py ukb|legacy|status", file=sys.stderr)
        return 2
    if mode == "ukb" and not os.path.exists(os.path.join(adapter.ukb_root(), "ukb.sqlite")):
        print("refusing: no UKB store at %s; run tools/kb/bootstrap.py first" % adapter.ukb_root(), file=sys.stderr)
        return 2
    from evolution.evidence_v3.contracts import atomic_json
    atomic_json(path, {"mode": mode})
    print(json.dumps({"data_root": root, "mode": mode}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
