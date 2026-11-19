"""Intake and tagging audit on real material.

An AI writer (MCP subprocess) summarises verified benchmark material into entries, following ukb_guide /
ukb_prepare; a second AI prereviews; the admin approves over HTTP. Then every stored row is checked against the
intended content, and retrieval by tag is checked from the reader's side: for each question a later agent might
have, the scope it would choose must contain the right entries and nothing irrelevant.

Usage: python tools/audit_intake.py <scratch_dir> <microbench_suite_dir>
Prints a JSON report; exit 0 only if every check passes.
"""
import base64
import hashlib
import http.client
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from ukb.api import UKB  # noqa: E402
from ukb.webui import Console, make_handler  # noqa: E402

CHECKS = []


def check(name, ok, detail=None):
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    return ok


def cli(root, *args):
    env = dict(os.environ, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "ukb", "--root", root] + list(args), capture_output=True, env=env,
                       timeout=300)
    return p.returncode, json.loads(p.stdout.decode("utf-8"))


class MCP:
    def __init__(self, root, agent):
        env = dict(os.environ, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, "-m", "ukb.mcp_server", "--root", root, "--agent", agent],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.n = 0
        self.init = self._rpc("initialize", {"protocolVersion": "2024-11-05"})

    def _rpc(self, method, params):
        self.n += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
                           .encode())
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline().decode("utf-8"))["result"]

    def call(self, name, args=None):
        r = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        return r.get("isError", False), json.loads(r["content"][0]["text"])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)
        self.p.stdout.close()
        self.p.stderr.close()


DOMAIN_PACK = {
    "tag.json": [
        {"facet": "domain", "slug": "molecular-dynamics", "label": "分子动力学",
         "description": "经典分子动力学模拟（力场/势函数驱动的原子尺度时间演化）。不含第一性原理计算。",
         "aliases": ["MD", "分子模拟"]},
        {"facet": "topic", "slug": "run-failure", "label": "运行失败与诊断",
         "description": "模拟中断、报错或明显异常时的症状、根因与修复。只收已确认根因的；不收正常结果分析。",
         "aliases": ["报错", "故障", "崩溃", "诊断"]},
        {"facet": "topic", "slug": "integration-stability", "label": "积分稳定性", "parent": "run-failure",
         "description": "时间步长、初始速度等导致的积分发散（原子丢失、能量爆炸）。",
         "aliases": ["时间步", "原子丢失", "lost atoms"]},
        {"facet": "topic", "slug": "ensemble-control", "label": "系综控制",
         "description": "恒温器、恒压器的参数设置与平衡判断（阻尼时间、平衡判据）。包括设置不当导致的振荡。",
         "aliases": ["恒压", "恒温", "NPT", "NVT", "阻尼"]},
        {"facet": "topic", "slug": "input-setup", "label": "输入设置错误",
         "description": "输入脚本缺项、命令次序、参数缺失等导致无法开始运行的错误。", "parent": "run-failure",
         "aliases": ["缺参数", "pair_coeff"]},
        {"facet": "topic", "slug": "equilibrium-properties", "label": "平衡性质计算",
         "description": "平衡态密度、晶格常数等性质的计算方法与已验证结果。", "aliases": ["密度", "晶格常数"]},
        {"facet": "tool", "slug": "lammps", "label": "LAMMPS", "description": "LAMMPS 模拟程序", "aliases": ["lmp"]},
        {"facet": "object", "slug": "lj-fluid", "label": "LJ 流体", "description": "Lennard-Jones 约化单位体系",
         "aliases": ["LJ", "lennard-jones"]},
        {"facet": "object", "slug": "fcc-metal", "label": "fcc 金属", "description": "面心立方金属晶体",
         "aliases": ["fcc", "Cu", "铜"]},
    ],
    "condition_key.json": [
        {"key_id": "units_style", "label": "单位制", "value_type": "enum", "enum": ["lj", "metal", "real", "si"]},
        {"key_id": "temperature_k", "label": "温度", "value_type": "number", "unit": "K"},
        {"key_id": "pressure_bar", "label": "压强", "value_type": "number", "unit": "bar"},
    ],
}

LOG_FACTS = {
    "A": {"title": "LJ 约化单位下 dt=0.025 叠加 T*=5.0 初速导致原子丢失",
          "situation": "LJ 约化单位 256 原子体系，velocity create 5.0（T*=5.0），timestep 0.025，NVE 运行 400 步。",
          "risk": "积分步长对高温体系过大，第 40 步左右出现 ERROR: Lost atoms: original 256 current 28，运行中断。",
          "fix": "把 timestep 降到 0.005（或更小），高温初态时先短程平衡再加大步长；若必须高温则同时减小 dt。",
          "check": "再次运行时 thermo 输出不再出现 Lost atoms，总能量漂移保持在小量级。",
          "tags": [("topic", "integration-stability"), ("object", "lj-fluid")], "log": "log_A.txt", "line": 44},
    "B": {"title": "NPT 恒压阻尼 Pdamp=0.03 过小导致密度振荡不收敛",
          "situation": "LJ 体系 NPT 运行 3000 步，fix npt 的 Pdamp=0.03（时间步 0.005，相当于约 6 个时间步）。",
          "risk": "恒压耦合过强，密度在 0.68~0.91 之间持续振荡，平衡统计无效。",
          "fix": "Pdamp 取 1000 个时间步量级（约 1000*dt，例如 5.0），Tdamp 取 100 个时间步量级。",
          "check": "生产段密度的前后半窗均值差小于一个标准差。",
          "tags": [("topic", "ensemble-control"), ("object", "lj-fluid")], "log": "log_B.txt", "line": 42},
    "C": {"title": "声明 pair_style eam 后缺少 pair_coeff 导致无法运行",
          "situation": "输入中声明了 pair_style eam，但没有给出 pair_coeff 指定势文件。",
          "risk": "run 之前报 ERROR: All pair coeffs are not set，运行无法开始。",
          "fix": "在 pair_style 之后加 pair_coeff * * <势文件>，并确认势文件路径可读。",
          "check": "运行能进入第 0 步 thermo 输出。",
          "tags": [("topic", "input-setup")], "log": "log_C.txt", "line": 26},
}

QUESTIONS = [
    {"q": "运行时报 Lost atoms，怎么办？", "scope": {"topic": ["原子丢失"]}, "want": {"A"}},
    {"q": "NPT 密度一直振荡", "scope": {"topic": ["NPT"]}, "want": {"B", "case", "ref"}},
    {"q": "NPT 振荡且只看经验教训", "scope": {"topic": ["NPT"]}, "types": ["lesson"], "want": {"B"}},
    {"q": "所有运行失败经验（父标签）", "scope": {"topic": ["故障"]}, "want": {"A", "C"}},
    {"q": "LJ 体系的问题", "scope": {"object": ["LJ"]}, "want": {"A", "B"}},
    {"q": "铜的平衡密度怎么算", "scope": {"topic": ["密度"], "object": ["铜"]}, "want": {"case"}},
    {"q": "MD 领域全部", "scope": {"domain": ["MD"]}, "want": {"A", "B", "C", "case", "ref"}},
]


def main(scratch, suite):
    if os.path.exists(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch)
    root = os.path.join(scratch, "kb")
    inbox = os.path.join(scratch, "inbox")
    ws = os.path.join(scratch, "ws")
    os.makedirs(inbox)
    os.makedirs(ws)
    rc, init = cli(root, "init", "--admin", "boss")
    tok = init["token"]
    for k, v in (("intake_roots", [inbox]), ("checkout_roots", [ws])):
        cli(root, "setting", k, json.dumps(v), "--admin", "boss", "--token", tok, "--reason", "audit setup")
    pack = os.path.join(scratch, "domain_md")
    os.makedirs(pack)
    for name, data in DOMAIN_PACK.items():
        with open(os.path.join(pack, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    rc, out = cli(root, "load-pack", pack)
    check("domain pack loaded", rc == 0, out)

    logs_dir = os.path.join(suite, "l0d-log-diagnosis", "assets")
    for n in ("A", "B", "C"):
        shutil.copy(os.path.join(logs_dir, "log_%s.txt" % n), os.path.join(inbox, "log_%s.txt" % n))
    ref = json.load(open(os.path.join(suite, "md01-cu-npt", "reference", "reference.json"), encoding="utf-8"))
    shutil.copy(os.path.join(suite, "md01-cu-npt", "assets", "Cu_u3.eam"), os.path.join(inbox, "Cu_u3.eam"))
    in_npt = ("units metal\nboundary p p p\nlattice fcc 3.615\nregion box block 0 5 0 5 0 5\ncreate_box 1 box\n"
              "create_atoms 1 box\npair_style eam\npair_coeff 1 1 Cu_u3.eam\nvelocity all create 300 4928 dist gaussian\n"
              "timestep 0.001\nfix 1 all npt temp 300 300 0.1 iso 0 0 1.0\nthermo 100\nrun 8000\n")
    with open(os.path.join(inbox, "in.npt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(in_npt)
    report_md = ("# NPT 平衡\n\n500 原子 fcc 超胞，NPT 300 K / 0 bar，平衡 4000 步、生产 4000 步。\n\n"
                 "# 结果\n\n密度 %.4f g/cm3（运行内涨落约 %.3f），300 K 晶格常数 %.4f Å。两个随机种子差 %.4f。\n"
                 % (ref["reference"]["density_g_cm3"], ref["reference"]["density_within_run_std"],
                    ref["reference"]["lattice_const_A_at_300K"], ref["reference"]["density_seed_spread"]))
    with open(os.path.join(inbox, "report.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(report_md)
    import fitz
    doc = fitz.open()
    for text in ("Thermostat and barostat damping guide (drill document).",
                 "Recommended Pdamp is about 1000 timesteps; Tdamp about 100 timesteps."):
        doc.new_page().insert_text((72, 72), text, fontsize=11)
    doc.save(os.path.join(inbox, "damping.pdf"))
    doc.close()

    writer = MCP(root, "writer")
    check("server instructions tell agents to contribute",
          "Contribute after you act" in writer.init.get("instructions", ""))
    err, guide = writer.call("ukb_guide")
    check("guide returns types and facets", not err and len(guide["types"]) >= 10 and guide["facets"])

    submitted = {}
    for key, f in LOG_FACTS.items():
        tags = [{"facet": "domain", "tag": "分子动力学"}, {"facet": "tool", "tag": "lmp"}] + \
               [{"facet": fa, "tag": tg} for fa, tg in f["tags"]]
        err, prep = writer.call("ukb_prepare", {"type_id": "lesson", "tags": tags})
        check("prepare %s ok" % key, not err, prep if err else None)
        payload = {"mode": "new", "type_id": "lesson", "title": f["title"],
                   "fields": {"situation": f["situation"], "risk": f["risk"], "fix": f["fix"], "check": f["check"]},
                   "tags": tags,
                   "conditions": [{"key": "units_style", "value": "lj" if key != "C" else "metal"}]
                   if key != "C" else [],
                   "sources": [{"source_type": "run_artifact", "title": "故障日志 %s" % key,
                                "locator": "automd-microbench-mini/suite/l0d-log-diagnosis/assets/log_%s.txt" % key,
                                "locator_detail": "L%d" % f["line"]}],
                   "origin": {"kind": "task", "ref": "l0d-log-diagnosis",
                              "summary_of_evidence": "根因已由评测方从生成故障的真实输入确认（reference.json）",
                              "excerpt": open(os.path.join(logs_dir, "log_%s.txt" % key), encoding="utf-8")
                              .read().splitlines()[f["line"] - 1].strip()}}
        err, r = writer.call("ukb_propose", {"payload": payload})
        check("propose lesson %s" % key, not err, r if err else None)
        submitted[key] = (r, payload)

    case = {"mode": "new", "type_id": "case", "title": "fcc 铜 NPT 300 K 平衡密度与晶格常数（已验证）",
            "fields": {"summary": "500 原子 fcc Cu，EAM 势，NPT 300 K/0 bar 8000 步，得到平衡密度与热晶格常数。",
                       "goal": "300 K、0 bar 下的平衡密度与晶格常数。",
                       "setup": "5x5x5 超胞 [[in.npt:L4]]，势文件 [[in.npt:L8]]，NPT 阻尼 [[in.npt:L11]]，步长 [[in.npt:L10]]",
                       "outcome": "密度 %.4f g/cm3、晶格常数 %.4f Å [[report.md#s2]]"
                                  % (ref["reference"]["density_g_cm3"], ref["reference"]["lattice_const_A_at_300K"]),
                       "findings": ["Tdamp=0.1 ps、Pdamp=1.0 ps（100/1000 步）下 4000 步即可平衡。"],
                       "result_reliability": "verified",
                       "reuse": {"entry_files": ["in.npt"],
                                 "parameters_to_adapt": [
                                     {"name": "温度", "location": "in.npt:L11", "meaning": "目标温度", "original": "300"},
                                     {"name": "超胞", "location": "in.npt:L4", "meaning": "体系尺寸", "original": "5x5x5"}],
                                 "prerequisites": ["势文件与输入同目录"], "verified_env": "LAMMPS 2 Aug 2023",
                                 "known_limits": ["500 原子有限尺寸效应", "8000 步统计"]}},
            "tags": [{"facet": "domain", "tag": "MD"}, {"facet": "topic", "tag": "平衡性质计算"},
                     {"facet": "topic", "tag": "系综控制"}, {"facet": "object", "tag": "fcc"},
                     {"facet": "tool", "tag": "lammps"}],
            "conditions": [{"key": "temperature_k", "value": 300}, {"key": "pressure_bar", "value": 0},
                           {"key": "units_style", "value": "metal"}],
            "sources": [{"source_type": "run_artifact", "title": "评测方双实现参考",
                         "locator": "automd-microbench-mini/suite/md01-cu-npt/reference/reference.json"}],
            "origin": {"kind": "run", "ref": "md01-cu-npt", "summary_of_evidence": "两个种子独立运行 + 0 K 静态验证"},
            "files": [{"path": "in.npt", "role": "input", "description": "主输入", "source_path": os.path.join(inbox, "in.npt")},
                      {"path": "Cu_u3.eam", "role": "parameter_file", "description": "EAM 势文件（冻结版本）",
                       "source_path": os.path.join(inbox, "Cu_u3.eam")},
                      {"path": "report.md", "role": "report", "description": "结果报告",
                       "source_path": os.path.join(inbox, "report.md")}]}
    err, prep = writer.call("ukb_prepare", {"type_id": "case", "tags": case["tags"]})
    check("case prepare sees lesson B in scope",
          not err and any(x["entry_id"] == submitted["B"][0]["entry_id"] for x in prep["open_proposals_in_scope"]),
          None if not err else prep)
    err, rc_ = writer.call("ukb_propose", {"payload": case})
    check("propose case", not err, rc_ if err else None)
    submitted["case"] = (rc_, case)

    with open(os.path.join(inbox, "damping.pdf"), "rb") as fh:
        pdf_b64 = base64.b64encode(fh.read()).decode()
    refp = {"mode": "new", "type_id": "reference", "title": "恒温恒压阻尼取值指南（演练文档）",
            "fields": {"citation": "Drill document, 2026", "summary": "给出恒温器、恒压器阻尼时间的推荐取值。",
                       "key_points": ["Pdamp 约 1000 个时间步 [[damping.pdf#p2]]",
                                      "Tdamp 约 100 个时间步 [[damping.pdf#p2]]"]},
            "tags": [{"facet": "domain", "tag": "MD"}, {"facet": "topic", "tag": "系综控制"}],
            "origin": {"kind": "document", "ref": "damping.pdf", "summary_of_evidence": "用户提供的文档"},
            "files": [{"path": "damping.pdf", "role": "fulltext", "description": "全文", "content_b64": pdf_b64}]}
    err, rr = writer.call("ukb_propose", {"payload": refp})
    check("propose reference", not err, rr if err else None)
    submitted["ref"] = (rr, refp)

    # a bad tag choice the prereviewer should flag, and a duplicate the prereviewer should catch
    dup = json.loads(json.dumps(submitted["A"][1]))
    dup["title"] = "步长过大导致 Lost atoms"
    dup["fields"]["situation"] = "LJ 体系中把时间步设得太大（0.025），初始温度又高。"
    dup["tags"] = [{"facet": "domain", "tag": "MD"}, {"facet": "topic", "tag": "run-failure"}]
    err, rd = writer.call("ukb_propose", {"payload": dup})
    check("propose near-duplicate", not err, rd if err else None)

    err, selfq = writer.call("ukb_prereview_queue")
    check("writer sees none of its own tasks in prereview queue", not err and selfq["tasks"] == [])
    err, own = writer.call("ukb_prereview_submit", {"task_id": submitted["A"][0]["task_id"], "page_tokens": [],
                                                    "opinion": {}})
    check("writer cannot prereview own submission", err and own["error"] == "SELF_PREREVIEW", own)

    checker = MCP(root, "checker")
    err, q = checker.call("ukb_prereview_queue")
    check("checker sees all 6 proposals", not err and len(q["tasks"]) == 6, len(q["tasks"]) if not err else q)
    decisions = {}
    for task in q["tasks"]:
        tid = task["task_id"]
        err, b = checker.call("ukb_prereview_bundle", {"task_id": tid})
        pages = [b] + [checker.call("ukb_prereview_bundle", {"task_id": tid, "page": i})[1] for i in range(1, b["pages"])]
        parts = {it["part"] for p in pages for it in p["items"]}
        pend = [it["content"] for p in pages for it in p["items"] if it["part"] == "pending"][0]
        scope_ids = [it["content"]["entry_id"] for p in pages for it in p["items"] if it["part"] == "scope"]
        op = {"type_opinion": {"ok": True, "note": "类型符合内容"}, "tag_opinion": {"ok": True, "note": "按说明归类"},
              "checklist": [{"item": c, "ok": True, "note": "已核对"} for c in b["instructions"]["checklist"]],
              "recommendation": "approve", "note": "已读完全部材料"}
        if b["instructions"]["document"]:
            op["summary_fidelity"] = [{"claim": "关键数值", "locator": "已核对", "ok": True, "note": "与原文一致"}]
        if pend["title"] == dup["title"]:
            a_id = submitted["A"][0]["entry_id"]
            check("prereview of duplicate sees original A in scope", a_id in scope_ids, scope_ids)
            check("prereview sees taxonomy", "taxonomy" in parts)
            op["duplicate_of"] = [{"entry_id": a_id, "note": "与 A 讲同一件事"}]
            op["tag_opinion"] = {"ok": False, "drop": ["topic/run-failure"],
                                 "add": [{"facet": "topic", "tag": "integration-stability"}],
                                 "note": "积分稳定性子标签更合适"}
            op["recommendation"] = "reject"
            decisions[tid] = ("reinforce_into", {"target_entry_id": a_id})
        elif "原子丢失" in pend["title"]:
            decisions[tid] = ("approve", None)
        elif "pair_coeff" in pend["title"]:
            op["tag_opinion"] = {"ok": False, "add": [{"facet": "object", "tag": "fcc"}],
                                 "note": "eam 场景，补 fcc 对象标签"}
            decisions[tid] = ("approve", {"tag_decisions": {"add": [{"facet": "object", "tag": "fcc"}]}})
        else:
            decisions[tid] = ("approve", None)
        tokens = [p["page_token"] for p in pages]
        err, res = checker.call("ukb_prereview_submit", {"task_id": tid, "page_tokens": tokens, "opinion": op})
        check("prereview %s" % pend["title"][:14], not err, res if err else None)
    checker.close()

    # admin over HTTP
    kb = UKB(root)
    console = Console(kb)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(console))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def req(method, path, form=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        body = urllib.parse.urlencode(form) if form else None
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if cookie:
            h["Cookie"] = cookie
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        hd = dict(r.getheaders())
        c.close()
        return r.status, hd, data

    st, h, _ = req("POST", "/login", {"reviewer_id": "boss", "token": tok})
    cookie = h["Set-Cookie"].split(";")[0]
    csrf = console.sessions[cookie.split("=", 1)[1]]["csrf"]
    st, _, page = req("GET", "/", cookie=cookie)
    check("queue page shows prereview recommendation", "建议驳回" in page and "建议批准" in page)
    for tid, (dec, args) in decisions.items():
        v = kb.review_view(tid)
        if dec == "approve" and not args:
            st, _, _ = req("POST", "/task/" + tid, {"csrf": csrf, "bound_hash": v["bound_hash"], "decision": "approve",
                                                   "back": "/"}, cookie=cookie)
            check("one-click approve %s" % tid, st == 303)
        else:
            form = {"csrf": csrf, "bound_hash": v["bound_hash"], "decision": dec, "rationale": "按预审意见处理"}
            if args and "target_entry_id" in args:
                form["target_entry_id"] = args["target_entry_id"]
            if args and "tag_decisions" in args:
                kb.decide(tid, "boss", tok, dec, "按预审意见补标签", v["bound_hash"], "webui", args)
                continue
            st, _, body = req("POST", "/task/" + tid, form, cookie=cookie)
            check("admin %s %s" % (dec, tid), st == 303, body[:300] if st != 303 else None)
    httpd.shutdown()
    httpd.server_close()

    # ---- row-level checks ----
    c = sqlite3.connect(os.path.join(root, "ukb.sqlite"))
    c.row_factory = sqlite3.Row
    ids = {k: v[0]["entry_id"] for k, v in submitted.items()}
    for key in ("A", "B", "C", "case", "ref"):
        e = c.execute("SELECT * FROM entry WHERE entry_id=?", (ids[key],)).fetchone()
        rev = c.execute("SELECT * FROM entry_revision WHERE rev_id=?", (e["head_rev"],)).fetchone()
        payload = submitted[key][1]
        check("%s active" % key, e["status"] == "active", e["status"])
        check("%s title stored exactly" % key, rev["title"] == payload["title"])
        stored = json.loads(rev["fields_json"])
        check("%s fields stored exactly" % key, stored == payload["fields"],
              None if stored == payload["fields"] else {"stored": stored})
        conf = {(r["facet_id"], r["slug"]) for r in c.execute(
            "SELECT t.facet_id, t.slug FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id WHERE et.entry_id=? AND "
            "et.state='confirmed'", (ids[key],))}
        check("%s has confirmed domain and topic tags" % key,
              any(f == "domain" for f, _ in conf) and any(f == "topic" for f, _ in conf), sorted(conf))
        check("%s no suggested tags left after approval" % key, c.execute(
            "SELECT count(*) FROM entry_tag WHERE entry_id=? AND state='suggested'", (ids[key],)).fetchone()[0] == 0)
        conds = {r["key_id"]: json.loads(r["value_json"]) for r in c.execute(
            "SELECT * FROM entry_condition WHERE rev_id=?", (rev["rev_id"],))}
        want = {x["key"]: x["value"] for x in payload.get("conditions", [])}
        check("%s conditions stored" % key, conds == want, conds)
        nsrc = c.execute("SELECT count(*) FROM entry_source WHERE rev_id=?", (rev["rev_id"],)).fetchone()[0]
        check("%s sources stored" % key, nsrc == len(payload.get("sources", [])), nsrc)
        org = json.loads(c.execute("SELECT context_json FROM intake WHERE result_entry_id=?",
                                   (ids[key],)).fetchone()[0])
        check("%s origin stored" % key, org == payload["origin"])
    c_tags = {r[0] for r in c.execute("SELECT t.slug FROM entry_tag et JOIN tag t ON t.tag_id=et.tag_id WHERE "
                                      "et.entry_id=? AND et.state='confirmed'", (ids["C"],))}
    check("C got the admin-added fcc tag", "fcc-metal" in c_tags, sorted(c_tags))
    case_rev = c.execute("SELECT head_rev FROM entry WHERE entry_id=?", (ids["case"],)).fetchone()[0]
    for path in ("in.npt", "Cu_u3.eam", "report.md"):
        row = c.execute("SELECT * FROM doc_file WHERE rev_id=? AND path=?", (case_rev, path)).fetchone()
        with open(os.path.join(inbox, path), "rb") as fh:
            want = hashlib.sha256(fh.read()).hexdigest()
        check("case file %s byte-identical" % path, row is not None and row["sha256"] == want)
    secs = [r["title"] for r in c.execute("SELECT title FROM doc_section WHERE rev_id=? AND path='report.md' ORDER BY ord",
                                          (case_rev,))]
    check("case report sections found", secs == ["NPT 平衡", "结果"], secs)
    dup_e = c.execute("SELECT * FROM entry WHERE entry_id=?", (rd["entry_id"],)).fetchone()
    check("duplicate rejected, not visible", dup_e["status"] == "rejected")
    a_src = {r[0] for r in c.execute("SELECT es.source_id FROM entry_source es JOIN entry e ON e.head_rev=es.rev_id "
                                     "WHERE e.entry_id=?", (ids["A"],))}
    reinf = {r[0] for r in c.execute("SELECT source_id FROM reinforcement WHERE entry_id=?", (ids["A"],))}
    check("duplicate citing the same log adds no fake reinforcement to A", reinf == set(),
          {"a_sources": sorted(a_src), "reinforcements": sorted(reinf)})
    check("duplicate_of relation recorded", c.execute(
        "SELECT count(*) FROM relation WHERE src_entry=? AND rel_id='duplicate_of' AND dst_entry=?",
        (rd["entry_id"], ids["A"])).fetchone()[0] == 1)
    check("candidate tags none left", c.execute("SELECT count(*) FROM tag WHERE status='candidate'").fetchone()[0] == 0)
    c.close()

    # ---- reader side: tag routing ----
    reader = MCP(root, "reader")
    keyof = {v: k for k, v in ids.items()}
    for qq in QUESTIONS:
        args = {"scope": qq["scope"]}
        if qq.get("types"):
            args["types"] = qq["types"]
        err, cat = reader.call("ukb_catalog", dict(args, format="json"))
        got = {keyof.get(x["ref"].split("@")[0], "?") for x in cat.get("items", [])}
        check("route: %s" % qq["q"], not err and got == qq["want"], {"got": sorted(got), "want": sorted(qq["want"])})
    err, c_ = reader.call("ukb_catalog", {"scope": {"topic": ["原子丢失"]}, "format": "json"})
    err, rd_ = reader.call("ukb_read", {"entry_refs": [i["ref"] for i in c_["items"]][:3], "format": "json"})
    check("read returns full lesson text", not err and rd_["items"][0]["fields"]["fix"] == LOG_FACTS["A"]["fix"])
    err, orig = reader.call("ukb_read_original", {"format": "json", "ref": ids["case"], "path": "in.npt", "lines": "L11"})
    check("case locator L11 is the npt line", not err and orig["text"].startswith("fix 1 all npt"), orig.get("text"))
    err, pg = reader.call("ukb_read_original", {"format": "json", "ref": ids["ref"], "path": "damping.pdf", "page": "p2"})
    check("reference p2 contains Pdamp", not err and "Pdamp" in pg["text"])
    err, mine = writer.call("ukb_my_submissions")
    states = sorted(i["task_state"] for i in mine["items"])
    check("writer sees outcome of all 6", states == ["approved"] * 5 + ["rejected"], states)
    reader.close()
    writer.close()
    rc, lint = cli(root, "lint")
    check("lint ok", lint["ok"], [i for i in lint["issues"] if i["code"] != "EMPTY_TAG"])
    rc, au = cli(root, "verify-audit")
    check("audit chain intact", au["ok"], au)
    kb.close()

    failed = [c_ for c_ in CHECKS if not c_["ok"]]
    print(json.dumps({"checks": len(CHECKS), "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
