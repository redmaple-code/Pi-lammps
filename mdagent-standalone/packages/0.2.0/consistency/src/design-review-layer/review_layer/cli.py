"""CLI for the design-review layer.

Usage:
  python -m review_layer.cli --case rt_a3_al_npt --mdagent-root <path> --out reports
  python -m review_layer.cli --intent ... --infile ... --gate ... --fixture --out reports
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from .consistency import build_consistency
from .models import Artifact, ReviewReport
from .review import summarize

CASES = {
    "rt_a3_al_npt": {
        "intent": "case_runs/rt_a3/proj-root/cases/rt_a3_al_npt_20261022_013637/intent.json",
        "infile": "case_runs/rt/a3_npt/in.case",
        "gate": "case_runs/rt/rt_a3_validate.json",
    },
    "rt_a1_ni_lattice": {
        "intent": "case_runs/rt_a1/proj-root/cases/rt_a1_ni_lattice_20261022_011810/intent.json",
        "infile": "case_runs/rt/a1_scan/in.case",
        "gate": "case_runs/rt/rt_a1_validate.json",
    },
}

VERDICT_LABEL = {"ok": "合理", "questionable": "存疑", "unproven": "未论证",
                 "invalid": "不成立", "plan_conflict": "方案内部矛盾·待修订"}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _artifact(kind: str, p: Path, source: str) -> Artifact:
    return Artifact(kind=kind, path=str(p), sha256=_sha(p) if p.exists() else "", source=source)


def build_report(intent_path: Path, infile_path: Path, gate_path: Path | None,
                 source: str, cfg: dict) -> ReviewReport:
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    text = infile_path.read_text(encoding="utf-8")
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path and gate_path.exists() else {}
    artifacts = [_artifact("intent", intent_path, source),
                 _artifact("infile", infile_path, source)]
    if gate_path and gate_path.exists():
        artifacts.append(_artifact("gate", gate_path, source))
    report = ReviewReport(
        case_id=intent.get("case_id") or infile_path.stem,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        artifacts=artifacts,
        consistency=build_consistency(intent, text),
        experiment_plan=(intent.get("experiment_plan") or {}).get("sections") or [],
        findings=[],
        gate_status=str(gate.get("status") or "UNKNOWN"),
        verdict="PENDING",
    )
    return summarize(report)


def render_md(r: ReviewReport) -> str:
    lines = ["# 一致性校核审核报告", "",
             "- 案例：`%s`" % r.case_id,
             "- 生成时间：%s" % r.generated_at,
             "- 审核结论：**%s**" % r.verdict,
             "- 既有门状态：%s" % r.gate_status, "",
             "## 工件", ""]
    for a in r.artifacts:
        lines.append("- %s: `%s` sha256=%s source=%s" % (a.kind, a.path, a.sha256 or "missing", a.source))
    if r.experiment_plan:
        lines += ["", "## 宏观实验（AI 补写，待确认）", ""]
        for sec in r.experiment_plan:
            lines.append("- **%s %s**：%s" % (sec.get("id"), sec.get("title"), sec.get("text")))
    lines += ["", "## 一致性条目", "", "| ID | 要素 | 判定 | 说明 |", "|---|---|---|---|"]
    for i in r.consistency:
        lines.append("| %s | %s | %s | %s |" % (i.id, i.element, VERDICT_LABEL.get(i.verdict, i.verdict), i.detail.replace("|", "/")))
    lines += ["", "## 审核发现", "", "| ID | 规则 | 严重度 | 标题 | 说明 |", "|---|---|---|---|---|"]
    for f in r.findings:
        lines.append("| %s | %s | %s | %s | %s |" % (f.id, f.rule, f.severity, f.title, f.detail.replace("|", "/")))
    return "\n".join(lines) + "\n"


def render_html(r: ReviewReport) -> str:
    def cls(v):
        return {"ok": "ok", "questionable": "q", "unproven": "q", "invalid": "bad",
                "plan_conflict": "pc"}.get(v, "q")
    rows = "".join(
        "<tr class='%s'><td>%s</td><td>%s</td><td><span class='v %s'>%s</span></td><td>%s</td></tr>"
        % (cls(i.verdict), i.id, i.element, cls(i.verdict), VERDICT_LABEL.get(i.verdict, i.verdict), i.detail)
        for i in r.consistency)
    finds = ("<tr><td colspan='5'>未配置任何审核规则（按所有者指示）；本报告仅汇总一致性条目。</td></tr>"
             if not r.findings else "".join(
        "<tr><td>%s</td><td>%s</td><td class='sev %s'>%s</td><td>%s</td><td>%s</td></tr>"
        % (f.id, f.rule, f.severity, f.severity, f.title, f.detail)
        for f in r.findings))
    arts = "".join("<li><b>%s</b> <code>%s</code> sha256=%s <i>%s</i></li>" % (a.kind, a.path, a.sha256 or "—", a.source) for a in r.artifacts)
    exphtml = "".join("<li><b>%s %s</b>：%s</li>" % (sec.get("id"), sec.get("title"), sec.get("text")) for sec in r.experiment_plan)
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>审核报告 · %s</title><style>
body{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#eef2f6;margin:0;padding:24px;color:#1c2530}
.card{max-width:1100px;margin:0 auto;background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(23,43,77,.12);padding:22px}
h1{font-size:20px;margin:0 0 6px} .meta{font-size:12px;color:#7b8896;margin-bottom:12px}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700}
.badge.PASS{background:#e7f6ec;color:#15803d}.badge.REVIEW{background:#fdf3e2;color:#b45309}.badge.NEEDS_REVISION{background:#e8f0fe;color:#1d4ed8}.badge.FAIL{background:#fdeaea;color:#b91c1c}
h2{font-size:14px;margin:18px 0 8px} ul{font-size:11.5px;color:#52606d;margin:0;padding-left:18px}
table{width:100%%;border-collapse:collapse;font-size:11.5px} th{text-align:left;color:#8a97a5;font-weight:600;padding:5px 7px;border-bottom:1px solid #e6ebf0}
td{padding:6px 7px;border-bottom:1px solid #f1f4f7;vertical-align:top;line-height:1.5}
.v{padding:1px 7px;border-radius:5px;font-weight:700;font-size:10.5px}.v.ok{background:#e7f6ec;color:#15803d}.v.q{background:#fdf3e2;color:#b45309}.v.bad{background:#fdeaea;color:#b91c1c}.v.pc{background:#e8f0fe;color:#1d4ed8}
tr.ok{background:#fbfffc} tr.q{background:#fffdf7} tr.bad{background:#fffafa} tr.pc{background:#f8fbff}
.sev{font-weight:700}.sev.blocker{color:#b91c1c}.sev.major{color:#b45309}.sev.minor{color:#6b7280}.sev.info{color:#2563eb}
.note{font-size:11px;color:#8a97a5;margin-top:14px}
</style></head><body><div class="card">
<h1>一致性校核审核报告</h1>
<div class="meta">案例 <b>%s</b> · %s · 既有门状态 <b>%s</b> · 审核结论 <span class="badge %s">%s</span></div>
<h2>工件（来源与哈希）</h2><ul>%s</ul>
<h2>宏观实验（AI 补写，待确认）</h2><ul>%s</ul>
<h2>一致性条目</h2><table><tr><th>ID</th><th>要素</th><th>判定</th><th>说明</th></tr>%s</table>
<h2>审核规则</h2><table><tr><th>ID</th><th>规则</th><th>严重度</th><th>标题</th><th>说明</th></tr>%s</table>
<div class="note">本报告由一致性校核嵌入层生成：只做一致性校核与汇总，不注入任何审核规则。经验源/证据源当前为 Null（接口已预留），接入后由所有者批准再启用。</div>
</div></body></html>""" % (r.case_id, r.case_id, r.generated_at, r.gate_status, r.verdict, r.verdict, arts, exphtml, rows, finds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(CASES))
    ap.add_argument("--mdagent-root")
    ap.add_argument("--intent")
    ap.add_argument("--infile")
    ap.add_argument("--gate")
    ap.add_argument("--fixture", action="store_true", help="mark artifacts as fixture (demo only)")
    ap.add_argument("--source", choices=["mdagent", "external", "fixture"], default=None)
    ap.add_argument("--config", help="layer config json (sources)")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    if args.case:
        if not args.mdagent_root:
            sys.exit("--mdagent-root required with --case")
        root = Path(args.mdagent_root)
        cfg_case = CASES[args.case]
        intent = root / cfg_case["intent"]
        infile = root / cfg_case["infile"]
        gate = root / cfg_case["gate"]
        case_id = args.case
    else:
        if not (args.intent and args.infile):
            sys.exit("provide --case+--mdagent-root or --intent+--infile[+--gate]")
        intent, infile = Path(args.intent), Path(args.infile)
        gate = Path(args.gate) if args.gate else None
        case_id = intent.stem

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    source = args.source or ("fixture" if args.fixture else "mdagent")
    report = build_report(intent, infile, gate, source, cfg)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    case_id = report.case_id
    (out / ("%s_review.json" % case_id)).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / ("%s_review.md" % case_id)).write_text(render_md(report), encoding="utf-8")
    (out / ("%s_review.html" % case_id)).write_text(render_html(report), encoding="utf-8")
    print("verdict: %s | items: %d | findings: %d" % (report.verdict, len(report.consistency), len(report.findings)))
    print("report:", out / ("%s_review.html" % case_id))
    sys.exit(0 if report.verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
