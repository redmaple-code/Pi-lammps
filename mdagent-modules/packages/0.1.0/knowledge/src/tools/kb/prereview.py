"""AI prereview helper for locally run agents.

The MCP server exposes the same flow to agents; this CLI is for agents that only have a shell.

  python tools/kb/prereview.py queue --limit 500
  python tools/kb/prereview.py bundle --task t... --out data/ukb_prereview/t....json
  python tools/kb/prereview.py submit --task t... --reviewer llm:mdagent-prereview-1 --opinion opinion.json

`bundle` writes every page of the current material (pending entry, origin, originals, taxonomy and scope) plus the
page tokens the submit call needs. Read the whole file before writing an opinion; never approve anything here.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402


def _open(data_root):
    os.environ["MDAGENT_KB"] = "ukb"
    return adapter.kb(data_root)


def _dump(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def _corpus(kb, out):
    """One line per active/trial/candidate entry so a prereviewer can search the whole KB for duplicates and
    contradictions once instead of re-reading the same scope scaffolding for every task."""
    vendor = os.path.join(REPO, "ukb")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    from ukb.core import navigate
    rows = kb.conn.execute(
        "SELECT e.*, coalesce(e.head_rev, (SELECT r.rev_id FROM entry_revision r WHERE r.entry_id=e.entry_id "
        "ORDER BY r.rev_no DESC LIMIT 1)) AS use_rev FROM entry e "
        "WHERE e.status IN ('active','trial','candidate') ORDER BY e.type_id, e.entry_id").fetchall()
    n = 0
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        for row in rows:
            rev = kb.conn.execute("SELECT * FROM entry_revision WHERE rev_id=?", (row["use_rev"],)).fetchone()
            if rev is None:
                continue
            rec = navigate.render(kb.conn, row, rev)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    _dump({"ok": True, "out": os.path.abspath(out), "entries": n})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("queue")
    s.add_argument("--limit", type=int, default=500)

    s = sub.add_parser("bundle")
    s.add_argument("--task", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--compact", action="store_true",
                   help="keep pending/origin/original/taxonomy in full, reduce scope entries to ref+title")

    s = sub.add_parser("corpus")
    s.add_argument("--out", required=True, help="JSONL: every active/trial/candidate entry, rendered")

    s = sub.add_parser("submit")
    s.add_argument("--task", required=True)
    s.add_argument("--reviewer", required=True,
                   help="llm:<name>; must differ from the submitter (import:legacy, agent:...)")
    s.add_argument("--bundle", help="bundle written earlier; defaults to the opinion's sibling file")
    s.add_argument("--opinion", required=True, help="JSON file with the structured opinion")

    a = ap.parse_args()
    kb = _open(a.data_root)
    try:
        if a.cmd == "queue":
            tasks = kb.queue("open", limit=a.limit, needs="prereview")
            _dump({"count": len(tasks),
                   "by_type": _counts(t["type"] for t in tasks),
                   "tasks": [{"task_id": t["task_id"], "type": t["type"], "title": t["title"],
                              "submitter": t["submitter"]} for t in tasks]})
            return 0
        if a.cmd == "bundle":
            first = kb.prereview_bundle(a.task, 0)
            pages = [first]
            for i in range(1, int(first["pages"])):
                pages.append(kb.prereview_bundle(a.task, i))
            out = {"task_id": a.task, "bound_hash": first["bound_hash"],
                   "page_tokens": [p["page_token"] for p in pages],
                   "instructions": first.get("instructions")}
            if a.compact:
                items = [it for p in pages for it in p["items"] if it["part"] != "scope"]
                scope = [it for p in pages for it in p["items"] if it["part"] == "scope"]
                out["items"] = items
                out["scope_refs"] = [it.get("ref") for it in scope]
                out["scope_note"] = ("scope entries are rendered in full in corpus.jsonl; read the ones that share "
                                     "tags with the pending entry before judging duplicates/conflicts")
            else:
                out["pages"] = pages
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8", newline="") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            refs = [it.get("ref") for p in pages for it in p["items"]]
            _dump({"ok": True, "out": os.path.abspath(a.out), "pages": len(pages), "items": len(refs),
                   "compact": bool(a.compact), "refs": refs[:20]})
            return 0
        if a.cmd == "corpus":
            _corpus(kb, a.out)
            return 0
        bundle_path = a.bundle
        if not bundle_path:
            bundle_path = os.path.splitext(a.opinion)[0] + ".bundle.json"
        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f)
        if bundle.get("task_id") != a.task:
            _dump({"ok": False, "error": "bundle belongs to another task", "bundle_task": bundle.get("task_id")})
            return 2
        with open(a.opinion, encoding="utf-8") as f:
            opinion = json.load(f)
        res = kb.prereview_submit(a.task, a.reviewer, bundle["page_tokens"], opinion)
        _dump(res)
        return 0
    except Exception as e:
        _dump({"ok": False, "error": type(e).__name__, "detail": str(e)})
        return 1
    finally:
        kb.close()


def _counts(items):
    out = {}
    for x in items:
        out[x or "unknown"] = out.get(x or "unknown", 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
