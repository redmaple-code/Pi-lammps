"""CLI for the baseline registry: list / add candidate / approve (owner action)."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_layer import baseline


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--definition", required=True)
    a.add_argument("--rationale", required=True)
    a.add_argument("--source", default="ai-discovery")
    p = sub.add_parser("approve")
    p.add_argument("--id", required=True)
    args = ap.parse_args()
    if args.cmd == "list":
        d = baseline.load()
        print("version:", d["version"])
        for x in d["dimensions"]:
            print(" ", x["id"], x["name"])
        for c in d.get("candidates", []):
            print("  candidate", c["id"], c["name"], "status:", c["status"])
    elif args.cmd == "add":
        c = baseline.add_candidate(args.name, args.definition, args.rationale, args.source)
        print("candidate added:", c["id"], "(pending owner review)")
    elif args.cmd == "approve":
        c = baseline.approve_candidate(args.id)
        print("approved:", c["id"], c["name"], "status:", c["status"])


if __name__ == "__main__":
    main()
