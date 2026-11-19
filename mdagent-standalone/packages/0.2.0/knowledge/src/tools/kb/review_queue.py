"""Summarise what the administrator still has to review in the UKB store.

  python tools/kb/review_queue.py [--data-root DIR] [--kind new] [--limit 500]

Migrated legacy entries arrive as candidates; open tools/kb/serve.py and approve them in the console
(one click per task) after reading the prereview opinion.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root")
    ap.add_argument("--kind")
    ap.add_argument("--limit", type=int, default=500)
    a = ap.parse_args()
    os.environ["MDAGENT_KB"] = "ukb"
    kb = adapter.kb(a.data_root)
    tasks = kb.queue("open", a.kind, a.limit)
    ne = sum(1 for t in tasks if not t["prereviewed"])
    by_type = {}
    for t in tasks:
        key = t["type"] or "unknown"
        by_type[key] = by_type.get(key, 0) + 1
    print(json.dumps({"store": adapter.ukb_root(a.data_root), "open": len(tasks), "awaiting_prereview": ne,
                      "by_type": by_type, "first": [{"task": t["task_id"], "title": t["title"], "type": t["type"],
                                                    "prereviewed": t["prereviewed"]} for t in tasks[:10]]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
