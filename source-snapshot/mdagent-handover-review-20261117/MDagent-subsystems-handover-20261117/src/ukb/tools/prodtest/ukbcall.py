"""One-shot MCP tool call for evaluation agents, with a call log.

  python -m tools.prodtest.ukbcall <kb_root> <agent> <tool> '<json args>'   (or @file.json)

Goes through the same Server.call as the stdio MCP server, so the agent sees exactly the production tool surface.
A real MCP session remembers which entries the catalog has shown; since every call here is a new process, that
set is kept in <kb_root>/../eval_seen_<agent>.json. Every call and its result are appended to
<kb_root>/../eval_calls_<agent>.jsonl for scoring.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ukb import agent_view  # noqa: E402
from ukb.api import UKB  # noqa: E402
from ukb.core.errors import UKBError  # noqa: E402
from ukb.mcp_server import TOOLS, Server  # noqa: E402


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        for t in TOOLS:
            print("%s: %s" % (t["name"], t["description"]))
            print("    args: %s" % json.dumps(t["inputSchema"]["properties"], ensure_ascii=False))
        return 0
    root, agent, tool = sys.argv[1], sys.argv[2], sys.argv[3]
    raw = sys.argv[4] if len(sys.argv) > 4 else "{}"
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as f:
            raw = f.read()
    args = json.loads(raw)
    base = os.path.dirname(os.path.abspath(root))
    seen_path = os.path.join(base, "eval_seen_%s.json" % agent)
    seen, handles = set(), agent_view.Handles()
    if os.path.exists(seen_path):
        with open(seen_path, encoding="utf-8") as f:
            st = json.load(f)
        seen = set(st["seen"])
        for eid in st["handles"]:
            handles.get(eid)
    kb = UKB(root)
    try:
        srv = Server(kb, "agent:" + agent, seen, handles)
        try:
            out = srv.call(tool, args)
            ok = True
        except UKBError as e:
            out = e.to_dict()
            ok = False
    finally:
        kb.close()
    with open(seen_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen), "handles": handles.ids}, f)
    os.replace(seen_path + ".tmp", seen_path)
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(base, "eval_calls_%s.jsonl" % agent), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "tool": tool, "args": args, "ok": ok, "chars": len(text),
                            "result": out}, ensure_ascii=False) + "\n")
    sys.stdout.write(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
