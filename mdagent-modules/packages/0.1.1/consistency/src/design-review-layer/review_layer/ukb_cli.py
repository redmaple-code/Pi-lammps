"""CLI for the design-review <-> UKB bridge."""
import argparse
import atexit
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_layer import kb_api, ukb_store

atexit.register(ukb_store.close_connections)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init"); i.add_argument("--admin", default="redmaple"); i.add_argument("--root")
    a = sub.add_parser("ensure-admin"); a.add_argument("--admin", default="redmaple"); a.add_argument("--force", action="store_true")
    sub.add_parser("ensure-system")
    sub.add_parser("seed-all")
    sub.add_parser("upgrade-metadata")
    sub.add_parser("stats")
    sub.add_parser("baseline")
    sub.add_parser("common")
    sub.add_parser("inspect")
    l = sub.add_parser("list"); l.add_argument("--type", action="append"); l.add_argument("--status", action="append")
    f = sub.add_parser("find"); f.add_argument("text"); f.add_argument("--type")
    s = sub.add_parser("show"); s.add_argument("ref"); s.add_argument("--profile", default="reviewer")
    args = ap.parse_args()
    if args.cmd == "init":
        print("token:", ukb_store.init(args.root, args.admin))
    elif args.cmd == "ensure-admin":
        print(ukb_store.ensure_admin_token(args.admin, args.force))
    elif args.cmd == "ensure-system":
        print(ukb_store.ensure_system_admin())
    elif args.cmd == "seed-all":
        print("baseline:", ukb_store.seed_baseline())
        print("common:", ukb_store.seed_common_issues())
        print("discovery:", ukb_store.seed_discovery())
    elif args.cmd == "upgrade-metadata":
        for r in ukb_store.upgrade_common_issue_metadata():
            print(r)
    elif args.cmd == "stats":
        print(dict(ukb_store.stats()))
    elif args.cmd == "baseline":
        for n in ukb_store.load_baseline():
            print(n)
    elif args.cmd == "common":
        d = ukb_store.load_common_issues() or {"entries": []}
        for e in d["entries"]:
            print(e["id"], e["name"], "|", e["disposition"], "|", e.get("evidence_level"), "|", e.get("status"))
    elif args.cmd == "inspect":
        with kb_api.KnowledgeBase() as kb:
            print("status:", kb.status())
            print("stats:", kb.stats())
            print("integrity:", kb.integrity())
            print("audit:", kb.verify_audit())
    elif args.cmd == "list":
        with kb_api.KnowledgeBase() as kb:
            for r in kb.list_entries(types=args.type, statuses=args.status):
                print("%s %s r%s | %s" % (r["entry_id"], r["status"], r["rev_no"], r["title"]))
    elif args.cmd == "find":
        with kb_api.KnowledgeBase() as kb:
            for r in kb.find_entries(args.text, type_id=args.type):
                print("%s %s r%s | %s" % (r["entry_id"], r["status"], r["rev_no"], r["title"]))
    elif args.cmd == "show":
        import json
        with kb_api.KnowledgeBase() as kb:
            print(json.dumps(kb.entry(args.ref, profile=args.profile), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
