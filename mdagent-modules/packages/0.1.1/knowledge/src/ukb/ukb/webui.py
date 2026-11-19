"""Admin review console (stdlib http.server). Bind to localhost by default.

The administrator logs in with id + token and patrols the queue: tasks already prereviewed by an AI can be
approved straight from the queue; open a task to read it and approve, correct, send back or reject. Every
decision posts the bound content hash, so a task whose content changed meanwhile is refused.
"""
import html
import http.server
import json
import secrets
import threading
import time
import urllib.parse

from .api import UKB
from .core.errors import UKBError

SESSION_TTL = 8 * 3600
CSS = """
:root{--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--bg:#f6f8fa;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--acc:#0969da}
*{box-sizing:border-box}body{margin:0;font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--fg)}
header{display:flex;gap:16px;align-items:center;padding:10px 20px;border-bottom:1px solid var(--line);background:var(--bg)}
header a{color:var(--acc);text-decoration:none}main{max-width:1100px;margin:0 auto;padding:16px 20px}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--bg);font-weight:600}.muted{color:var(--muted)}.pill{display:inline-block;padding:0 8px;border-radius:10px;border:1px solid var(--line);font-size:12px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);padding:10px;border-radius:6px;max-height:520px;overflow:auto}
section{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0}h2{font-size:16px;margin:4px 0 8px}
textarea{width:100%;min-height:90px;font:13px/1.4 ui-monospace,Consolas,monospace}input[type=text],input[type=password]{padding:6px;width:260px}
button{padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:#fff;cursor:pointer;margin-right:6px}
button.primary{background:var(--ok);color:#fff;border-color:var(--ok)}button.danger{color:var(--bad)}
details summary{cursor:pointer;color:var(--acc)}label{display:block;margin:6px 0 2px;font-weight:600}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
"""


REC_LABEL = {"approve": "建议批准", "approve_as_trial": "建议试用", "request_changes": "建议退回",
             "reject": "建议驳回"}


def esc(x):
    return html.escape("" if x is None else str(x))


def pretty(obj):
    return esc(json.dumps(obj, ensure_ascii=False, indent=2))


class Console:
    def __init__(self, kb):
        self.kb = kb
        self.sessions = {}
        self.lock = threading.Lock()
        self.db_lock = threading.Lock()

    def page(self, title, body, who=None):
        nav = ""
        if who:
            nav = ('<a href="/">待审队列</a><a href="/closed">已处理</a><a href="/lint">巡检</a>'
                   '<span class="muted">管理员：%s</span><form method="post" action="/logout" style="margin-left:auto">'
                   '<input type="hidden" name="csrf" value="%s"><button>退出</button></form>'
                   % (esc(who["reviewer_id"]), esc(who["csrf"])))
        return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>%s · UKB 审核台</title><style>%s</style></head>'
                '<body><header><strong>UKB 审核台</strong>%s</header><main>%s</main></body></html>'
                % (esc(title), CSS, nav, body))

    def login_page(self, msg=""):
        body = ('<h1>登录</h1>%s<form method="post" action="/login"><label for="rid">管理员 ID</label>'
                '<input id="rid" type="text" name="reviewer_id" autocomplete="username" required>'
                '<label for="tok">令牌</label><input id="tok" type="password" name="token" '
                'autocomplete="current-password" required><p><button class="primary">登录</button></p></form>'
                % ('<p class="bad" role="alert">%s</p>' % esc(msg) if msg else ""))
        return self.page("登录", body)

    def queue_page(self, who, state="open"):
        tasks = self.kb.queue(state)
        rows = []
        for t in tasks:
            pre = "—"
            if t["prereview_required"]:
                if t["prereviewed"]:
                    recs = ", ".join(REC_LABEL.get(r, r) for r in t["prereview_recommendations"])
                    pre = '<span class="ok">已预审：%s</span>' % esc(recs)
                else:
                    pre = '<span class="warn">待 AI 预审</span>'
            risk = ", ".join(r["rule_id"] for r in t["risk"]) or ""
            quick = ""
            if state == "open" and (t["prereviewed"] or not t["prereview_required"]):
                v = self.kb.review_view(t["task_id"])
                quick = ('<form method="post" action="/task/%s" style="margin:0"><input type="hidden" name="csrf" '
                         'value="%s"><input type="hidden" name="bound_hash" value="%s"><input type="hidden" '
                         'name="back" value="/"><button name="decision" value="approve" class="primary" '
                         'aria-label="批准 %s">批准</button></form>'
                         % (esc(t["task_id"]), esc(who["csrf"]), esc(v["bound_hash"]), esc(t["title"])))
            rows.append('<tr><td><a href="/task/%s">%s</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                        '<td>%s</td><td class="bad">%s</td><td>%s</td></tr>'
                        % (esc(t["task_id"]), esc(t["title"] or t["task_id"]), esc(t["kind"]), esc(t["type"]),
                           esc(t["submitter"]), pre, esc(t["opened_at"][:16].replace("T", " ")), esc(risk), quick))
        st = self.kb.status()
        head = ('<p class="muted">条目：%s；待审任务：%s；等待 AI 预审：%s</p>'
                % (esc(json.dumps(st["entries"], ensure_ascii=False)), esc(json.dumps(st["open_tasks"],
                                                                                     ensure_ascii=False)),
                   st["awaiting_prereview"]))
        table = ('<table><thead><tr><th scope="col">标题</th><th scope="col">任务</th><th scope="col">类型</th>'
                 '<th scope="col">投递方</th><th scope="col">预审</th><th scope="col">时间</th>'
                 '<th scope="col">高危</th><th scope="col">操作</th></tr></thead><tbody>%s</tbody></table>'
                 % ("".join(rows) or '<tr><td colspan="8" class="muted">没有任务</td></tr>'))
        title = "待审队列" if state == "open" else "已处理"
        return self.page(title, "<h1>%s</h1>%s%s" % (title, head, table), who)

    def _entry_block(self, title, r):
        if not r:
            return ""
        doc = ""
        if r.get("document"):
            files = "".join('<tr><td><a href="/orig?ref=%s&path=%s" target="_blank" rel="noopener">%s</a></td>'
                            '<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                            % (urllib.parse.quote(r["ref"]), urllib.parse.quote(f["path"]), esc(f["path"]),
                               esc(f["role"]), esc(f["size"]), "可复用" if f["reusable"] else "—",
                               esc(f["description"]))
                            for f in r["document"]["files"])
            doc = ('<h3>原文文件</h3><table><thead><tr><th scope="col">文件</th><th scope="col">角色</th>'
                   '<th scope="col">字节</th><th scope="col">复用</th><th scope="col">说明</th></tr></thead>'
                   '<tbody>%s</tbody></table>' % files)
        tags = " ".join('<span class="pill">%s/%s%s</span>' % (esc(t["facet"]), esc(t["tag"]),
                                                               "（建议）" if t["state"] == "suggested" else "")
                        for t in r.get("tags", []))
        return ('<section><h2>%s：%s <span class="muted">%s · %s · %s</span></h2><p>%s</p><pre>%s</pre>'
                '<details><summary>条件、来源、关系</summary><pre>%s</pre></details>%s</section>'
                % (esc(title), esc(r["title"]), esc(r["ref"]), esc(r["type"]), esc(r["status"]), tags,
                   pretty(r["fields"]), pretty({"conditions": r["conditions"], "sources": r["sources"],
                                                "reinforced_by": r["reinforced_by"], "relations": r["relations"]}),
                   doc))

    def task_page(self, who, task_id, msg=""):
        v = self.kb.review_view(task_id)
        parts = ['<h1>任务 %s <span class="pill">%s</span> <span class="pill">%s</span></h1>'
                 % (esc(task_id), esc(v["kind"]), esc(v["state"]))]
        if msg:
            parts.append('<p class="bad" role="alert">%s</p>' % esc(msg))
        parts.append('<p class="muted">投递方 %s · %s</p>' % (esc(v["submitter"]), esc(v["opened_at"])))
        if v.get("origin"):
            parts.append('<section><h2>来源说明（投递方给出）</h2><pre>%s</pre></section>' % pretty(v["origin"]))
        if v["risk"]:
            parts.append('<p class="bad">高危规则命中：%s</p>' % esc(", ".join(r["rule_id"] for r in v["risk"])))
        if v["exact_duplicate_of"]:
            parts.append('<p class="warn">与已有版本逐字相同：%s</p>' % esc(v["exact_duplicate_of"]))
        parts.append(self._entry_block("待审内容", v.get("pending")))
        parts.append(self._entry_block("当前版本", v.get("current")))
        if v.get("payload"):
            parts.append('<section><h2>提议内容</h2><pre>%s</pre></section>' % pretty(v["payload"]))
        if v.get("landing_scope"):
            rows = "".join("<tr><td>%s/%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % (esc(x["facet"]), esc(x["tag"]), esc(x["tag_status"]), x["existing_entries"],
                              x["existing_chars"]) for x in v["landing_scope"])
            parts.append('<section><h2>落入范围</h2><table><thead><tr><th scope="col">标签</th><th scope="col">'
                         '标签状态</th><th scope="col">已有条目</th><th scope="col">已有字数</th></tr></thead>'
                         '<tbody>%s</tbody></table></section>' % rows)
        if v.get("checklist"):
            parts.append('<section><h2>审核清单</h2><ul>%s</ul></section>'
                         % "".join("<li>%s</li>" % esc(c) for c in v["checklist"]))
        pres = v["prereviews"]
        if pres:
            for p in pres:
                parts.append('<section><h2>AI 预审 %s <span class="muted">%s%s</span></h2><pre>%s</pre></section>'
                             % (esc(p["reviewer"]), esc(p["at"]), "" if p["current"] else "（针对旧内容）",
                                pretty(p["opinion"])))
        elif v["prereview_required"]:
            parts.append('<p class="warn">尚无 AI 预审，暂不能批准。</p>')
        if v["decisions"]:
            parts.append('<section><h2>已有决定</h2><pre>%s</pre></section>' % pretty(v["decisions"]))
        if v["state"] == "open":
            parts.append(self._decision_form(who, v))
        return self.page("任务 " + task_id, "".join(parts), who)

    def _decision_form(self, who, v):
        content = v["kind"] in ("new", "revise")
        recheck = v["kind"] == "status_change" and (v.get("payload") or {}).get("recheck")
        corr = ""
        if content:
            pend = v["pending"]
            seed = {"title": pend["title"], "fields": pend["fields"]}
            corr = ('<details><summary>修正后批准（改标题/字段/条件/来源/标签）</summary>'
                    '<label for="corr">修正内容（JSON，仅写需要改的键：title、fields、conditions、sources、files、tags）'
                    '</label><textarea id="corr" name="correction">%s</textarea>'
                    '<p><button name="decision" value="correct" class="primary">修正并批准</button></p></details>'
                    % esc(json.dumps(seed, ensure_ascii=False, indent=2)))
        extra = ""
        if content:
            extra = ('<details><summary>标签决定与关系（可选）</summary>'
                     '<label for="rej">驳回的建议标签（每行一个 facet/tag）</label><textarea id="rej" name="reject_tags">'
                     '</textarea><label for="rel">采纳的关系（JSON 列表：[{"rel":"supersedes","entry_id":"e…",'
                     '"direction":"out","retire_other":true,"note":"…"}]）</label>'
                     '<textarea id="rel" name="relations"></textarea>'
                     '<label for="rtgt">并入已有条目（reinforce_into 时填写目标 entry_id）</label>'
                     '<input id="rtgt" type="text" name="target_entry_id"></details>')
        if recheck:
            extra = ('<label for="ts">复审结论</label><select id="ts" name="target_status">'
                     '<option value="keep">保持现状</option><option value="deprecated">标为过时</option>'
                     '<option value="revoked">撤销</option></select>')
        buttons = ['<button name="decision" value="approve" class="primary">批准</button>']
        if content:
            buttons.append('<button name="decision" value="request_changes">退回修改</button>')
            if v["kind"] == "new":
                buttons.append('<button name="decision" value="reinforce_into">并入已有条目</button>')
        buttons.append('<button name="decision" value="reject" class="danger">驳回</button>')
        return ('<section><h2>决定</h2><form method="post" action="/task/%s"><input type="hidden" name="csrf" '
                'value="%s"><input type="hidden" name="bound_hash" value="%s"><label for="why">备注（可选；退回或驳回时'
                '建议写明原因，投递方能看到）</label><textarea id="why" name="rationale"></textarea>%s%s<p>%s</p>'
                '</form></section>'
                % (esc(v["task_id"]), esc(who["csrf"]), esc(v["bound_hash"]), extra, corr, "".join(buttons)))

    def lint_page(self, who):
        r = self.kb.lint()
        a = self.kb.verify_audit()
        return self.page("巡检", '<h1>巡检</h1><p>审计链：%s（%d 条）</p><pre>%s</pre>'
                         % ('<span class="ok">完整</span>' if a["ok"] else '<span class="bad">断裂</span>',
                            a["count"], pretty(r)), who)


def _parse_decision(form):
    args = {}
    rej = [x.strip() for x in form.get("reject_tags", "").splitlines() if x.strip()]
    if rej:
        args["tag_decisions"] = {"reject": rej}
    rel = form.get("relations", "").strip()
    if rel:
        args["relations"] = json.loads(rel)
    if form.get("decision") == "correct":
        args["correction"] = json.loads(form.get("correction") or "{}")
    if form.get("target_entry_id", "").strip():
        args["target_entry_id"] = form["target_entry_id"].strip()
    if form.get("target_status"):
        args["target_status"] = form["target_status"]
    return args


def make_handler(console):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "ukb-review"

        def log_message(self, fmt, *a):
            pass

        def _session(self):
            c = self.headers.get("Cookie", "")
            for part in c.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ukb_s":
                    with console.lock:
                        s = console.sessions.get(v)
                        if s and s["exp"] > time.time():
                            return v, s
                        console.sessions.pop(v, None)
            return None, None

        def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                                        "form-action 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, loc, headers=()):
            self.send_response(303)
            self.send_header("Location", loc)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5 * 1024 * 1024:
                raise UKBError("PAYLOAD_INVALID", "form too large")
            raw = self.rfile.read(n).decode("utf-8")
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        def do_GET(self):
            with console.db_lock:
                return self._get()

        def do_POST(self):
            with console.db_lock:
                return self._post()

        def _get(self):
            u = urllib.parse.urlparse(self.path)
            sid, s = self._session()
            if s is None:
                return self._send(200, console.login_page())
            try:
                if u.path == "/":
                    return self._send(200, console.queue_page(s))
                if u.path == "/closed":
                    out = console.queue_page(s, "approved")
                    return self._send(200, out)
                if u.path == "/lint":
                    return self._send(200, console.lint_page(s))
                if u.path.startswith("/task/"):
                    return self._send(200, console.task_page(s, u.path[6:]))
                if u.path == "/orig":
                    q = urllib.parse.parse_qs(u.query)
                    r = console.kb.read_original("reviewer", q["ref"][0], q["path"][0], cursor=q.get("cursor", [None])[0])
                    body = "<h1>%s</h1><p class='muted'>%s</p><pre>%s</pre>" % (
                        esc(r["path"]), esc(r.get("selection", "")), esc(r.get("text") or r.get("note")))
                    if r.get("next_cursor"):
                        body += "<p><a href='/orig?ref=%s&path=%s&cursor=%s'>下一页</a></p>" % (
                            urllib.parse.quote(q["ref"][0]), urllib.parse.quote(q["path"][0]),
                            urllib.parse.quote(r["next_cursor"]))
                    return self._send(200, console.page(r["path"], body, s))
                return self._send(404, console.page("未找到", "<p>未找到</p>", s))
            except UKBError as e:
                return self._send(400, console.page("错误", "<p class='bad'>%s</p><pre>%s</pre>" % (
                    esc(e.message), pretty(e.to_dict())), s))

        def _post(self):
            u = urllib.parse.urlparse(self.path)
            try:
                form = self._form()
            except UKBError as e:
                return self._send(400, e.message, "text/plain; charset=utf-8")
            if u.path == "/login":
                try:
                    who = console.kb.auth(form.get("reviewer_id", ""), form.get("token", ""))
                except UKBError:
                    time.sleep(0.5)
                    return self._send(401, console.login_page("登录失败"))
                sid = secrets.token_urlsafe(24)
                with console.lock:
                    console.sessions[sid] = dict(who, token=form["token"], csrf=secrets.token_urlsafe(16),
                                                 exp=time.time() + SESSION_TTL)
                return self._redirect("/", [("Set-Cookie", "ukb_s=%s; HttpOnly; SameSite=Strict; Path=/" % sid)])
            sid, s = self._session()
            if s is None:
                return self._send(401, console.login_page("请先登录"))
            if not secrets.compare_digest(form.get("csrf", ""), s["csrf"]):
                return self._send(403, console.page("拒绝", "<p class='bad'>表单已失效，请刷新后重试</p>", s))
            if u.path == "/logout":
                with console.lock:
                    console.sessions.pop(sid, None)
                return self._redirect("/", [("Set-Cookie", "ukb_s=; Max-Age=0; Path=/")])
            if u.path.startswith("/task/"):
                task_id = u.path[6:]
                try:
                    args = _parse_decision(form)
                    console.kb.decide(task_id, s["reviewer_id"], s["token"], form.get("decision"),
                                      form.get("rationale", ""), form.get("bound_hash"), "webui", args)
                except (UKBError, ValueError) as e:
                    msg = e.message if isinstance(e, UKBError) else "JSON 格式错误：%s" % e
                    if isinstance(e, UKBError) and e.detail:
                        msg += " " + json.dumps(e.detail, ensure_ascii=False)
                    return self._send(400, console.task_page(s, task_id, msg))
                back = form.get("back")
                return self._redirect(back if back == "/" else "/task/" + task_id)
            return self._send(404, "not found", "text/plain")
    return H


def serve(root, host="127.0.0.1", port=8765):
    console = Console(UKB(root))
    httpd = http.server.ThreadingHTTPServer((host, port), make_handler(console))
    return httpd, console
