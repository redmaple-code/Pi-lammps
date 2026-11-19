import base64
import json
import os
import unittest

from tests.helpers import ORIGIN, KBTestCase, minimal_pdf
from ukb.core.errors import UKBError
from ukb.plugins import pdf_text


class DocumentIntake(KBTestCase):
    def test_trajectory_and_large_files_refused(self):
        for name in ("traj.dump", "dump.melt", "x.lammpstrj", "run.xtc", "a.dcd", "pack.zip", "w.h5",
                     "state.restart", "big.npy", "secret.pem", ".env", "kb.sqlite"):
            p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "d",
                                          "content_text": "a\nb\nc\n"},
                                         {"path": name, "role": "data", "description": "d", "content_text": "x"}])
            with self.assertRaises(UKBError, msg=name) as cm:
                self.kb.propose(p, "mcp", "agent:a")
            self.assertEqual(cm.exception.code, "FILE_TYPE_DENIED", name)
        p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "d",
                                      "content_text": "a\nb\nc\n"},
                                     {"path": "weird.qqq", "role": "data", "description": "d", "content_text": "x"}])
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_TYPE_NOT_ALLOWED")
        big = "x" * (2 * 1024 * 1024 + 10)
        p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "d",
                                      "content_text": "a\nb\nc\n"},
                                     {"path": "log.run", "role": "log", "description": "d", "content_text": big}])
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_TOO_LARGE")

    def test_global_file_limit_setting(self):
        self.set_setting("max_file_bytes", 100)
        p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "d",
                                      "content_text": "a\nb\nc\n" + "y" * 200}])
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_TOO_LARGE")

    def test_content_must_match_format(self):
        bad = [
            ({"path": "in.main", "role": "input", "description": "d",
              "content_b64": base64.b64encode(b"\x00\x01\x02bin").decode()}, "FILE_CONTENT_INVALID"),
            ({"path": "in.main", "role": "input", "description": "d",
              "content_b64": base64.b64encode(b"\xff\xfe\xfa").decode()}, "FILE_CONTENT_INVALID"),
        ]
        for f, code in bad:
            with self.assertRaises(UKBError) as cm:
                self.kb.propose(self.case_payload(files=[f]), "mcp", "agent:a")
            self.assertEqual(cm.exception.code, code)
        p = {"mode": "new", "type_id": "reference", "title": "Paper",
             "fields": {"citation": "A. Author, Journal, 2020", "summary": "s",
                        "key_points": ["point [[paper.pdf]]"]},
             "tags": [{"facet": "topic", "tag": "widgets"}], "origin": ORIGIN,
             "files": [{"path": "paper.pdf", "role": "fulltext", "description": "full text",
                        "content_b64": base64.b64encode(b"not a pdf").decode()}]}
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_CONTENT_INVALID")

    def test_path_safety(self):
        for path in ("../x.md", "/abs.md", "a\\b.md", "C:x.md", "a//b.md", "./../x.md", "a/./b.md", "a/ b .md",
                     "UKB_PROVENANCE.json", "a\x01.md"):
            p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "d",
                                          "content_text": "a\nb\nc\n"},
                                         {"path": path, "role": "report", "description": "d", "content_text": "x"}])
            with self.assertRaises(UKBError, msg=repr(path)) as cm:
                self.kb.propose(p, "mcp", "agent:a")
            self.assertIn(cm.exception.code, ("FILE_PATH_INVALID", "FILE_TYPE_NOT_ALLOWED"), path)

    def test_source_path_restricted_to_intake_roots(self):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("# x\n")
        p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "d",
                                      "content_text": "a\nb\nc\n"},
                                     {"path": "r.md", "role": "report", "description": "d", "source_path": outside}])
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_SOURCE_DENIED")
        inside = os.path.join(self.inbox, "r.md")
        with open(inside, "w") as f:
            f.write("# Report\nok\n")
        p["files"][1]["source_path"] = inside
        p["fields"]["outcome"] = "see [[r.md#s1]]"
        self.assertTrue(self.kb.propose(p, "mcp", "agent:a")["ok"])

    def test_roles_and_required_roles(self):
        p = self.case_payload(files=[{"path": "notes.md", "role": "report", "description": "d",
                                      "content_text": "# Setup\nx\n# Result\ny\n"}])
        p["fields"]["setup"] = "see [[notes.md#s1]]"
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_ROLE_MISSING")
        p = self.case_payload(files=[{"path": "in.main", "role": "fulltext", "description": "d",
                                      "content_text": "a\nb\nc\n"}])
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_ROLE_INVALID")
        p = self.case_payload()
        p["files"][0].pop("description")
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILE_DESCRIPTION_REQUIRED")

    def test_locators_must_resolve(self):
        for bad in ("[[nofile.md]]", "[[in.main:L99]]", "[[notes.md#s9]]", "[[in.main:L0-2]]"):
            p = self.case_payload()
            p["fields"]["outcome"] = "result " + bad
            with self.assertRaises(UKBError, msg=bad) as cm:
                self.kb.propose(p, "mcp", "agent:a")
            self.assertEqual(cm.exception.code, "LOCATOR_INVALID", bad)
        p = self.case_payload()
        p["fields"]["setup"] = "no citation here"
        p["fields"]["outcome"] = "also none"
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "LOCATOR_REQUIRED")

    def test_summary_length_limit(self):
        p = self.case_payload()
        p["fields"]["findings"] = ["f" * 900] * 4
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "SUMMARY_TOO_LONG")

    def test_case_requires_prereview_with_fidelity(self):
        r = self.kb.propose(self.case_payload(), "mcp", "agent:a")
        b = self.kb.prereview_bundle(r["task_id"], 0)
        self.assertTrue(b["instructions"]["document"])
        parts = [it["part"] for it in b["items"]]
        self.assertIn("original", parts)
        op = {"tag_opinion": {}, "recommendation": "approve", "note": "x",
              "checklist": [{"item": c, "ok": True} for c in b["instructions"]["checklist"]]}
        tokens = [self.kb.prereview_bundle(r["task_id"], i)["page_token"] for i in range(b["pages"])]
        with self.assertRaises(UKBError) as cm:
            self.kb.prereview_submit(r["task_id"], "llm:p", tokens, op)
        self.assertEqual(cm.exception.code, "OPINION_INCOMPLETE")


class DocumentRead(KBTestCase):
    def setUp(self):
        super().setUp()
        self.case = self.add_approved(self.case_payload())

    def test_read_returns_summary_not_original(self):
        it = self.kb.read("agent", entry_refs=[self.case["entry_id"]])["items"][0]
        self.assertIn("document", it)
        self.assertNotIn("units x", json.dumps(it))
        paths = [f["path"] for f in it["document"]["files"]]
        self.assertEqual(paths, ["in.main", "notes.md"])
        notes = [f for f in it["document"]["files"] if f["path"] == "notes.md"][0]
        self.assertEqual([s["title"] for s in notes["sections"]], ["Setup", "Result"])
        cat = self.kb.catalog("agent")["items"][0]
        self.assertTrue(cat["has_files"])
        self.assertNotIn("fields", cat)

    def test_read_original_selectors(self):
        ref = self.case["entry_id"]
        r = self.kb.read_original("agent", ref, "in.main", lines="L2-3")
        self.assertEqual(r["text"], "size 10\nsteps 500")
        r = self.kb.read_original("agent", ref, "notes.md", section="s1")
        self.assertTrue(r["text"].startswith("# Setup"))
        self.assertNotIn("Result", r["text"])
        whole = self.kb.read_original("agent", ref, "in.main")
        self.assertIn("run 500", whole["text"])
        with self.assertRaises(UKBError):
            self.kb.read_original("agent", ref, "in.main", lines="L1", section="s1")
        with self.assertRaises(UKBError):
            self.kb.read_original("agent", ref, "nope.md")

    def test_read_original_pages_long_text(self):
        long = "\n".join("line %05d %s" % (i, "z" * 40) for i in range(3000))
        p = self.case_payload("Long case", files=[{"path": "in.main", "role": "input", "description": "d",
                                                   "content_text": "a\nb\nc\n"},
                                                  {"path": "log.out", "role": "log", "description": "log",
                                                   "content_text": long}])
        p["fields"]["setup"] = "see [[in.main:L2]]"
        p["fields"]["outcome"] = "see [[log.out:L2999-3000]]"
        c = self.add_approved(p)
        got, cur, n = [], None, 0
        while True:
            r = self.kb.read_original("agent", c["entry_id"], "log.out", cursor=cur)
            got.append(r["text"])
            n += 1
            cur = r["next_cursor"]
            if not cur:
                break
        self.assertGreater(n, 1)
        self.assertEqual("".join(got), long)
        with self.assertRaises(UKBError) as cm:
            self.kb.read_original("agent", c["entry_id"], "in.main", cursor="10:deadbeef")
        self.assertEqual(cm.exception.code, "CURSOR_STALE")

    def test_blob_corruption_fails_closed(self):
        f = self.kb.conn.execute("SELECT sha256 FROM doc_file WHERE path='in.main'").fetchone()[0]
        with open(self.kb.blobs.path(f), "ab") as fh:
            fh.write(b"tamper")
        with self.assertRaises(UKBError) as cm:
            self.kb.read_original("agent", self.case["entry_id"], "in.main")
        self.assertEqual(cm.exception.code, "BLOB_CORRUPT")
        self.assertFalse(self.kb.lint()["ok"])

    def test_checkout_reusable_and_provenance(self):
        dest = os.path.join(self.ws, "job")
        out = self.kb.checkout("agent", self.case["entry_id"], dest, "agent:a", task_ref="T1")
        self.assertEqual([f["path"] for f in out["files"]], ["in.main"])
        with open(os.path.join(dest, "in.main")) as f:
            self.assertIn("size 10", f.read())
        with open(out["provenance"], encoding="utf-8") as fh:
            prov = json.load(fh)
        self.assertEqual(prov["entry_id"], self.case["entry_id"])
        self.assertEqual(prov["summary"]["reuse"]["parameters_to_adapt"][0]["name"], "size")
        n = self.kb.conn.execute("SELECT count(*) FROM checkout_log").fetchone()[0]
        self.assertEqual(n, 1)

    def test_checkout_guards(self):
        ref = self.case["entry_id"]
        with self.assertRaises(UKBError) as cm:
            self.kb.checkout("agent", ref, os.path.join(self.tmp, "elsewhere"), "agent:a")
        self.assertEqual(cm.exception.code, "CHECKOUT_DENIED")
        with self.assertRaises(UKBError) as cm:
            self.kb.checkout("agent", ref, os.path.join(self.ws, "..", "escape"), "agent:a")
        self.assertEqual(cm.exception.code, "CHECKOUT_DENIED")
        with self.assertRaises(UKBError) as cm:
            self.kb.checkout("agent", ref, os.path.join(self.ws, "j"), "agent:a", paths=["notes.md"])
        self.assertEqual(cm.exception.code, "CHECKOUT_NOT_REUSABLE")
        dest = os.path.join(self.ws, "j2")
        self.kb.checkout("agent", ref, dest, "agent:a")
        with open(os.path.join(dest, "in.main"), "w") as f:
            f.write("local edits")
        with self.assertRaises(UKBError) as cm:
            self.kb.checkout("agent", ref, dest, "agent:a")
        self.assertEqual(cm.exception.code, "CHECKOUT_EXISTS")
        out = self.kb.checkout("agent", ref, dest, "agent:a", overwrite=True)
        bk = out["files"][0]["backup"]
        with open(bk) as fh:
            self.assertEqual(fh.read(), "local edits")

    def test_revise_case_keeps_files_and_new_manifest(self):
        ref = self.case["entry_id"]
        p = {"mode": "revise", "target_entry_id": ref, "title": "Case one v2",
             "fields": dict(self.case_payload()["fields"], summary="Updated summary."),
             "sources": [{"source_type": "run_artifact", "title": "run dir", "locator": "runs/case1"}],
             "origin": ORIGIN, "files": [{"path": "in.main", "keep": True}, {"path": "notes.md", "keep": True},
                       {"path": "post.py", "role": "script", "description": "post", "content_text": "print(1)\n"}]}
        r = self.kb.propose(p, "mcp", "agent:b")
        self.prereview(r["task_id"])
        self.admin(r["task_id"])
        it = self.kb.read("agent", entry_refs=[ref])["items"][0]
        self.assertEqual(sorted(f["path"] for f in it["document"]["files"]), ["in.main", "notes.md", "post.py"])
        old = self.kb.read("agent", entry_refs=[ref + "@1"])["items"][0]
        self.assertEqual(len(old["document"]["files"]), 2)
        self.assertNotEqual(it["content_hash"], old["content_hash"])


class References(KBTestCase):
    pdf_extractor = "auto"

    def test_pdf_reference_with_page_locators(self):
        if not pdf_text.available():
            self.skipTest("pypdf not installed")
        pdf = minimal_pdf(["Page one says alpha.", "Page two says beta value 42."])
        p = {"mode": "new", "type_id": "reference", "title": "Widget paper",
             "fields": {"citation": "A. Author, Widget Journal 1, 2020", "summary": "Studies widgets.",
                        "key_points": ["beta value is 42 [[paper.pdf#p2]]"],
                        "data_values": [{"quantity": "beta", "value": "42", "locator": "paper.pdf#p2"}]},
             "tags": [{"facet": "topic", "tag": "widgets"}], "origin": ORIGIN,
             "files": [{"path": "paper.pdf", "role": "fulltext", "description": "full text PDF",
                        "content_b64": base64.b64encode(pdf).decode(), "license_note": "personal copy"}]}
        r = self.add_approved(p)
        page2 = self.kb.read_original("agent", r["entry_id"], "paper.pdf", page="p2")
        self.assertIn("42", page2["text"])
        self.assertNotIn("alpha", page2["text"])
        p["title"] = "Bad"
        p["fields"]["key_points"] = ["x [[paper.pdf#p5]]"]
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "LOCATOR_INVALID")

    def test_cjk_pdf_text_extracted_cleanly(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF not installed")
        doc = fitz.open()
        for text in ("第一页：介绍", "第二页：层错能约 44"):
            doc.new_page().insert_textbox(fitz.Rect(56, 56, 540, 780), text, fontsize=11, fontname="china-s")
        data = doc.tobytes()
        doc.close()
        out = pdf_text.extract(data)
        self.assertEqual(out.split("\f")[1].strip(), "第二页：层错能约 44")
        self.assertIsNone(pdf_text._clean(["N\x00e\x00\x00\x00\x00\x00 garbage"]))

    def test_pdf_with_supplied_text(self):
        pdf = minimal_pdf(["x"])
        p = {"mode": "new", "type_id": "reference", "title": "Paper with text",
             "fields": {"citation": "B. Author 2021", "summary": "s", "key_points": ["k [[p.pdf#p1]]"]},
             "tags": [{"facet": "topic", "tag": "widgets"}], "origin": ORIGIN,
             "files": [{"path": "p.pdf", "role": "fulltext", "description": "pdf",
                        "content_b64": base64.b64encode(pdf).decode(),
                        "text_content_text": "first page text\fsecond page text"}]}
        r = self.add_approved(p)
        self.assertEqual(self.kb.read_original("agent", r["entry_id"], "p.pdf", page="2")["text"], "second page text")

    def test_reference_without_fulltext_refused(self):
        p = {"mode": "new", "type_id": "reference", "title": "No file",
             "fields": {"citation": "C 2022", "summary": "s", "key_points": ["k"]},
             "tags": [{"facet": "topic", "tag": "widgets"}], "files": [], "origin": ORIGIN}
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "FILES_REQUIRED")


if __name__ == "__main__":
    unittest.main()
