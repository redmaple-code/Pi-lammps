"""Production test: inject a metal-MD corpus through the real deployment and exercise everything.

Deployment under test (all real processes):
  - `python -m ukb init / setting / load-pack` via CLI
  - `python -m ukb serve` as a separate HTTP process (admin console)
  - several writer agents as concurrent `python -m ukb.mcp_server` subprocesses
  - one prereviewer agent (MCP subprocess) that reads every bundle and writes an opinion
  - the admin acting over HTTP only (login, queue one-click approve, task page decisions)

Usage: python -m tools.prodtest.run <kb_root> [--keep]
The KB is left in place for later inspection and cleanup (see cleanup.py). Prints a JSON report.
"""
import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE = os.path.dirname(HERE)
BENCH = os.path.join(WORKSPACE, "automd-microbench-mini")
sys.path.insert(0, HERE)

from tools.prodtest import corpus_docs, corpus_entries  # noqa: E402
from tools.prodtest.corpus_entries import BATCH  # noqa: E402

ENV = dict(os.environ, PYTHONPATH=HERE, PYTHONIOENCODING="utf-8")
REPORT = {"checks": [], "timings": {}, "counts": {}}
PROCS = []


def check(name, ok, detail=None):
    REPORT["checks"].append({"check": name, "ok": bool(ok), "detail": detail if not ok else None})
    return ok


def cli(root, *args):
    p = subprocess.run([sys.executable, "-m", "ukb", "--root", root] + list(args), capture_output=True, env=ENV,
                       timeout=600, cwd=HERE)
    out = p.stdout.decode("utf-8")
    try:
        return p.returncode, json.loads(out)
    except ValueError:
        return p.returncode, {"raw": out, "err": p.stderr.decode("utf-8")[-2000:]}


class MCP:
    def __init__(self, root, agent):
        self.agent = agent
        self.p = subprocess.Popen([sys.executable, "-m", "ukb.mcp_server", "--root", root, "--agent", agent],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV,
                                  cwd=HERE)
        PROCS.append(self.p)
        self.n = 0
        self.lock = threading.Lock()
        self.init = self._rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.calls = 0

    def _rpc(self, method, params):
        with self.lock:
            self.n += 1
            self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params},
                                           ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()
            line = self.p.stdout.readline().decode("utf-8")
        return json.loads(line)["result"]

    def call(self, name, args=None):
        self.calls += 1
        r = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        return r.get("isError", False), json.loads(r["content"][0]["text"])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=60)
        self.p.stdout.close()
        self.p.stderr.close()


class Web:
    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None

    def req(self, method, path, form=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        body = urllib.parse.urlencode(form) if form else None
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if self.cookie:
            h["Cookie"] = self.cookie
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        hd = dict(r.getheaders())
        c.close()
        return r.status, hd, data

    def login(self, admin, token):
        st, h, _ = self.req("POST", "/login", {"reviewer_id": admin, "token": token})
        if st != 303:
            return False
        self.cookie = h["Set-Cookie"].split(";")[0]
        st, _, page = self.req("GET", "/")
        m = re.search(r'name="csrf" value="([^"]+)"', page)
        self.csrf = m.group(1) if m else None
        return True

    def task_hash(self, task_id):
        st, _, page = self.req("GET", "/task/" + task_id)
        m = re.search(r'name="bound_hash" value="([0-9a-f]{64})"', page)
        return (m.group(1) if m else None), page

    def decide(self, task_id, decision, rationale="", **extra):
        h, page = self.task_hash(task_id)
        if not h:
            return 0, page
        form = {"csrf": self.csrf, "bound_hash": h, "decision": decision, "rationale": rationale}
        form.update(extra)
        st, _, body = self.req("POST", "/task/" + task_id, form)
        return st, body


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------- prereviewer judgment ----------------

def _judge(bundle_pages):
    """A deterministic stand-in for an LLM prereviewer, but reading the real bundle: it checks what a careful
    reviewer would check from the material and records concrete findings."""
    items = [it for p in bundle_pages for it in p["items"]]
    pending = [it["content"] for it in items if it["part"] == "pending"][0]
    scope = [it["content"] for it in items if it["part"] == "scope"]
    originals = {it["content"]["path"]: it["content"] for it in items if it["part"] == "original"}
    tax = [it["content"] for it in items if it["part"] == "taxonomy"]
    findings = {"duplicate_of": [], "contradicts": [], "tag_notes": []}
    title = pending["title"]
    for s in scope:
        if s["title"] == title or (s["title"][:12] == title[:12] and len(title) > 12):
            findings["duplicate_of"].append({"entry_id": s["entry_id"], "note": "标题与内容讲同一件事：" + s["title"]})
    # tag sanity: every tag on the entry must exist in taxonomy with a description
    known = {(f["facet"], t["tag"]) for f in (tax[0] if tax else []) for t in f["tags"]}
    for t in pending.get("tags", []):
        if (t["facet"], t["tag"]) not in known:
            findings["tag_notes"].append("标签 %s/%s 不在标签树中" % (t["facet"], t["tag"]))
    # parent-only tags where a child exists
    children = {}
    for f in (tax[0] if tax else []):
        for t in f["tags"]:
            if t["parent"]:
                children.setdefault((f["facet"], t["parent"]), []).append(t["tag"])
    for t in pending.get("tags", []):
        kids = children.get((t["facet"], t["tag"]))
        mine = {x["tag"] for x in pending["tags"] if x["facet"] == t["facet"]}
        if kids and not (set(kids) & mine):
            findings["tag_notes"].append("只挂了父标签 %s/%s，可考虑子标签 %s" % (t["facet"], t["tag"], "、".join(kids)))
    # summary fidelity for documents: every [[path:Lx]] / [[path#pN]] locator text must exist in originals
    fidelity = []
    if originals:
        blob = json.dumps(pending["fields"], ensure_ascii=False)
        for m in re.finditer(r"\[\[([^\]]+)\]\]", blob):
            loc = m.group(1)
            path = re.split(r"[#:]", loc, 1)[0]
            o = originals.get(path)
            ok = o is not None and (o.get("readable") is False or bool(o.get("text")))
            fidelity.append({"claim": loc, "locator": loc, "ok": ok,
                             "note": "原文存在且可读" if ok else "原文缺失或不可读"})
        if not fidelity:
            fidelity.append({"claim": "整体", "locator": "-", "ok": True, "note": "无定位"})
    return pending, findings, fidelity


def prereview(agent, task_id):
    err, b = agent.call("ukb_prereview_bundle", {"task_id": task_id})
    if err:
        return err, b, None
    pages = [b] + [agent.call("ukb_prereview_bundle", {"task_id": task_id, "page": i})[1] for i in range(1, b["pages"])]
    pending, f, fidelity = _judge(pages)
    op = {"checklist": [{"item": c, "ok": True, "note": "已核对"} for c in b["instructions"]["checklist"]],
          "duplicate_of": f["duplicate_of"], "recommendation": "reject" if f["duplicate_of"] else "approve",
          "note": "读完 %d 页材料；%s" % (len(pages), "；".join(f["tag_notes"]) or "未发现问题")}
    if b["kind"] in ("new", "revise"):
        op["type_opinion"] = {"ok": True, "note": "类型与内容一致"}
        op["tag_opinion"] = {"ok": not f["tag_notes"], "note": "；".join(f["tag_notes"]) or "按标签说明归类"}
    if b["instructions"]["document"]:
        op["summary_fidelity"] = fidelity
    err, res = agent.call("ukb_prereview_submit", {"task_id": task_id, "page_tokens": [p["page_token"] for p in pages],
                                                   "opinion": op})
    return err, res, {"pending": pending, "findings": f, "pages": len(pages)}


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if "prodtest" not in root.replace("\\", "/"):
        sys.exit("refusing to reset a directory whose path does not contain 'prodtest': " + root)
    if os.path.exists(root):
        shutil.rmtree(root)
    base = os.path.dirname(root)
    inbox = os.path.join(base, os.path.basename(root) + "_inbox")
    ws = os.path.join(base, os.path.basename(root) + "_ws")
    for d in (inbox, ws):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d)
    t0 = time.time()

    # ---- 1. deploy via CLI ----
    rc, init = cli(root, "init", "--admin", "admin")
    check("cli init", rc == 0, init)
    tok = init["token"]
    for k, v in (("intake_roots", [inbox]), ("checkout_roots", [ws]), ("agent_daily_proposal_limit", 40)):
        rc, out = cli(root, "setting", k, json.dumps(v), "--admin", "admin", "--token", tok, "--reason", "prodtest")
        check("cli setting " + k, rc == 0 and out.get("state") == "approved", out)
    rc, out = cli(root, "load-pack", os.path.join(HERE, "data", "domain", "metal_md"))
    check("cli load-pack metal_md", rc == 0, out)
    REPORT["counts"]["domain_pack_items"] = out.get("items")

    port = free_port()
    srv = subprocess.Popen([sys.executable, "-m", "ukb", "--root", root, "serve", "--port", str(port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV, cwd=HERE)
    PROCS.append(srv)
    web = Web(port)
    for _ in range(50):
        try:
            web.req("GET", "/")
            break
        except OSError:
            time.sleep(0.2)
    check("console process up", srv.poll() is None)
    check("admin login over HTTP", web.login("admin", tok))
    check("bad token refused", Web(port).login("admin", "wrong") is False)

    # ---- 2. corpus files ----
    payloads = []
    for e in corpus_entries.ENTRIES:
        p = json.loads(json.dumps(e))
        p["mode"] = "new"
        payloads.append(p)
    case_payloads, copies = corpus_docs.cases(BENCH)
    for rel, src in copies.items():
        dst = os.path.join(inbox, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    for c in case_payloads:
        c = json.loads(json.dumps(c))
        c["mode"] = "new"
        for f in c["files"]:
            f["source_path"] = os.path.join(inbox, *f.pop("src").split("/"))
        payloads.append(c)
    for r in corpus_docs.references(inbox):
        r["mode"] = "new"
        payloads.append(r)
    REPORT["counts"]["corpus"] = len(payloads)

    # ---- 3. concurrent writers ----
    writers = [MCP(root, "writer-%d" % i) for i in range(4)]
    check("server instructions ask agents to contribute",
          all("Contribute after you act" in w.init.get("instructions", "") for w in writers))
    err, guide = writers[0].call("ukb_guide")
    check("guide lists facets and types only (staged)", not err and guide["facets"] and "tags" not in guide["facets"][0])
    err, kids = writers[0].call("ukb_overview", {"facet": "topic", "parent": "potential", "format": "json"})
    check("domain child tags reachable via overview", not err and any(t["tag"] == "potential-format" for t in kids["tags"]))
    results, errors = {}, []

    def work(w, items):
        for idx, p in items:
            err, prep = w.call("ukb_prepare", {"type_id": p["type_id"], "tags": p["tags"]})
            if err:
                errors.append({"title": p["title"], "stage": "prepare", "err": prep})
                continue
            err, r = w.call("ukb_propose", {"payload": p})
            if err:
                errors.append({"title": p["title"], "stage": "propose", "err": r})
            else:
                results[idx] = (w.agent, r, p)

    t1 = time.time()
    shards = [[] for _ in writers]
    for i, p in enumerate(payloads):
        shards[i % len(writers)].append((i, p))
    ths = [threading.Thread(target=work, args=(w, s)) for w, s in zip(writers, shards)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    REPORT["timings"]["concurrent_propose_s"] = round(time.time() - t1, 1)
    check("all corpus items accepted for review", not errors and len(results) == len(payloads),
          {"errors": errors[:10], "accepted": len(results), "total": len(payloads)})

    # ---- 4. deliberate bad / edge submissions ----
    w0 = writers[0]
    bad_cases = {
        "trajectory file": dict(case_payloads[0], title="带轨迹的案例",
                                files=[{"path": "in.npt", "role": "input", "description": "x",
                                        "source_path": os.path.join(inbox, "npt", "in.npt")},
                                       {"path": "dump.melt", "role": "data", "description": "轨迹",
                                        "content_text": "ITEM: TIMESTEP\n0\n"}]),
        "no origin": {k: v for k, v in payloads[0].items() if k != "origin"},
        "outside intake root": dict(case_payloads[0], title="越界读取",
                                    files=[{"path": "in.npt", "role": "input", "description": "x",
                                            "source_path": os.path.join(BENCH, "README.md")}]),
        "unknown tag": dict(payloads[0], title="未知标签", tags=[{"facet": "topic", "tag": "量子色动力学"}]),
        "bad locator": dict(case_payloads[0], title="定位失效",
                            fields=dict(case_payloads[0]["fields"], setup="见 [[in.npt:L999]]"),
                            files=[{"path": "in.npt", "role": "input", "description": "x",
                                    "source_path": os.path.join(inbox, "npt", "in.npt")}]),
        "secret in text": dict(payloads[0], title="含凭证",
                               fields=dict(payloads[0]["fields"], fix="api_key = sk-ABCDEF1234567890 然后运行")),
    }
    want = {"trajectory file": "FILE_TYPE_DENIED", "no origin": "ORIGIN_REQUIRED",
            "outside intake root": "FILE_SOURCE_DENIED", "unknown tag": "TAG_UNKNOWN",
            "bad locator": "LOCATOR_INVALID"}
    quarantined = None
    for name, p in bad_cases.items():
        p = json.loads(json.dumps(p))
        p["mode"] = "new"
        if "files" in p:
            for f in p["files"]:
                f.pop("src", None)
        err, r = w0.call("ukb_propose", {"payload": p})
        if name == "secret in text":
            check("secret goes to quarantine", not err and r.get("quarantined"), r)
            quarantined = r
        else:
            check("refuse: " + name, err and r.get("error") == want[name], r)
    # duplicate submitted by another agent, and a new-tag proposal
    dup = json.loads(json.dumps(payloads[0]))
    dup["title"] = payloads[0]["title"]
    dup["sources"] = [{"source_type": "dialogue", "title": "用户转述", "locator": BATCH + "/dialogue-1"}]
    err, rdup = writers[1].call("ukb_propose", {"payload": dup})
    if not check("near-duplicate accepted for review", not err, rdup):
        raise RuntimeError("duplicate refused: %s" % rdup)
    newtag = {"mode": "new", "type_id": "lesson", "title": "冲击加载需要 NVE 与 piston 而不是恒温器",
              "fields": {"situation": "用恒温器控制冲击波模拟温度。", "risk": "恒温器会吸收冲击能量，冲击后状态错误。",
                         "fix": "冲击区用 NVE，piston 或动量镜驱动；只在远端加阻尼层。", "check": "Hugoniot 关系满足。"},
              "tags": [corpus_entries.D, {"facet": "topic", "tag": "mechanical-loading"},
                       {"facet": "topic", "new_tag": {"slug": "shock", "label": "冲击加载", "parent": "mechanical-loading",
                                                      "description": "冲击波、piston、Hugoniot 相关", "aliases": ["冲击"]},
                        "reason": "力学加载下没有冲击子类"}],
              "sources": [corpus_entries.SRC_EST], "origin": corpus_entries.ORIGIN_TEXTBOOK}
    err, rnew = writers[2].call("ukb_propose", {"payload": newtag})
    check("new-tag proposal accepted", not err, rnew)
    err, rclash = writers[2].call("ukb_propose", {"payload": dict(newtag, title="别名冲突", tags=[corpus_entries.D, {
        "facet": "topic", "new_tag": {"slug": "loading2", "label": "加载", "description": "d"}, "reason": "r"}])})
    check("new tag colliding with alias refused", err and rclash.get("error") == "TAG_EXISTS", rclash)

    # ---- 5. prereview ----
    pre = MCP(root, "checker")
    t2 = time.time()
    err, q = pre.call("ukb_prereview_queue", {"limit": 500})
    n_tasks = len(q["tasks"])
    check("prereview queue has everything", n_tasks == len(payloads) + 3, n_tasks)
    err, own = writers[0].call("ukb_prereview_queue", {"limit": 500})
    check("writer queue excludes own submissions",
          all(t["submitter"] != "agent:writer-0" for t in own["tasks"]))
    pre_errors, judged = [], {}
    for t in q["tasks"]:
        err, res, info = prereview(pre, t["task_id"])
        if err:
            pre_errors.append({"task": t["task_id"], "err": res})
        else:
            judged[t["task_id"]] = info
    REPORT["timings"]["prereview_all_s"] = round(time.time() - t2, 1)
    check("all prereviews submitted", not pre_errors, pre_errors[:5])
    dup_task = rdup["task_id"]
    check("prereviewer found the duplicate", judged.get(dup_task, {}).get("findings", {}).get("duplicate_of"),
          judged.get(dup_task, {}).get("findings"))
    tag_notes = {tid: i["findings"]["tag_notes"] for tid, i in judged.items() if i["findings"]["tag_notes"]}
    REPORT["counts"]["prereview_tag_notes"] = len(tag_notes)
    REPORT["prereview_tag_notes_sample"] = [{"title": judged[t]["pending"]["title"], "notes": n}
                                            for t, n in list(tag_notes.items())[:8]]

    # ---- 6. admin over HTTP ----
    t3 = time.time()
    st, _, page = web.req("GET", "/")
    check("queue page shows prereview advice", "建议批准" in page and "建议驳回" in page)
    approve_ok = 0
    special = {dup_task, quarantined["task_id"] if quarantined else None, rnew["task_id"]}
    for t in q["tasks"]:
        if t["task_id"] in special:
            continue
        h, _ = web.task_hash(t["task_id"])
        st, _, _ = web.req("POST", "/task/" + t["task_id"], {"csrf": web.csrf, "bound_hash": h, "decision": "approve",
                                                            "back": "/"})
        approve_ok += st == 303
    check("admin one-click approved corpus", approve_ok == len(payloads), approve_ok)
    target = results[0][1]["entry_id"]
    st, body = web.decide(dup_task, "reinforce_into", "与已有条目重复", target_entry_id=target)
    check("admin: duplicate merged into existing", st == 303, body[:400])
    st, body = web.decide(rnew["task_id"], "approve", "新增冲击子标签")
    check("admin: new tag approved", st == 303, body[:400])
    if quarantined:
        st, body = web.decide(quarantined["task_id"], "reject", "含凭证")
        check("admin: quarantined rejected", st == 303, body[:400])
    REPORT["timings"]["admin_review_s"] = round(time.time() - t3, 1)
    st, _, page = web.req("GET", "/lint")
    check("lint page shows intact audit", "完整" in page)

    # ---- 7. correction round: admin fixes one entry's tags and one entry's content ----
    reader = MCP(root, "reader")
    err, cat = reader.call("ukb_catalog", {"scope": {"topic": ["势函数"]}, "format": "json"})
    first = cat["items"][0]["ref"].split("@")[0]
    rv = {"mode": "revise", "target_entry_id": first, "base_rev_no": 1, "title": cat["items"][0]["title"] + "（修订）",
          "origin": corpus_entries.ORIGIN_TEXTBOOK}
    err, full = reader.call("ukb_read", {"entry_refs": [first], "view": "full", "format": "json"})
    it = full["items"][0]
    rv["fields"] = it["fields"]
    rv["sources"] = [{"source_type": s["source_type_id"], "title": s["title"], "locator": s["locator"]}
                     for s in it["sources"]]
    rv["conditions"] = [{k: c[k] for k in ("key", "op", "value")} for c in it["conditions"]]
    err, rrev = writers[3].call("ukb_propose", {"payload": rv})
    check("revise proposal", not err, rrev)
    if not err:
        prereview(pre, rrev["task_id"])
        st, body = web.decide(rrev["task_id"], "approve", "")
        check("revise approved", st == 303, body[:300])
        err, now = reader.call("ukb_read", {"entry_refs": [first], "format": "json"})
        check("revision is now head", now["items"][0]["rev_no"] == 2)

    # ---- 8. reading like a downstream agent ----
    questions = [
        ("pair_coeff 报错", {"topic": ["元素映射"]}, ["funcfl"]),
        ("Lost atoms 怎么办", {"topic": ["时间步"]}, ["Lost atoms"]),
        ("NPT 阻尼取多少", {"topic": ["NPT"]}, ["Pdamp"]),
        ("拉伸前要做什么", {"topic": ["拉伸"]}, ["零应力"]),
        ("铜的弹性常数", {"property": ["弹性常数"], "object": ["铜"]}, ["C11"]),
        ("应变率影响", {"topic": ["应变率"]}, ["数量级"]),
        ("熔点怎么算", {"property": ["熔点"]}, ["两相共存"]),
        ("扩散系数", {"topic": ["MSD"]}, ["unwrap"]),
        ("续算", {"topic": ["checkpoint"]}, ["reset_timestep"]),
        ("位错分析", {"topic": ["DXA"]}, ["DXA"]),
        ("高熵合金", {"object": ["HEA"]}, ["随机样本"]),
        ("bcc 位错芯", {"object": ["bcc"], "topic": ["缺陷"]}, ["芯"]),
        ("冲击", {"topic": ["冲击"]}, ["piston"]),
    ]
    route = []
    for q_, scope, needles in questions:
        err, cat = reader.call("ukb_catalog", {"scope": scope, "format": "json"})
        if err:
            check("route: " + q_, False, cat)
            continue
        refs = [i["ref"] for i in cat["items"]]
        text, chars = json.dumps(cat, ensure_ascii=False), 0
        for k in range(0, len(refs), 3):
            err, rd = reader.call("ukb_read", {"entry_refs": refs[k:k + 3], "format": "json"})
            if err:
                break
            chars += rd["chars"]
            text += json.dumps(rd, ensure_ascii=False)
            if all(n in text for n in needles):
                break
        hit = not err and all(n in text for n in needles)
        route.append({"q": q_, "catalog": cat["count"], "read_chars": chars, "hit": hit})
        check("route: " + q_, hit, {"catalog": cat["count"]})
    REPORT["routing"] = route
    err, ov = reader.call("ukb_overview", {"facet": "topic", "format": "json"})
    check("overview topic tree (top level)", not err and len(ov["tags"]) >= 10 and all("children" in t for t in ov["tags"]))
    err, kids = reader.call("ukb_overview", {"facet": "topic", "parent": "potential", "format": "json"})
    check("overview children", not err and {t["tag"] for t in kids["tags"]} == {"potential-format", "potential-validation"})
    fresh = MCP(root, "fresh-reader")
    err, nl = fresh.call("ukb_read", {"entry_refs": [first], "format": "json"})
    check("read refused before catalog", err and nl.get("error") == "NOT_LISTED", nl)
    err, nl = fresh.call("ukb_read", {"scope": {"topic": ["NPT"]}, "format": "json"})
    check("scope read refused", err and nl.get("error") == "ARGUMENT_UNKNOWN", nl)
    fresh.close()
    err, mr = reader.call("ukb_must_read", {})
    check("must_read works (empty profile)", not err)

    # documents: summary, original, checkout
    err, big = reader.call("ukb_catalog", {"scope": {"domain": ["MD"]}, "format": "json"})
    check("big catalog capped with narrowing hints", not err and big["shown"] < big["count"] and big.get("narrow_by"))
    err, cases_ = reader.call("ukb_catalog", {"scope": {"domain": ["MD"]}, "types": ["case"], "format": "json"})
    check("four cases visible", not err and cases_["count"] == 4, cases_.get("count"))
    case_refs = {c["title"]: c["ref"].split("@")[0] for c in cases_["items"]}
    npt = [v for k, v in case_refs.items() if "NPT" in k][0]
    tens = [v for k, v in case_refs.items() if "拉伸" in k][0]
    err, rd = reader.call("ukb_read", {"entry_refs": [npt], "format": "json"})
    check("case read returns summary not original", "fix             int all npt" not in json.dumps(rd, ensure_ascii=False))
    err, o = reader.call("ukb_read_original", {"format": "json", "ref": npt, "path": "in.npt", "lines": "L38"})
    check("case locator L38 is the npt fix", not err and o["text"].startswith("fix             int all npt"), o)
    err, o = reader.call("ukb_read_original", {"format": "json", "ref": npt, "path": "log.npt"})
    pages_ = 1
    while not err and o.get("next_cursor"):
        err, o = reader.call("ukb_read_original", {"format": "json", "ref": npt, "path": "log.npt", "cursor": o["next_cursor"]})
        pages_ += 1
    check("145 KB log paged fully", not err and pages_ >= 4, pages_)
    err, o = reader.call("ukb_read_original", {"format": "json", "ref": tens, "path": "report.md", "section": "s3"})
    check("report section readable", not err and o["text"].strip().startswith("#"))
    err, co = reader.call("ukb_checkout", {"ref": tens, "dest": os.path.join(ws, "tension_job"), "task_ref": "prodtest"})
    check("checkout tension case", not err and [f["path"] for f in co["files"]] == ["in.tension"], co)
    if not err:
        import hashlib
        with open(os.path.join(ws, "tension_job", "in.tension"), "rb") as fh:
            got = hashlib.sha256(fh.read()).hexdigest()
        with open(copies["tension/in.tension"], "rb") as fh:
            want_ = hashlib.sha256(fh.read()).hexdigest()
        check("checked-out file byte-identical to benchmark original", got == want_)
    err, co2 = reader.call("ukb_checkout", {"ref": tens, "dest": os.path.join(ws, "tension_job")})
    check("checkout refuses overwrite", err and co2.get("error") == "CHECKOUT_EXISTS", co2)
    err, refs = reader.call("ukb_catalog", {"scope": {"domain": ["MD"]}, "types": ["reference"], "format": "json"})
    mish = [x["ref"].split("@")[0] for x in refs["items"] if "Mishin" in x["title"]][0]
    err, pg = reader.call("ukb_read_original", {"format": "json", "ref": mish, "path": "mishin2001_notes.pdf", "page": "p2"})
    check("Chinese PDF page text extracted cleanly", not err and "层错能" in pg.get("text", ""), pg.get("text"))

    # feedback -> recheck
    err, fb = reader.call("ukb_feedback", {"entry": first, "outcome": "misleading", "note": "生产测试：触发复审"})
    check("negative feedback opens recheck", not err and fb.get("recheck_task"))
    if not err and fb.get("recheck_task"):
        st, body = web.decide(fb["recheck_task"], "approve", "保持", target_status="keep")
        check("admin keeps after recheck", st == 303, body[:300])

    # writer follow-up
    err, mine = writers[1].call("ukb_my_submissions", {"limit": 100})
    states = {}
    for i in mine["items"]:
        states[i.get("task_state") or i["state"]] = states.get(i.get("task_state") or i["state"], 0) + 1
    check("writer-1 sees outcomes", not err and states.get("approved", 0) > 0, states)
    REPORT["counts"]["writer1_states"] = states

    # rate limit
    lim_hit = None
    for k in range(60):
        p = json.loads(json.dumps(payloads[1]))
        p["title"] = "限流测试 %d" % k
        err, r = writers[3].call("ukb_propose", {"payload": p})
        if err and r.get("error") == "RATE_LIMITED":
            lim_hit = k
            break
    check("daily limit enforced", lim_hit is not None, lim_hit)
    REPORT["counts"]["rate_limit_probe_submissions"] = lim_hit
    for i in writers[3].call("ukb_my_submissions", {"limit": 200})[1]["items"]:
        if i.get("title", "") and i["title"].startswith("限流测试") and i.get("task_state") == "open":
            web.decide(i["task_id"], "reject", "限流探针，清理")

    for w in writers + [pre, reader]:
        w.close()
    srv.terminate()
    srv.wait(timeout=30)

    # ---- 9. maintenance ----
    rc, lint = cli(root, "lint")
    serious = [i for i in lint.get("issues", []) if i["code"] not in ("EMPTY_TAG",)]
    REPORT["lint_issues"] = serious
    check("lint ok", lint.get("ok"), serious)
    rc, au = cli(root, "verify-audit")
    check("audit chain intact", au.get("ok"), au)
    rc, st_ = cli(root, "status")
    REPORT["counts"]["status"] = st_.get("entries")
    exp = os.path.join(base, os.path.basename(root) + "_export")
    if os.path.exists(exp):
        shutil.rmtree(exp)
    rc, ex = cli(root, "export", exp)
    check("export", rc == 0, ex)
    bk = os.path.join(base, os.path.basename(root) + "_backup.sqlite")
    if os.path.exists(bk):
        os.remove(bk)
    rc, b = cli(root, "backup", bk)
    check("backup", rc == 0 and os.path.isfile(bk), b)
    REPORT["counts"]["db_mb"] = round(os.path.getsize(os.path.join(root, "ukb.sqlite")) / 1e6, 2)
    blob_bytes = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(os.path.join(root, "blobs")) for f in fs)
    REPORT["counts"]["blobs_mb"] = round(blob_bytes / 1e6, 2)
    REPORT["timings"]["total_s"] = round(time.time() - t0, 1)
    REPORT["paths"] = {"kb": root, "inbox": inbox, "workspace": ws, "export": exp, "backup": bk}
    failed = [c for c in REPORT["checks"] if not c["ok"]]
    REPORT["summary"] = {"checks": len(REPORT["checks"]), "failed": len(failed)}
    print(json.dumps(REPORT, ensure_ascii=False, indent=1))
    return 0 if not failed else 1


def _safe_main():
    import traceback
    try:
        return main()
    except Exception:
        check("harness crashed", False, traceback.format_exc()[-3000:])
        REPORT["summary"] = {"checks": len(REPORT["checks"]),
                             "failed": len([c for c in REPORT["checks"] if not c["ok"]])}
        print(json.dumps(REPORT, ensure_ascii=False, indent=1))
        return 1
    finally:
        for p in PROCS:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)


if __name__ == "__main__":
    sys.exit(_safe_main())
