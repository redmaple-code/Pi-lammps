"""MCP server, web console, CLI, export/backup and the domain-literal isolation rule."""
import http.client
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout

from tests.helpers import KBTestCase
from ukb import cli
from ukb.core.errors import UKBError
from ukb.mcp_server import TOOLS, Server
from ukb.webui import Console, make_handler

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ukb")


def _read(path):
    with open(path, "rb") as f:
        return f.read()


class MCP(KBTestCase):
    def call(self, srv, name, args):
        if name in ("ukb_overview", "ukb_catalog", "ukb_read", "ukb_read_original") and "format" not in args:
            args = dict(args, format="json")
        out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
        res = out["result"]
        return res.get("isError", False), json.loads(res["content"][0]["text"])

    def test_tool_surface_has_no_admin_decisions(self):
        names = {t["name"] for t in TOOLS}
        for forbidden in ("ukb_decide", "ukb_approve", "ukb_registry_approve", "ukb_set_status"):
            self.assertNotIn(forbidden, names)
        srv = Server(self.kb, "agent:t")
        out = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}})
        self.assertEqual(out["result"]["serverInfo"]["name"], "ukb")
        err, data = self.call(srv, "ukb_decide", {})
        self.assertTrue(err)

    def test_agent_flow_over_mcp(self):
        srv = Server(self.kb, "agent:t")
        err, data = self.call(srv, "ukb_propose", {"payload": self.lesson()})
        self.assertFalse(err, data)
        tid = data["task_id"]
        pre = Server(self.kb, "agent:checker")
        err, q = self.call(pre, "ukb_prereview_queue", {})
        self.assertEqual([t["task_id"] for t in q["tasks"]], [tid])
        err, own = self.call(srv, "ukb_prereview_queue", {})
        self.assertEqual(own["tasks"], [])
        err, b = self.call(pre, "ukb_prereview_bundle", {"task_id": tid})
        self.assertFalse(err)
        op = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve", "note": "fine",
              "checklist": [{"item": c, "ok": True} for c in b["instructions"]["checklist"]]}
        err, res = self.call(pre, "ukb_prereview_submit", {"task_id": tid, "page_tokens": [b["page_token"]],
                                                           "opinion": op})
        self.assertFalse(err, res)
        self.assertFalse(res["counts_as_approval"])
        err, mine = self.call(srv, "ukb_my_submissions", {})
        self.assertEqual(mine["items"][0]["task_state"], "open")
        self.assertEqual(mine["items"][0]["feedback"][0]["kind"], "llm")
        err, q = self.call(pre, "ukb_prereview_queue", {})
        self.assertEqual(q["tasks"], [])
        self.approve(tid)
        err, data = self.call(srv, "ukb_read", {"scope": {"topic": ["widgets"]}})
        self.assertEqual(data["error"], "ARGUMENT_UNKNOWN")
        reader = Server(self.kb, "agent:reader")
        eid = self.kb.catalog("agent")["items"][0]["ref"].split("@")[0]
        err, data = self.call(reader, "ukb_read", {"entry_refs": [eid]})
        self.assertEqual(data["error"], "NOT_LISTED")
        err, cat = self.call(reader, "ukb_catalog", {"scope": {"topic": ["widget"]}})
        self.assertEqual(cat["count"], 1)
        self.assertNotIn("fields", cat["items"][0])
        err, rd = self.call(reader, "ukb_read", {"entry_refs": [cat["items"][0]["ref"]]})
        self.assertEqual(rd["items"][0]["fields"]["fix"], "Warm it first.")
        err, data = self.call(reader, "ukb_catalog", {})
        self.assertEqual(data["error"], "ARGUMENT_MISSING")
        err, e = self.call(srv, "ukb_read", {"entry_refs": ["bad ref"]})
        self.assertTrue(err)
        self.assertEqual(e["error"], "ENTRY_REF_INVALID")

    def test_unknown_or_missing_arguments_refused(self):
        srv = Server(self.kb, "agent:t")
        err, data = self.call(srv, "ukb_read", {"refs": ["e1"]})
        self.assertTrue(err)
        self.assertEqual(data["error"], "ARGUMENT_UNKNOWN")
        self.assertIn("entry_refs", data["detail"]["accepted"])
        err, data = self.call(srv, "ukb_propose", {})
        self.assertEqual(data["error"], "ARGUMENT_MISSING")
        err, data = self.call(srv, "ukb_propose", self.lesson())
        self.assertEqual(data["error"], "ARGUMENT_UNKNOWN")

    def test_compact_read_is_smaller_and_keeps_content(self):
        c = self.add_approved(self.case_payload())
        srv = Server(self.kb, "agent:t")
        self.call(srv, "ukb_catalog", {"scope": {"topic": ["widgets"]}})
        err, comp = self.call(srv, "ukb_read", {"entry_refs": [c["entry_id"]]})
        err, full = self.call(srv, "ukb_read", {"entry_refs": [c["entry_id"]], "view": "full"})
        self.assertLess(comp["chars"], full["chars"])
        a, b = comp["items"][0], full["items"][0]
        self.assertEqual(a["fields"], b["fields"])
        self.assertEqual(a["tags"], ["topic/widgets"])
        self.assertTrue(all(isinstance(s, str) for s in a["sources"]))
        self.assertIn("in.main [input] main input (reusable)", a["document"]["files"])
        self.assertEqual(a["document"]["sections"]["notes.md"], ["s1 Setup", "s2 Result"])

    def test_read_limit_per_call(self):
        refs = [self.add_approved(self.lesson("L%d" % i))["entry_id"] for i in range(5)]
        srv = Server(self.kb, "agent:t")
        err, cat = self.call(srv, "ukb_catalog", {"scope": {"topic": ["widgets"]}})
        self.assertEqual(cat["count"], 5)
        err, data = self.call(srv, "ukb_read", {"entry_refs": refs})
        self.assertEqual(data["error"], "READ_TOO_MANY")
        err, rd = self.call(srv, "ukb_read", {"entry_refs": refs[:3]})
        self.assertFalse(err)
        self.assertEqual(rd["returned"], 3)

    def test_catalog_asks_to_narrow_when_large(self):
        for i in range(22):
            self.add_approved(self.lesson("Bulk %02d" % i))
        srv = Server(self.kb, "agent:t")
        err, cat = self.call(srv, "ukb_catalog", {"scope": {"topic": ["widgets"]}})
        self.assertEqual((cat["count"], cat["shown"]), (22, 20))
        self.assertIn("narrow", cat)
        self.assertIsInstance(cat["narrow_by"], list)

    def test_text_format_is_lean_and_uses_handles(self):
        self.add_approved(self.lesson("Alpha lesson"))
        self.add_approved(self.case_payload())
        srv = Server(self.kb, "agent:t")
        ov = srv.call("ukb_overview", {"facet": "topic"})
        self.assertIsInstance(ov, str)
        self.assertIn("widgets", ov)
        self.assertNotIn("Entries about widgets", ov)
        self.assertIn("Entries about widgets", srv.call("ukb_overview", {"facet": "topic", "describe": True}))
        cat = srv.call("ukb_catalog", {"scope": {"topic": ["widgets"]}})
        self.assertIn("#1 ", cat)
        self.assertNotIn("Warm it first", cat)
        rd = srv.call("ukb_read", {"entry_refs": ["#1", "#2"]})
        self.assertIn("situation: When the widget is cold.", rd)
        case_line = [l for l in cat.splitlines() if "Case one" in l][0]
        h = case_line.split()[0]
        core = srv.call("ukb_read", {"entry_refs": [h]})
        self.assertIn("未展开", core)
        self.assertNotIn("parameters_to_adapt", core)
        self.assertIn("parameters_to_adapt", srv.call("ukb_read", {"entry_refs": [h], "depth": "full"}))
        with self.assertRaises(UKBError):
            srv.call("ukb_read", {"entry_refs": ["#99"]})
        orig = srv.call("ukb_read_original", {"ref": h, "path": "in.main", "lines": "L2"})
        self.assertIn("size 10", orig)
        fresh = Server(self.kb, "agent:fresh")
        with self.assertRaises(UKBError):
            fresh.call("ukb_read_original", {"ref": "#1", "path": "in.main"})
        out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "ukb_catalog", "arguments": {"scope": {"topic": ["widgets"]}}}})
        self.assertNotIn("structuredContent", out["result"])

    def test_guide_has_payload_examples(self):
        srv = Server(self.kb, "agent:t")
        err, g = self.call(srv, "ukb_guide", {})
        self.assertEqual(set(g["payload_examples"]), {"call", "new", "document_files", "revise", "reinforce"})
        self.assertIn("note", g["payload_examples"]["reinforce"])

    def test_stdio_subprocess(self):
        env = dict(os.environ, PYTHONPATH=os.path.dirname(PKG), PYTHONIOENCODING="utf-8")
        p = subprocess.Popen([sys.executable, "-m", "ukb.mcp_server", "--root", self.root, "--agent", "sub"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ukb_overview",
                                                                               "arguments": {}}}]
        out, _ = p.communicate("\n".join(json.dumps(m) for m in msgs).encode() + b"\n", timeout=60)
        lines = [json.loads(x) for x in out.decode("utf-8").splitlines() if x.strip()]
        self.assertEqual([x["id"] for x in lines], [1, 2, 3])
        self.assertGreater(len(lines[1]["result"]["tools"]), 10)


class Web(KBTestCase):
    def setUp(self):
        super().setUp()
        import http.server
        self.console = Console(self.kb)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.console))
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, form=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = urllib.parse.urlencode(form) if form is not None else None
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if cookie:
            h["Cookie"] = cookie
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        return r.status, dict(r.getheaders()), data

    def login(self, rid, tok):
        st, h, _ = self.req("POST", "/login", {"reviewer_id": rid, "token": tok})
        self.assertEqual(st, 303)
        cookie = h["Set-Cookie"].split(";")[0]
        sess = self.console.sessions[cookie.split("=", 1)[1]]
        return cookie, sess["csrf"]

    def test_login_required_and_bad_token(self):
        st, _, body = self.req("GET", "/")
        self.assertIn("登录", body)
        st, _, _ = self.req("POST", "/login", {"reviewer_id": "admin1", "token": "nope"})
        self.assertEqual(st, 401)

    def test_patrol_approve_and_correct(self):
        r1 = self.kb.propose(self.lesson("<script>alert(1)</script>"), "mcp", "agent:a")
        r2 = self.kb.propose(self.lesson("Second"), "mcp", "agent:a")
        cookie, csrf = self.login("admin1", self.admin_tok)
        st, _, body = self.req("GET", "/", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("待 AI 预审", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)
        v = self.kb.review_view(r1["task_id"])
        st, _, body = self.req("POST", "/task/" + r1["task_id"], {"csrf": csrf, "bound_hash": v["bound_hash"],
                                                                  "decision": "approve", "rationale": "ok"},
                               cookie=cookie)
        self.assertEqual(st, 400)
        self.assertIn("AI prereview", body)
        self.prereview(r1["task_id"])
        self.prereview(r2["task_id"])
        st, _, _ = self.req("POST", "/task/" + r1["task_id"], {"csrf": "forged", "bound_hash": v["bound_hash"],
                                                               "decision": "approve", "rationale": "ok"}, cookie=cookie)
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/task/" + r1["task_id"], {"csrf": csrf, "bound_hash": v["bound_hash"],
                                                               "decision": "approve", "rationale": "ok"}, cookie=cookie)
        self.assertEqual(st, 303)
        self.assertEqual(self.kb.review_view(r1["task_id"])["state"], "approved")
        v2 = self.kb.review_view(r2["task_id"])
        corr = json.dumps({"title": "Second (fixed)"})
        st, _, body = self.req("POST", "/task/" + r2["task_id"], {"csrf": csrf, "bound_hash": v2["bound_hash"],
                                                                  "decision": "correct", "rationale": "title",
                                                                  "correction": corr}, cookie=cookie)
        self.assertEqual(st, 303, body)
        titles = sorted(x["title"] for x in self.kb.catalog("agent")["items"])
        self.assertEqual(titles, ["<script>alert(1)</script>", "Second (fixed)"])
        st, _, body = self.req("GET", "/lint", cookie=cookie)
        self.assertIn("完整", body)

    def test_document_original_view(self):
        c = self.add_approved(self.case_payload())
        cookie, _ = self.login("admin1", self.admin_tok)
        st, _, body = self.req("GET", "/orig?ref=%s&path=in.main" % c["entry_id"], cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("size 10", body)


class CLIAndMaintenance(KBTestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, json.loads(buf.getvalue())

    def test_cli_flow(self):
        p = os.path.join(self.tmp, "p.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.lesson(), f)
        rc, out = self.run_cli("propose", "@" + p, "--submitter", "agent:cli")
        self.assertEqual(rc, 0)
        self.prereview(out["task_id"])
        rc, res = self.run_cli("decide", out["task_id"], "approve", "--admin", "admin1", "--token", self.admin_tok,
                               "--rationale", "ok")
        self.assertEqual((rc, res["state"]), (0, "approved"))
        rc, cat = self.run_cli("catalog", "--scope", '{"topic":["widgets"]}')
        self.assertEqual(cat["count"], 1)
        rc, err = self.run_cli("decide", out["task_id"], "approve", "--admin", "admin1", "--token", "bad",
                               "--rationale", "x")
        self.assertEqual(rc, 1)

    def test_cli_init(self):
        root2 = os.path.join(self.tmp, "kb2")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--root", root2, "init", "--admin", "boss"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(out["token"])

    def test_export_covers_every_table(self):
        from ukb.core.maintenance import EXPORT_TABLES
        tables = {r[0] for r in self.kb.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT "
                                                     "LIKE 'sqlite_%'")}
        self.assertEqual(tables - set(EXPORT_TABLES), {"reviewer"})
        self.assertEqual(set(EXPORT_TABLES) - tables, set())

    def test_export_and_backup(self):
        self.add_approved(self.case_payload())
        d = os.path.join(self.tmp, "export")
        counts = self.kb.export(d)
        self.assertEqual(counts["entry"], 1)
        self.assertEqual(counts["doc_file"], 2)
        again = os.path.join(self.tmp, "export2")
        self.kb.export(again)
        for name in os.listdir(d):
            if name in ("audit_log.jsonl",):
                continue
            self.assertEqual(_read(os.path.join(d, name)), _read(os.path.join(again, name)), name)
        b = os.path.join(self.tmp, "b.sqlite")
        self.kb.backup(b)
        bc = sqlite3.connect(b)
        n = bc.execute("SELECT count(*) FROM entry").fetchone()[0]
        bc.close()
        self.assertEqual(n, 1)
        with self.assertRaises(FileExistsError):
            self.kb.backup(b)


class DomainIsolation(unittest.TestCase):
    """Core code must not contain concrete domain vocabulary; that lives in data packs."""
    # "dump" is not listed: json.dump(s) is ordinary code. Trajectory formats are covered by their extensions.
    FORBIDDEN = ["lammps", "gromacs", "vasp", "pair_style", "eam", "meam", "reaxff", "tersoff", "xtc",
                     "trajectory", "potential", "atom", "molecul", "crystal", "lattice", "thermostat", "metal", "steel",
                 "copper", "simulation", "dft", "lammpstrj", "restart"]

    def test_no_domain_literals_in_core(self):
        core = os.path.join(PKG, "core")
        hits = []
        for name in sorted(os.listdir(core)):
            if not name.endswith((".py", ".sql")):
                continue
            with open(os.path.join(core, name), encoding="utf-8") as fh:
                text = fh.read().lower()
            for w in self.FORBIDDEN:
                if re.search(r"(?<![a-z])%s" % re.escape(w), text):
                    hits.append((name, w))
        self.assertEqual(hits, [])

    def test_tests_use_invented_vocabulary(self):
        here = os.path.dirname(os.path.abspath(__file__))
        hits = []
        for name in os.listdir(os.path.join(here, "fixtures", "pack_alpha")):
            with open(os.path.join(here, "fixtures", "pack_alpha", name), encoding="utf-8") as fh:
                text = fh.read().lower()
            for w in self.FORBIDDEN:
                if w in text:
                    hits.append((name, w))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
