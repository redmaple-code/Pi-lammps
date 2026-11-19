"""Aggregate discovery points + coverage into a single HTML page (common issues collapsed)."""
import json
from pathlib import Path
from review_layer import common_issues

BASE = Path(__file__).resolve().parents[1]
D = BASE / "reports" / "discovery"


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    rows = []
    for i in range(1, 6):
        cid = "case%d" % i
        p = D / ("%s_points.json" % cid)
        c = D / ("%s_coverage.json" % cid)
        if not p.exists() or not c.exists():
            continue
        pts = json.loads(p.read_text(encoding="utf-8"))
        counts = common_issues.annotate(pts["points"])
        agg = common_issues.aggregate(pts["points"])
        cov = json.loads(c.read_text(encoding="utf-8"))["coverage"]
        rows.append((cid, pts, cov, counts, agg))

    html = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>AI 审查点发现 · 试点结果</title><style>',
            'body{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#eef2f6;margin:0;padding:24px;color:#1c2530}',
            '.card{max-width:1200px;margin:0 auto;background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(23,43,77,.10);padding:22px}',
            'h1{font-size:20px;margin:0 0 4px}.sub{font-size:12px;color:#7b8896;margin-bottom:12px}',
            'h2{font-size:14px;margin:14px 0 6px}table{width:100%;border-collapse:collapse;font-size:11px}',
            'th{text-align:left;color:#8a97a5;font-weight:600;padding:5px 6px;border-bottom:1px solid #e6ebf0}',
            'td{padding:5px 6px;border-bottom:1px solid #f1f4f7;vertical-align:top;line-height:1.45}',
            '.ok{color:#15803d;font-weight:700}.warn{color:#b45309;font-weight:700}.muted{color:#8a97a5}',
            'details{margin:8px 0;border:1px solid #eef2f6;border-radius:8px;padding:6px 10px;background:#fbfcfe}',
            'summary{cursor:pointer;font-size:11.5px;font-weight:700;color:#52606d}',
            '.note{font-size:11px;color:#8a97a5;margin-top:10px}',
            '</style></head><body><div class="card">',
            '<h1>AI 审查点发现 · 试点结果</h1>',
            '<div class="sub">发现层只产候选，不做裁决 · 9 维基线仅作参考映射 · 常见问题/已知局限默认折叠 · 模型来源见各案例</div>',
            '<table><tr><th>案例</th><th>审查点</th><th>待回答</th><th>需记录</th><th>已解释（折叠）</th><th>饱和状态</th><th>模型来源</th></tr>']

    for cid, pts, cov, counts, agg in rows:
        sat = cov.get("saturation", {})
        html.append("<tr><td>%s</td><td>%d</td><td class='warn'>%d</td><td>%d</td><td class='muted'>%d</td><td>%s（%d→%d，%.0f%%）</td><td>%s</td></tr>" % (
            cid, len(pts["points"]), counts.get("open", 0), counts.get("record_required", 0),
            counts.get("explained", 0),
            "未饱和" if sat.get("status") == "not_saturated" else ("已饱和" if sat.get("status") == "saturated" else "首轮"),
            sat.get("previous", 0), sat.get("new", 0), sat.get("new_rate", 0) * 100, esc(pts.get("provider", ""))))

    for cid, pts, cov, counts, agg in rows:
        html.append("<h2>%s（共 %d 条：待回答 %d / 需记录 %d / 已解释 %d）</h2>" % (
            cid, len(pts["points"]), counts.get("open", 0), counts.get("record_required", 0), counts.get("explained", 0)))
        groups = [("open", "待回答", "warn"), ("record_required", "需记录", "muted"), ("explained", "已解释·常见问题/已知局限", "muted")]
        for disp, label, cls in groups:
            group = [p for p in pts["points"] if p.get("disposition") == disp] if disp == "open" else [g for g in agg["common_groups"] if g["disposition"] == disp]
            if not group:
                continue
            if disp == "open":
                html.append("<table><tr><th style='width:40px'>#</th><th>审查点</th><th>维度</th><th>位置</th><th>物理问题</th><th>严重度</th></tr>")
                for p in group:
                    loc = "；".join(filter(None, [
                        ("实验 " + ",".join(p["locations"].get("experiment", []))) if p["locations"].get("experiment") else "",
                        ("in " + ",".join("L%d" % x for x in p["locations"].get("infile", []))) if p["locations"].get("infile") else ""]))
                    html.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        esc(p["point_id"]), esc(p["title"]), esc(p["dimension_guess"]), esc(loc),
                        esc(p["physical_question"]), esc(p["severity_hint"])))
                html.append("</table>")
            else:
                groups = [g for g in agg["common_groups"] if g["disposition"] == disp]
                html.append("<details><summary>%s：%d 类 / %d 次命中（点击展开；内置解释，不占待回答清单）</summary><table><tr><th style='width:80px'>ID</th><th>常见问题</th><th style='width:60px'>命中</th><th>示例条目</th></tr>" % (
                    label, len(groups), sum(g["occurrences"] for g in groups)))
                for g in groups:
                    html.append("<tr><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>" % (
                        esc(g["common_issue_id"]), esc(g["name"]), g["occurrences"],
                        esc("、".join(g["point_ids"][:6]))))
                html.append("</table></details>")

    html.append('<div class="note">口径：9 维基线仅参考映射，不强制每案覆盖；完整性看饱和（连续两轮新增≤5%）。常见问题库版本见 common_issues/registry.json（v1.0，12 条）；新增条目需所有者批准。</div>')
    html.append('</div></body></html>')
    (BASE / "reports" / "discovery_summary.html").write_text("\n".join(html), encoding="utf-8")
    print("discovery_summary.html written")


if __name__ == "__main__":
    main()
