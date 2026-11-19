"""End-to-end drill through the real surfaces: CLI init, MCP stdio subprocess, web console over HTTP.

Usage: python tools/live_drill.py <scratch_dir> <case_asset_dir>
Leaves nothing outside <scratch_dir>. Prints a JSON report.
"""
import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from ukb.webui import Console, make_handler  # noqa: E402
from ukb.api import UKB  # noqa: E402


def run(cmd, env=None, inp=None):
    p = subprocess.run(cmd, input=inp, capture_output=True, env=env, timeout=300)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def cli(root, *args):
    env = dict(os.environ, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
    rc, out, err = run([sys.executable, "-m", "ukb", "--root", root] + list(args), env)
    return rc, json.loads(out) if out.strip() else err


class MCP:
    def __init__(self, root, agent):
        env = dict(os.environ, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, "-m", "ukb.mcp_server", "--root", root, "--agent", agent],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.n = 0
        self._send({"jsonrpc": "2.0", "id": self._id(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"}})
        self._recv()

    def _id(self):
        self.n += 1
        return self.n

    def _send(self, m):
        self.p.stdin.write((json.dumps(m) + "\n").encode())
        self.p.stdin.flush()

    def _recv(self):
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def call(self, name, args):
        self._send({"jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
        r = self._recv()["result"]
        return r.get("isError", False), json.loads(r["content"][0]["text"])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)
        self.p.stdout.close()
        self.p.stderr.close()


def make_pdf(path):
    import fitz
    doc = fitz.open()
    for text in ("Widget transport study. Page one: introduction.",
                 "Page two: measured value of the transfer coefficient is 0.73 at 300 K."):
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    doc.save(path)
    doc.close()


def prereview_all(mcp, task_id, extra=None):
    err, b = mcp.call("ukb_prereview_bundle", {"task_id": task_id})
    assert not err, b
    tokens = [b["page_token"]]
    for i in range(1, b["pages"]):
        tokens.append(mcp.call("ukb_prereview_bundle", {"task_id": task_id, "page": i})[1]["page_token"])
    op = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True, "note": "tags fit"},
          "recommendation": "approve", "note": "read all pages",
          "checklist": [{"item": c, "ok": True, "note": "ok"} for c in b["instructions"]["checklist"]]}
    if b["instructions"]["document"]:
        op["summary_fidelity"] = [{"claim": "key values", "locator": "checked", "ok": True, "note": "match"}]
    if extra:
        op.update(extra)
    return mcp.call("ukb_prereview_submit", {"task_id": task_id, "page_tokens": tokens, "opinion": op}), b


def main(scratch, assets):
    report = {}
    if os.path.exists(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch)
    root = os.path.join(scratch, "kb")
    ws = os.path.join(scratch, "workspace")
    inbox = os.path.join(scratch, "inbox")
    os.makedirs(ws)
    os.makedirs(inbox)

    rc, init = cli(root, "init", "--admin", "alice")
    report["init"] = rc == 0
    alice_tok = init["token"]
    for key, val in (("checkout_roots", [ws]), ("intake_roots", [inbox])):
        rc, out = cli(root, "setting", key, json.dumps(val), "--admin", "alice", "--token", alice_tok,
                      "--reason", "drill setup")
        assert rc == 0, out

    # domain pack: concrete tags live in data, not code
    pack = os.path.join(scratch, "pack")
    os.makedirs(pack)
    with open(os.path.join(pack, "tag.json"), "w", encoding="utf-8") as f:
        json.dump([{"facet": "topic", "slug": "equilibration", "label": "平衡与弛豫",
                    "description": "体系平衡、弛豫与系综控制相关的经验和案例", "aliases": ["平衡", "npt"]},
                   {"facet": "topic", "slug": "log-diagnosis", "label": "日志诊断",
                    "description": "从运行日志判断异常", "aliases": ["日志"]},
                   {"facet": "tool", "slug": "md-engine-a", "label": "引擎 A", "description": "演练用的计算引擎"}],
                  f, ensure_ascii=False)
    rc, out = cli(root, "load-pack", pack)
    report["domain_pack_items"] = out["items"]

    shutil.copy(os.path.join(assets, "md01-cu-npt", "assets", "Cu_u3.eam"), os.path.join(inbox, "Cu_u3.eam"))
    log_b = os.path.join(assets, "l0d-log-diagnosis", "assets", "log_B.txt")
    shutil.copy(log_b, os.path.join(inbox, "log.lammps"))
    with open(os.path.join(inbox, "in.npt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("units metal\nlattice fcc 3.615\nregion box block 0 10 0 10 0 10\ncreate_box 1 box\n"
                "create_atoms 1 box\npair_style eam\npair_coeff 1 1 Cu_u3.eam\n"
                "fix 1 all npt temp 300 300 0.1 iso 0 0 1.0\nrun 20000\n")
    with open(os.path.join(inbox, "traj.lammpstrj"), "w") as f:
        f.write("ITEM: TIMESTEP\n0\n")
    make_pdf(os.path.join(inbox, "paper.pdf"))

    agent = MCP(root, "writer")
    pre = MCP(root, "prereviewer")

    err, types = agent.call("ukb_guide", {})
    report["types_listed"] = len(types["types"])

    case = {"mode": "new", "type_id": "case", "title": "NPT 平衡演练案例",
            "fields": {"summary": "fcc 体系 NPT 平衡 20000 步。",
                       "goal": "得到 300 K 零压下的平衡晶格。",
                       "setup": "晶格常数 [[in.npt:L2]]，系综与温度 [[in.npt:L8]]，参数文件 [[Cu_u3.eam]]",
                       "outcome": "日志见 [[log.lammps]]。",
                       "result_reliability": "single_run",
                       "reuse": {"entry_files": ["in.npt"],
                                 "parameters_to_adapt": [{"name": "temperature", "location": "in.npt:L8",
                                                          "meaning": "目标温度", "original": "300"}],
                                 "prerequisites": ["参数文件与输入同目录"]}},
            "tags": [{"facet": "topic", "tag": "平衡"}, {"facet": "tool", "tag": "md-engine-a"}],
            "origin": {"kind": "run", "ref": "drill/run1", "summary_of_evidence": "drill run"},
            "sources": [{"source_type": "run_artifact", "title": "drill run", "locator": "drill/run1"}],
            "files": [{"path": "in.npt", "role": "input", "description": "主输入",
                       "source_path": os.path.join(inbox, "in.npt")},
                      {"path": "Cu_u3.eam", "role": "parameter_file", "description": "参数文件",
                       "source_path": os.path.join(inbox, "Cu_u3.eam")},
                      {"path": "log.lammps", "role": "log", "description": "运行日志",
                       "source_path": os.path.join(inbox, "log.lammps")}]}
    bad = json.loads(json.dumps(case))
    bad["files"].append({"path": "traj.lammpstrj", "role": "data", "description": "轨迹",
                         "source_path": os.path.join(inbox, "traj.lammpstrj")})
    err, res = agent.call("ukb_propose", {"payload": bad})
    report["trajectory_refused"] = err and res["error"] == "FILE_TYPE_DENIED"

    err, c = agent.call("ukb_propose", {"payload": case})
    assert not err, c
    err, cat = agent.call("ukb_catalog", {"scope": {"topic": ["npt"]}, "format": "json"})
    report["candidate_hidden"] = cat["count"] == 0
    (ok, sub), bundle = prereview_all(pre, c["task_id"])
    report["case_prereview_parts"] = sorted({it["part"] for it in bundle["items"]})

    with open(os.path.join(inbox, "paper.pdf"), "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode()
    ref = {"mode": "new", "type_id": "reference", "title": "Widget transport study",
           "fields": {"citation": "A. Example, Drill Journal 1 (2026)", "summary": "演练用文献。",
                      "key_points": ["传输系数 0.73（300 K）[[paper.pdf#p2]]"],
                      "data_values": [{"quantity": "transfer coefficient", "value": "0.73",
                                       "conditions": "300 K", "locator": "paper.pdf#p2"}]},
           "tags": [{"facet": "topic", "tag": "equilibration"}],
           "origin": {"kind": "document", "ref": "paper.pdf", "summary_of_evidence": "user supplied"},
           "files": [{"path": "paper.pdf", "role": "fulltext", "description": "全文 PDF",
                      "content_b64": pdf_b64, "license_note": "drill copy"}]}
    err, r = agent.call("ukb_propose", {"payload": ref})
    assert not err, r
    (ok2, _), _ = prereview_all(pre, r["task_id"])

    # human patrol over HTTP
    import http.server
    kb = UKB(root)
    console = Console(kb)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(console))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    port = httpd.server_address[1]

    def req(method, path, form=None, cookie=None):
        cn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        body = urllib.parse.urlencode(form) if form else None
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if cookie:
            h["Cookie"] = cookie
        cn.request(method, path, body=body, headers=h)
        resp = cn.getresponse()
        data = resp.read().decode("utf-8")
        hdr = dict(resp.getheaders())
        cn.close()
        return resp.status, hdr, data

    st, h, _ = req("POST", "/login", {"reviewer_id": "alice", "token": alice_tok})
    cookie = h["Set-Cookie"].split(";")[0]
    csrf = console.sessions[cookie.split("=", 1)[1]]["csrf"]
    st, _, qpage = req("GET", "/", cookie=cookie)
    report["queue_page_lists_both"] = ("NPT 平衡演练案例" in qpage) and ("Widget transport study" in qpage)
    for tid in (c["task_id"], r["task_id"]):
        v = kb.review_view(tid)
        st, _, page = req("GET", "/task/" + tid, cookie=cookie)
        assert "AI 预审" in page
        st, _, body = req("POST", "/task/" + tid, {"csrf": csrf, "bound_hash": v["bound_hash"], "decision": "approve",
                                                   "rationale": "巡检通过"}, cookie=cookie)
        assert st == 303, body
    report["approved_via_web"] = all(kb.review_view(t)["state"] == "approved" for t in (c["task_id"], r["task_id"]))
    httpd.shutdown()
    httpd.server_close()
    kb.close()

    t0 = time.time()
    err, ov = agent.call("ukb_overview", {"facet": "topic", "format": "json"})
    err, c_ = agent.call("ukb_catalog", {"scope": {"topic": ["平衡"]}, "format": "json"})
    err, rd = agent.call("ukb_read", {"entry_refs": [i["ref"] for i in c_["items"]][:3], "format": "json"})
    report["read_scope_entries"] = [x["title"] for x in rd["items"]]
    report["read_has_no_original_text"] = "pair_coeff" not in json.dumps(rd, ensure_ascii=False)
    err, orig = agent.call("ukb_read_original", {"format": "json", "ref": c["entry_id"], "path": "in.npt", "lines": "L8"})
    report["original_line"] = orig["text"]
    err, pg = agent.call("ukb_read_original", {"format": "json", "ref": r["entry_id"], "path": "paper.pdf", "page": "p2"})
    report["pdf_page2_has_value"] = "0.73" in pg["text"]
    err, co = agent.call("ukb_checkout", {"ref": c["entry_id"], "dest": os.path.join(ws, "job1"), "task_ref": "drill"})
    report["checkout_files"] = [f["path"] for f in co["files"]]
    report["checkout_has_provenance"] = os.path.isfile(co["provenance"])
    err, esc = agent.call("ukb_checkout", {"ref": c["entry_id"], "dest": os.path.join(scratch, "outside")})
    report["checkout_outside_refused"] = err and esc["error"] == "CHECKOUT_DENIED"
    report["read_latency_s"] = round(time.time() - t0, 3)
    err, fb = agent.call("ukb_feedback", {"entry": c["entry_id"], "outcome": "reused_ok", "note": "drill"})
    agent.close()
    pre.close()

    rc, lint = cli(root, "lint")
    report["lint_ok"] = lint["ok"]
    report["lint_codes"] = sorted({i["code"] for i in lint["issues"]})
    rc, va = cli(root, "verify-audit")
    report["audit"] = va
    rc, st = cli(root, "status")
    report["status_entries"] = st["entries"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(v for k, v in report.items() if isinstance(v, bool)) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
