#!/usr/bin/env python3
"""Owner-side CLI for evidence v3 authorizations. Never call this from the agent.

    python tools/human_approve.py pending [--task TASK]
    python tools/human_approve.py show <request_hash>
    python tools/human_approve.py approve <request_hash> --actor NAME --reason "..." [--yes]
    python tools/human_approve.py reject  <request_hash> --actor NAME --reason "..."

Decisions land in <data_root>/human_inbox/decisions/<request_hash>.json and are
valid for MDSIM_HUMAN_INBOX_MAX_AGE (default 900 s). If
MDSIM_HUMAN_INBOX_SECRET is set, the decision is HMAC-signed.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import human_authority as human  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(prog="human_approve")
    sub = parser.add_subparsers(dest="command", required=True)
    row = sub.add_parser("pending")
    row.add_argument("--task")
    row = sub.add_parser("show")
    row.add_argument("request_hash")
    for name in ("approve", "reject"):
        row = sub.add_parser(name)
        row.add_argument("request_hash")
        row.add_argument("--actor", required=True)
        row.add_argument("--reason", required=True)
        row.add_argument("--yes", action="store_true",
                         help="skip the interactive confirmation prompt")
    args = parser.parse_args(argv)

    if args.command == "pending":
        for item in human.pending(args.task):
            request = item["request"]
            print("%s  task=%s  scope=%s  decision=%s  %s" % (
                item["request_hash"], item["task_id"], request.get("scope"),
                request.get("decision"), request.get("reason")))
        return 0

    path = human.inbox_root() / "requests" / (args.request_hash + ".json")
    if not path.is_file():
        print("no such request: %s" % args.request_hash, file=sys.stderr)
        return 2
    request_file = human._read_json(path)
    if args.command == "show":
        print(json.dumps(request_file, ensure_ascii=False, indent=2))
        return 0

    if not args.yes:
        print(json.dumps(request_file, ensure_ascii=False, indent=2))
        answer = input("%s %s? [y/N] " % (args.command, args.request_hash))
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    decision = {"schema": 1, "kind": "mdagent-authorization-decision",
                "request_hash": args.request_hash, "task_id": request_file["task_id"],
                "decision": "approve" if args.command == "approve" else "deny",
                "actor_id": args.actor, "reason": args.reason, "issued_at": int(time.time())}
    signature = human.sign(decision)
    if signature:
        decision["signature"] = signature
    human._atomic_json(human.inbox_root() / "decisions" / (args.request_hash + ".json"), decision)
    print(args.command, args.request_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
