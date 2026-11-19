"""Generate a full review HTML page: experiment plan + simulation plan + comparison + in-file check."""
from __future__ import annotations

import html
import json
from pathlib import Path

LABELS = {"ok": "合理", "questionable": "存疑", "unproven": "未论证", "invalid": "不成立",
          "plan_conflict": "方案内部矛盾·待修订"}
VCLS = {"ok": "ok", "questionable": "q", "unproven": "q", "invalid": "bad", "plan_conflict": "pc"}
VERDICT_CLS = {"PASS": "PASS", "REVIEW": "REVIEW", "NEEDS_REVISION": "NEEDS_REVISION", "FAIL": "FAIL"}


def esc(x):
    return html.escape(str(x if x is not None else ""))


def parse_infile(text: str):
    lines = text.splitlines()
    cmds = []
    for i, raw in enumerate(lines, 1):
        t = raw.split("#", 1)[0].strip().split()
        if t:
            cmds.append((i, t))
    info = {"lines": lines, "units": None, "boundary": None, "lattice": None, "regions": [],
            "pair_style": None, "pair_coeff": None, "fixes": [], "timestep": None, "runs": [],
            "computes": [], "dumps": [], "thermo": [], "reads": [], "velocity": []}
    for ln, t in cmds:
        k = t[0]
        if k == "units": info["units"] = " ".join(t[1:])
        elif k == "boundary": info["boundary"] = " ".join(t[1:])
        elif k == "lattice": info["lattice"] = " ".join(t[1:])
        elif k == "region": info["regions"].append((ln, " ".join(t[1:])))
        elif k == "pair_style": info["pair_style"] = " ".join(t[1:])
        elif k == "pair_coeff": info["pair_coeff"] = " ".join(t[1:])
        elif k == "fix": info["fixes"].append((ln, t))
        elif k == "timestep": info["timestep"] = t[1] if len(t) > 1 else None
        elif k == "run": info["runs"].append((ln, t[1] if len(t) > 1 else "?"))
        elif k == "compute": info["computes"].append((ln, " ".join(t[1:])))
        elif k == "dump": info["dumps"].append((ln, " ".join(t[1:])))
        elif k in ("thermo", "thermo_style"): info["thermo"].append((ln, " ".join(t[1:])))
        elif k in ("read_data", "read_restart"): info["reads"].append((ln, " ".join(t[1:])))
        elif k == "velocity": info["velocity"].append((ln, " ".join(t[1:])))
    return info


def sim_sections(intent, info):
    fixes = ["fix %s %s %s %s" % (t[1], t[2], t[3], " ".join(t[4:])) for _, t in info["fixes"]]
    ens = [f for f in fixes if any(x in f.split()[3] for x in ("nvt", "npt", "nve", "nph"))]
    load = [f for f in fixes if f.split()[3] in ("deform", "move", "indent")]
    secs = []
    secs.append({"id": "S-01", "title": "目标与对应量",
                 "text": (intent.get("objective") or {}).get("simulation_goal") or "（未声明）",
                 "why": "目标取自 intent.objective。"})
    secs.append({"id": "S-02", "title": "模型与势函数",
                 "text": "pair_style = %s；pair_coeff = %s；units = %s。" % (
                     info["pair_style"] or "（未设置）", info["pair_coeff"] or "（未设置）", info["units"] or "（未设置）"),
                 "why": "依据 in 文件实际设置；选型理由未在方案中声明（需补充）。"})
    regions = "；".join("%s（L%d）" % (r, ln) for ln, r in info["regions"][:4]) or "（无 region，使用 read_data）"
    secs.append({"id": "S-03", "title": "几何与边界",
                 "text": "boundary = %s；lattice = %s；%s；%s。" % (
                     info["boundary"] or "（未设置）", info["lattice"] or "（未设置）", regions,
                     "；".join("%s（L%d）" % (r, ln) for ln, r in info["reads"]) or ""),
                 "why": "边界/几何取自 in 文件；宏观尺度对应关系需方案声明。"})
    secs.append({"id": "S-04", "title": "系综与控温",
                 "text": "；".join(ens) if ens else "（未找到 nvt/npt/nve 积分 fix）",
                 "why": "系综由积分 fix 推断；选择理由需方案补充。"})
    secs.append({"id": "S-05", "title": "加载实现",
                 "text": "；".join(load) if load else ("；".join(v for _, v in info["velocity"]) or "（未找到 deform/move/indent）"),
                 "why": "加载方式取自 fix deform/move/indent 与 velocity；速率换算与实验对应需声明。"})
    secs.append({"id": "S-06", "title": "时间步与尺度",
                 "text": "timestep = %s ps；run 语句 %s。" % (
                     info["timestep"] or "（未设置）",
                     "、".join("%s 步（L%d）" % (v, ln) for ln, v in info["runs"]) or "（无）"),
                 "why": "时间步/步数取自 in 文件；与实验时间尺度的差异需在对照协议中声明。"})
    secs.append({"id": "S-07", "title": "统计与采样",
                 "text": "compute：%s；dump：%s；thermo：%s。" % (
                     "；".join(c for _, c in info["computes"]) or "（无）",
                     "；".join(d for _, d in info["dumps"]) or "（无）",
                     "；".join(d for _, d in info["thermo"]) or "（无）"),
                 "why": "输出来自 in 文件；与实验观测量的对应需方案声明。"})
    secs.append({"id": "S-08", "title": "简化假设",
                 "text": "（in 文件未显式声明简化假设；需方案补充。）", "why": "—"})
    secs.append({"id": "S-09", "title": "对照协议",
                 "text": "（in 文件未声明对照协议；需方案补充。）", "why": "—"})
    return secs


def build():
    cases = []
    for i in range(1, 6):
        cid = "case%d" % i
        intent = json.loads(Path("cases/%s/intent.json" % cid).read_text(encoding="utf-8"))
        text = Path("cases/%s/in.case" % cid).read_text(encoding="utf-8")
        report = json.loads(Path("reports/%s_review.json" % cid).read_text(encoding="utf-8"))
        info = parse_infile(text)
        cases.append({"id": cid, "intent": intent, "report": report, "info": info,
                      "sim": sim_sections(intent, info)})
    return cases


CSS = """
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#e9eef3;margin:0;padding:18px;color:#1c2530}
.wrap{max-width:1320px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}.sub{font-size:12px;color:#7b8896;margin-bottom:12px}
.tabs{display:flex;gap:6px;margin-bottom:12px}.tab{padding:6px 12px;border-radius:999px;background:#fff;font-size:12px;cursor:pointer;border:1px solid #e6ebf0}
.tab.on{background:#e8f0fe;border-color:#c9dcfb;color:#2563eb;font-weight:700}
.badge{padding:2px 10px;border-radius:999px;font-weight:700;font-size:11px}
.PASS{background:#e7f6ec;color:#15803d}.REVIEW{background:#fdf3e2;color:#b45309}.NEEDS_REVISION{background:#e8f0fe;color:#1d4ed8}.FAIL{background:#fdeaea;color:#b91c1c}
.case{display:none}.case.on{display:block}
.card{background:#fff;border-radius:14px;box-shadow:0 10px 30px rgba(23,43,77,.10);padding:16px 18px;margin-bottom:14px}
.head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.head h2{font-size:16px;margin:0}.goal{font-size:12px;color:#52606d;margin:4px 0 6px}
.meta{font-size:10.5px;color:#8a97a5;line-height:1.5;margin-bottom:6px}
h3{font-size:13px;margin:8px 0 6px;color:#52606d}
.docs{display:flex;gap:10px}.col{flex:1;border:1px solid #eef2f6;border-radius:10px;background:#fdfefe;padding:10px 12px;min-width:0}
.sec{margin-bottom:6px}.sec .h{font-size:10.5px;font-weight:700;color:#52606d}.sec .h .pid{color:#2563eb;font-family:ui-monospace,Consolas,monospace;margin-right:4px}
.sec .p{font-size:9.8px;line-height:1.55;color:#33414f;margin-top:1px;text-align:justify}
details.why{margin-top:3px;background:#f8fbff;border:1px solid #e8f0fe;border-radius:6px;padding:3px 7px}
details.why summary{font-size:9.3px;font-weight:700;color:#1d4ed8;cursor:pointer;list-style:none}details.why summary::-webkit-details-marker{display:none}
details.why summary:before{content:"▸ "}details.why[open] summary:before{content:"▾ "}
.why .q{font-size:9.2px;color:#33414f;line-height:1.5;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:10.4px}th{text-align:left;color:#8a97a5;font-weight:600;padding:4px 6px;border-bottom:1px solid #e6ebf0}
td{padding:5px 6px;border-bottom:1px solid #f1f4f7;vertical-align:top;line-height:1.5}
.v{padding:1px 6px;border-radius:5px;font-weight:700;font-size:9.6px}.v.ok{background:#e7f6ec;color:#15803d}.v.q{background:#fdf3e2;color:#b45309}.v.bad{background:#fdeaea;color:#b91c1c}.v.pc{background:#e8f0fe;color:#1d4ed8}
.chkgrid{display:flex;gap:10px}.filepane{width:48%;border:1px solid #eef2f6;border-radius:10px;background:#fbfcfd}
.fh{display:flex;align-items:center;gap:8px;padding:5px 9px;border-bottom:1px solid #eef2f6;font-size:10.5px;font-weight:700}
.code{font-family:ui-monospace,Consolas,monospace;font-size:9.2px;line-height:17px;padding:4px 0;max-height:420px;overflow:auto}
.ln{display:flex;white-space:pre}.ln .no{width:32px;text-align:right;padding-right:8px;color:#a5b1bd;flex:none}
.ln.sel{background:#e8f0fe}.ln.bad{background:#fde8e6}
.chklist{flex:1;display:flex;flex-direction:column;gap:4px}
.checkrow{display:flex;align-items:center;gap:8px;border:1px solid #eef2f6;border-radius:7px;padding:6px 8px;background:#fff;cursor:pointer}
.checkrow.selrow{border-color:#c9dcfb;background:#f5f9ff}
.checkrow .b{flex:1}.checkrow .t{font-size:10.5px;font-weight:600}.checkrow .s{font-size:9.5px;color:#7b8896}
.note{font-size:10px;color:#8a97a5;margin-top:8px}
"""


def render(cases):
    parts = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>五案例完整审查</title>',
             '<style>%s</style></head><body><div class="wrap">' % CSS,
             '<h1>五案例完整审查</h1>',
             '<div class="sub">每个任务：实验方案全文 ｜ 仿真方案全文 ｜ 两案对照 ｜ in 文件查验（零规则，仅汇总一致性条目；宏观实验为 AI 补写，待确认）</div>',
             '<div class="tabs">']
    for c in cases:
        parts.append("<span class='tab' data-case='%s'>%s</span>" % (c["id"], c["id"]))
    parts.append("</div>")

    for c in cases:
        d, intent = c["report"], c["intent"]
        parts.append("<div class='case' id='%s'><div class='card'>" % c["id"])
        parts.append("<div class='head'><h2>%s</h2><span class='badge %s'>%s</span></div>" % (
            c["id"], VERDICT_CLS.get(d["verdict"], "REVIEW"), d["verdict"]))
        parts.append("<div class='goal'>%s</div>" % esc(intent["objective"]["simulation_goal"]))
        arts = "；".join("%s <code>%s</code> sha256=%s (%s)" % (
            a["kind"], a["path"], a["sha256"] or "—", a["source"]) for a in d["artifacts"])
        parts.append("<div class='meta'>工件：%s ｜ 门状态：%s</div>" % (arts, esc(d["gate_status"])))
        parts.append("<h3>① 方案全文（左：实验 / 右：仿真）</h3><div class='docs'><div class='col'>")
        for s in d["experiment_plan"]:
            parts.append("<div class='sec'><div class='h'><span class='pid'>%s</span>%s</div><div class='p'>%s</div></div>" % (
                s["id"], s["title"], esc(s["text"])))
        parts.append("</div><div class='col'>")
        for s in c["sim"]:
            parts.append("<div class='sec'><div class='h'><span class='pid'>%s</span>%s</div><div class='p'>%s</div>"
                         "<details class='why' open><summary>设置依据</summary><div class='q'>%s</div></details></div>" % (
                             s["id"], s["title"], esc(s["text"]), esc(s["why"])))
        parts.append("</div></div>")

        pairs = [("目的与判据", "E-01/E-05", "S-01/S-09"), ("材料与势", "E-02", "S-02"),
                 ("装置与几何", "E-03", "S-03"), ("加载", "E-04", "S-05"),
                 ("环境", "E-05", "S-04"), ("观测", "E-05", "S-07")]
        parts.append("<h3>② 两案对照</h3><table><tr><th style='width:70px'>维度</th><th style='width:90px'>实验段</th><th>实验内容（摘要）</th><th style='width:90px'>仿真段</th><th>仿真内容（摘要）</th><th style='width:160px'>结论</th></tr>")
        expmap = {s["id"]: s["text"] for s in d["experiment_plan"]}
        simmap = {s["id"]: s["text"] for s in c["sim"]}
        for dim, eid, sid in pairs:
            etext = next((expmap[k] for k in eid.split("/") if k in expmap), "")
            stext = next((simmap[k] for k in sid.split("/") if k in simmap), "")
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>对应关系已建立；判定见下方一致性校核（宏观实验为 AI 补写，待确认）</td></tr>" % (
                dim, eid, esc(etext[:110]), sid, esc(stext[:110])))
        parts.append("</table>")

        parts.append("<h3>③ in 文件查验（行号高亮 + 一致性条目）</h3><div class='chkgrid'><div class='filepane'>")
        parts.append("<div class='fh'>in 文件 <span style='font-weight:400;color:#8a97a5'>%s</span></div><div class='code' id='code-%s'>" % (
            esc(Path("cases/%s/in.case" % c["id"]).name), c["id"]))
        for n, line in enumerate(c["info"]["lines"], 1):
            parts.append("<div class='ln' data-n='%d'><span class='no'>%d</span><span class='tx'>%s</span></div>" % (
                n, n, esc(line)))
        parts.append("</div></div><div class='chklist'>")
        for item in d["consistency"]:
            cls = VCLS.get(item["verdict"], "q")
            lines = item["anchors"]
            parts.append("<div class='checkrow' onclick=\"hl('%s',[%s],this)\"><div class='b'><div class='t'>%s · %s</div><div class='s'>%s</div></div><span class='v %s'>%s</span></div>" % (
                c["id"], ",".join(map(str, lines)), esc(item["element"]), esc(item["detail"][:60]),
                esc("L" + ",".join(map(str, lines)) if lines else "—"), cls, esc(LABELS.get(item["verdict"], item["verdict"]))))
        parts.append("</div></div>")
        parts.append("<div class='note'>审核规则：未配置（按所有者指示）；结论仅为一句话汇总。宏观实验为 AI 补写，未经实验记录核对。</div></div></div>")

    parts.append("</div><script>")
    parts.append("""
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  t.classList.add('on');
  document.querySelectorAll('.case').forEach(c=>c.classList.remove('on'));
  document.getElementById(t.dataset.case).classList.add('on');
}));
document.querySelectorAll('.tab')[0].classList.add('on');
document.querySelectorAll('.case')[0].classList.add('on');
function hl(caseId, lines, row){
  const pane=document.getElementById('code-'+caseId);
  pane.querySelectorAll('.ln').forEach(l=>l.classList.remove('sel'));
  lines.forEach(n=>{const el=pane.querySelector('.ln[data-n="'+n+'"]'); if(el) el.classList.add('sel');});
  row.parentNode.querySelectorAll('.checkrow').forEach(x=>x.classList.remove('selrow'));
  row.classList.add('selrow');
}
""")
    parts.append("</script></body></html>")
    return "\n".join(parts)


if __name__ == "__main__":
    cases = build()
    Path("reports/full_reviews.html").write_text(render(cases), encoding="utf-8")
    print("full_reviews.html written")
