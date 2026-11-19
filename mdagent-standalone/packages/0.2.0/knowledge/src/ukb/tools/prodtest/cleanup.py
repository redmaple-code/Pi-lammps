"""List or retire the injected prodtest batch through the normal review path.

  python -m tools.prodtest.cleanup <kb_root> list
  python -m tools.prodtest.cleanup <kb_root> retire --admin <id> [--token T] [--status deprecated|revoked]

'retire' opens one status-change task per active/trial entry whose intake origin.ref equals the batch id, then
approves each as the admin. Everything stays auditable; nothing is deleted. Deleting the whole test KB directory
is the alternative when the KB was only ever a test.
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.prodtest.corpus_entries import BATCH  # noqa: E402
from ukb.api import UKB  # noqa: E402


def batch_entries(kb):
    rows = kb.conn.execute(
        "SELECT DISTINCT e.entry_id, e.status, e.type_id, r.title FROM intake i JOIN entry e ON "
        "e.entry_id = coalesce(i.result_entry_id, i.target_entry_id) LEFT JOIN entry_revision r ON r.rev_id=e.head_rev "
        "WHERE json_extract(i.context_json, '$.ref') = ? ORDER BY e.type_id, e.entry_id", (BATCH,)).fetchall()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("action", choices=["list", "retire"])
    ap.add_argument("--admin")
    ap.add_argument("--token")
    ap.add_argument("--status", default="deprecated", choices=["deprecated", "revoked"])
    a = ap.parse_args()
    kb = UKB(a.root)
    try:
        rows = batch_entries(kb)
        if a.action == "list":
            by = {}
            for r in rows:
                by[r["status"]] = by.get(r["status"], 0) + 1
            print(json.dumps({"batch": BATCH, "count": len(rows), "by_status": by, "entries": rows},
                             ensure_ascii=False, indent=1))
            return 0
        if not a.admin:
            ap.error("--admin is required for retire")
        tok = a.token or os.environ.get("UKB_TOKEN") or getpass.getpass("admin token: ")
        done = 0
        for r in rows:
            if r["status"] not in ("active", "trial"):
                continue
            t = kb.propose_status(r["entry_id"], a.status, "清理注入测试批次 " + BATCH, "cli:" + a.admin)
            v = kb.review_view(t["task_id"])
            kb.decide(t["task_id"], a.admin, tok, "approve", "清理测试批次", v["bound_hash"], "cli")
            done += 1
        print(json.dumps({"batch": BATCH, "retired": done, "status": a.status}, ensure_ascii=False))
        return 0
    finally:
        kb.close()


if __name__ == "__main__":
    sys.exit(main())
