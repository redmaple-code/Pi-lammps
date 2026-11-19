"""Command line: setup, reviewer management, review, reading and maintenance."""
import argparse
import getpass
import json
import os
import sys

from .api import DEFAULT_PACK, UKB
from .core.errors import UKBError


def _out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _load_json(arg):
    if arg is None:
        return None
    if arg.startswith("@"):
        with open(arg[1:], encoding="utf-8") as f:
            return json.load(f)
    return json.loads(arg)


def _token(a):
    return a.token or os.environ.get("UKB_TOKEN") or getpass.getpass("admin token: ")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ukb")
    ap.add_argument("--root", default=os.environ.get("UKB_ROOT"), help="knowledge base directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--pack", action="append", help="extra pack directories loaded after the seed pack")
    s.add_argument("--admin", required=True, help="administrator id")
    s = sub.add_parser("load-pack")
    s.add_argument("pack")
    s = sub.add_parser("admin-add")
    s.add_argument("admin_id")
    s = sub.add_parser("admin-reset")
    s.add_argument("admin_id")
    s = sub.add_parser("admin-disable")
    s.add_argument("admin_id")
    s = sub.add_parser("setting")
    s.add_argument("key")
    s.add_argument("value", help="JSON value")
    s.add_argument("--admin", required=True)
    s.add_argument("--token")
    s.add_argument("--reason", required=True)

    s = sub.add_parser("propose")
    s.add_argument("payload", help="JSON or @file")
    s.add_argument("--submitter", required=True)
    s = sub.add_parser("queue")
    s.add_argument("--state", default="open")
    s.add_argument("--needs", choices=["prereview", "admin"])
    s = sub.add_parser("show")
    s.add_argument("task_id")
    s = sub.add_parser("decide")
    s.add_argument("task_id")
    s.add_argument("decision", choices=["approve", "approve_as_trial", "request_changes", "reject", "correct",
                                        "reinforce_into"])
    s.add_argument("--admin", required=True)
    s.add_argument("--token")
    s.add_argument("--rationale", default="")
    s.add_argument("--args", help="JSON or @file with correction / tag_decisions / relations / target_entry_id")
    s = sub.add_parser("propose-registry")
    s.add_argument("kind")
    s.add_argument("spec", help="JSON or @file")
    s.add_argument("--submitter", required=True)
    s.add_argument("--reason", required=True)

    for name in ("overview", "catalog", "read"):
        s = sub.add_parser(name)
        s.add_argument("--profile", default="agent")
        s.add_argument("--scope", help="JSON {facet:[tags]}")
        if name == "overview":
            s.add_argument("--facet")
        if name == "read":
            s.add_argument("--refs", nargs="*")
            s.add_argument("--cursor")
    s = sub.add_parser("read-original")
    s.add_argument("ref")
    s.add_argument("path")
    s.add_argument("--profile", default="agent")
    s.add_argument("--section")
    s.add_argument("--page")
    s.add_argument("--lines")
    s.add_argument("--cursor")
    s = sub.add_parser("checkout")
    s.add_argument("ref")
    s.add_argument("dest")
    s.add_argument("--profile", default="agent")
    s.add_argument("--actor", required=True)
    s.add_argument("--paths", nargs="*")
    s.add_argument("--overwrite", action="store_true")

    sub.add_parser("status")
    sub.add_parser("lint")
    sub.add_parser("verify-audit")
    s = sub.add_parser("export")
    s.add_argument("dest")
    s = sub.add_parser("backup")
    s.add_argument("dest")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    a = ap.parse_args(argv)
    if not a.root:
        ap.error("--root or UKB_ROOT is required")
    try:
        if a.cmd == "init":
            packs = [DEFAULT_PACK] + (a.pack or [])
            kb = UKB.init(a.root, packs, actor="bootstrap:" + a.admin)
            try:
                tok = kb.add_admin(a.admin, "bootstrap:" + a.admin)
            finally:
                kb.close()
            _out({"ok": True, "root": kb.root, "admin": a.admin, "token": tok,
                  "note": "store the token now; it is not shown again"})
            return 0
        kb = UKB(a.root)
        try:
            return _run(kb, a)
        finally:
            kb.close()
    except UKBError as e:
        _out(e.to_dict())
        return 1


def _run(kb, a):
    try:
        c = a.cmd
        if c == "load-pack":
            _out({"ok": True, "items": kb.load_pack(a.pack, "cli")})
        elif c == "admin-add":
            _out({"ok": True, "admin_id": a.admin_id, "token": kb.add_admin(a.admin_id, "cli"),
                  "note": "store the token now; it is not shown again"})
        elif c == "admin-reset":
            _out({"ok": True, "token": kb.reset_token(a.admin_id, "cli")})
        elif c == "admin-disable":
            kb.set_admin_active(a.admin_id, False, "cli")
            _out({"ok": True})
        elif c == "setting":
            r = kb.propose_registry("setting", {"key": a.key, "value": json.loads(a.value)}, a.reason,
                                    "cli:" + a.admin)
            v = kb.review_view(r["task_id"])
            _out(kb.decide(r["task_id"], a.admin, _token(a), "approve", a.reason, v["bound_hash"], "cli"))
        elif c == "propose":
            _out(kb.propose(_load_json(a.payload), "cli", a.submitter))
        elif c == "queue":
            _out(kb.queue(a.state, needs=a.needs))
        elif c == "show":
            _out(kb.review_view(a.task_id))
        elif c == "decide":
            v = kb.review_view(a.task_id)
            _out(kb.decide(a.task_id, a.admin, _token(a), a.decision, a.rationale, v["bound_hash"], "cli",
                           _load_json(a.args)))
        elif c == "propose-registry":
            _out(kb.propose_registry(a.kind, _load_json(a.spec), a.reason, a.submitter))
        elif c == "overview":
            _out(kb.overview(a.profile, a.facet, _load_json(a.scope)))
        elif c == "catalog":
            _out(kb.catalog(a.profile, _load_json(a.scope)))
        elif c == "read":
            _out(kb.read(a.profile, _load_json(a.scope), a.refs or None, None, a.cursor))
        elif c == "read-original":
            _out(kb.read_original(a.profile, a.ref, a.path, a.section, a.page, a.lines, a.cursor))
        elif c == "checkout":
            _out(kb.checkout(a.profile, a.ref, a.dest, a.actor, a.paths or None, False, a.overwrite))
        elif c == "status":
            _out(kb.status())
        elif c == "lint":
            _out(kb.lint())
        elif c == "verify-audit":
            _out(kb.verify_audit())
        elif c == "export":
            _out(kb.export(a.dest))
        elif c == "backup":
            _out(kb.backup(a.dest))
        elif c == "serve":
            from .webui import serve
            httpd, _ = serve(a.root, a.host, a.port)
            if a.host not in ("127.0.0.1", "localhost", "::1"):
                sys.stderr.write("warning: review console bound to a non-local address; put it behind TLS\n")
            sys.stderr.write("UKB review console on http://%s:%d\n" % (a.host, a.port))
            httpd.serve_forever()
        return 0
    except UKBError as e:
        _out(e.to_dict())
        return 1


if __name__ == "__main__":
    sys.exit(main())
