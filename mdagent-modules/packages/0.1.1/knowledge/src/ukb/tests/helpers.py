"""Shared fixtures. Uses invented facets/tags only (no real domain words)."""
import os
import shutil
import tempfile
import unittest

from ukb.api import DEFAULT_PACK, UKB

FIXTURE_PACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "pack_alpha")
ORIGIN = {"kind": "task", "ref": "task-42", "summary_of_evidence": "observed in run 1 and confirmed by rerun"}


def minimal_pdf(pages):
    """Build a tiny valid PDF with one text line per page (Helvetica)."""
    objs = []
    kids = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    for i, text in enumerate(pages):
        page_id = 3 + 2 * i
        content_id = page_id + 1
        kids.append("%d 0 R" % page_id)
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = "BT /F1 12 Tf 72 720 Td (%s) Tj ET" % safe
        objs.append((page_id, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                              "/Resources << /Font << /F1 %d 0 R >> >> >>" % (content_id, font_id)))
        objs.append((content_id, "<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)))
    objs.append((font_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objs.insert(0, (2, "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(kids), n_pages)))
    objs.insert(0, (1, "<< /Type /Catalog /Pages 2 0 R >>"))
    objs.sort()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for oid, body in objs:
        offsets[oid] = len(out)
        out += ("%d 0 obj\n%s\nendobj\n" % (oid, body)).encode("latin-1")
    xref = len(out)
    size = max(offsets) + 1
    out += ("xref\n0 %d\n0000000000 65535 f \n" % size).encode()
    for oid in range(1, size):
        out += ("%010d 00000 n \n" % offsets[oid]).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)).encode()
    return bytes(out)


class KBTestCase(unittest.TestCase):
    pdf_extractor = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ukbtest-")
        self.root = os.path.join(self.tmp, "kb")
        self.kb = UKB.init(self.root, packs=(DEFAULT_PACK, FIXTURE_PACK))
        self.kb._pdf_mode = self.pdf_extractor
        self.admin_tok = self.kb.add_admin("admin1", "test")
        self.ws = os.path.join(self.tmp, "ws")
        self.inbox = os.path.join(self.tmp, "inbox")
        os.makedirs(self.ws)
        os.makedirs(self.inbox)
        self.set_setting("checkout_roots", [self.ws])
        self.set_setting("intake_roots", [self.inbox])

    def tearDown(self):
        self.kb.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_setting(self, key, value):
        r = self.kb.propose_registry("setting", {"key": key, "value": value}, "test setup", "agent:setup")
        self.admin(r["task_id"])

    # ---- payload builders ----
    def lesson(self, title="Alpha lesson", tags=None, **over):
        p = {"mode": "new", "type_id": "lesson", "title": title,
             "fields": {"situation": "When the widget is cold.", "risk": "Gears slip.", "fix": "Warm it first."},
             "tags": tags if tags is not None else [{"facet": "topic", "tag": "widgets"}],
             "sources": [{"source_type": "case", "title": "run 1", "locator": "runs/1"}],
             "origin": dict(ORIGIN)}
        p.update(over)
        return p

    def case_payload(self, title="Case one", files=None, **over):
        files = files or [
            {"path": "in.main", "role": "input", "description": "main input",
             "content_text": "units x\nsize 10\nsteps 500\nrun 500\n"},
            {"path": "notes.md", "role": "report", "description": "report",
             "content_text": "# Setup\nWe set size to 10.\n# Result\nIt converged.\n"},
        ]
        p = {"mode": "new", "type_id": "case", "title": title,
             "fields": {"summary": "A small run that converged.", "goal": "Check convergence.",
                        "setup": "size 10 [[in.main:L2]], steps [[in.main:L3]]",
                        "outcome": "Converged, see [[notes.md#s2]].",
                        "result_reliability": "single_run",
                        "reuse": {"entry_files": ["in.main"],
                                  "parameters_to_adapt": [{"name": "size", "location": "in.main:L2",
                                                           "meaning": "box size", "original": "10"}]}},
             "tags": [{"facet": "topic", "tag": "widgets"}],
             "sources": [{"source_type": "run_artifact", "title": "run dir", "locator": "runs/case1"}],
             "origin": {"kind": "run", "ref": "runs/case1", "summary_of_evidence": "finished run with logs"},
             "files": files}
        p.update(over)
        return p

    # ---- review helpers ----
    def prereview(self, task_id, opinion=None, agent="llm:pre"):
        b = self.kb.prereview_bundle(task_id, 0)
        pages = [b] + [self.kb.prereview_bundle(task_id, i) for i in range(1, b["pages"])]
        tokens = [p["page_token"] for p in pages]
        op = {"duplicate_of": [], "reinforces": [], "contradicts": [], "refines": [], "supersedes": [],
              "checklist": [{"item": c, "ok": True, "note": "checked"} for c in b["instructions"]["checklist"]],
              "recommendation": "approve", "note": "read everything"}
        if b["kind"] in ("new", "revise"):
            op["type_opinion"] = {"ok": True, "note": "fits"}
            op["tag_opinion"] = {"ok": True, "note": "fits"}
        if b["instructions"]["document"]:
            op["summary_fidelity"] = [{"claim": "setup", "locator": "x", "ok": True, "note": "matches"}]
        if opinion:
            op.update(opinion)
        return self.kb.prereview_submit(task_id, agent, tokens, op), pages

    def admin(self, task_id, decision="approve", args=None, rationale="ok", token=None):
        v = self.kb.review_view(task_id)
        return self.kb.decide(task_id, "admin1", token or self.admin_tok, decision, rationale, v["bound_hash"],
                              "webui", args)

    approve = admin

    def add_approved(self, payload, submitter="agent:a"):
        r = self.kb.propose(payload, "mcp", submitter)
        self.prereview(r["task_id"])
        self.admin(r["task_id"])
        return r
