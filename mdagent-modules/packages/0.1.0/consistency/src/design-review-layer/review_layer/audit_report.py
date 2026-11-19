"""Generate a gap-audit HTML for the current comparison content (no policy rules)."""
import json, html
from pathlib import Path

def esc(x): return html.escape(str(x or ""))


def main():
    rows = []
    cases = {}
    for i in range(1, 6):
        cid = "case%d" % i
        plan = json.loads(Path("reports/%s_review.json" % cid).read_text(encoding="utf-8"))
        cases[cid] = plan
        # comparison rows live in server build_plan; re-use it
        import sys
        sys.path.insert(0, "web")
        import design_plan_server as srv
        p = srv.build_plan(cid)
        for c in p["comparisons"]:
            dim = c["dim"]
            if dim == "加载":
                level = "跨尺度论证（文字）"
                evidence = "无 E1–E6 引用"
                missing = "应变率敏感性文献、惯性/温升估计、适用边界"
            elif dim in ("目的与判据", "材料与势", "环境"):
                level = "字段比对"
                evidence = "无"
                missing = {
                    "目的与判据": "判据可测性、解释角色、误差传递",
                    "材料与势": "势函数适用性、成分/组织保真",
                    "环境": "散热路径、润滑/气氛、温度均匀性",
                }[dim]
            else:
                level = "占位（对应关系已建立）"
                evidence = "无"
                missing = {"装置与几何": "尺度分离、边界等效、几何相似",
                           "观测": "观测量定义、统计口径、不确定度"}[dim]
            rows.append({"case": cid, "dim": dim, "conclusion": c["diff"], "verdict": c["verdict"],
                         "level": level, "evidence": evidence, "missing": missing})

    dims = [
        ("目标与解释角色", "✗ 缺失", "只有字段对应，无解释角色/结论层级审查"),
        ("材料模型保真", "✗ 缺失", "势函数与成分/组织仅字段比对"),
        ("抽象映射保真（几何/尺度）", "✗ 缺失", "尺寸差、边界等效未量化"),
        ("物理可表达性（时间/空间）", "△ 部分", "仅加载项有跨尺度文字，无证据"),
        ("设定自洽（系综/控温/加载）", "△ 部分", "系综/时间步/步数字段比对"),
        ("数值与有限尺度", "△ 部分", "仅 timestep/步数"),
        ("观测与判据", "✗ 缺失", "观测量定义与统计口径未审"),
        ("不确定度与对照协议", "✗ 缺失", "无不确定度/误差传递"),
        ("证据完备性（E1–E6）", "✗ 缺失", "所有结论无证据引用"),
    ]


    def render():
        parts=['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>物理一致性校核缺口审计</title><style>',
        'body{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#eef2f6;margin:0;padding:24px;color:#1c2530}',
        '.card{max-width:1200px;margin:0 auto;background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(23,43,77,.10);padding:22px}',
        'h1{font-size:20px;margin:0 0 4px}.sub{font-size:12px;color:#7b8896;margin-bottom:12px}',
        'h2{font-size:14px;margin:16px 0 8px}table{width:100%;border-collapse:collapse;font-size:11px}',
        'th{text-align:left;color:#8a97a5;font-weight:600;padding:5px 6px;border-bottom:1px solid #e6ebf0}',
        'td{padding:5px 6px;border-bottom:1px solid #f1f4f7;vertical-align:top;line-height:1.5}',
        '.v{padding:1px 6px;border-radius:5px;font-weight:700;font-size:9.5px}.ok{background:#e7f6ec;color:#15803d}.q{background:#fdf3e2;color:#b45309}.bad{background:#fdeaea;color:#b91c1c}',
        '.cover{display:flex;gap:10px;margin:8px 0 12px}.box{flex:1;border:1px solid #e6ebf0;border-radius:11px;padding:10px 12px;background:#fbfcfe}',
        '.box .n{font-size:22px;font-weight:800}.box .l{font-size:11px;color:#7b8896}.warn{color:#b45309}',
        '</style></head><body><div class="card">',
        '<h1>物理一致性校核缺口审计</h1>',
        '<div class="sub">对象：五个测试案例的 30 条对照 · 生成时间 2026-10-30 · 零规则层（仅统计与缺口分析）</div>',
        '<div class="cover"><div class="box"><div class="n">30</div><div class="l">对照条目</div></div><div class="box"><div class="n">0</div><div class="l">有 E1–E6 证据</div></div><div class="box"><div class="n warn">1/6</div><div class="l">有跨尺度论证（仅文字）</div></div><div class="box"><div class="n warn">7/9</div><div class="l">物理审核维度缺失</div></div></div>',
        '<h2>一、逐条对照的实际校核等级</h2>',
        '<table><tr><th>案例</th><th>维度</th><th>当前结论</th><th>实际等级</th><th>证据</th><th>缺失的物理审查</th></tr>']
        for r in rows:
            cls = "ok" if r["verdict"]=="ok" else "q"
            parts.append("<tr><td>%s</td><td>%s</td><td><span class='v %s'>%s</span><br>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                r["case"], r["dim"], cls, r["verdict"], esc(r["conclusion"][:60]), r["level"], r["evidence"], r["missing"]))
        parts.append('</table><h2>二、物理一致性审核维度覆盖</h2><table><tr><th>审核维度</th><th>覆盖</th><th>说明</th></tr>')
        for d, st, note in dims:
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (d, st, note))
        parts.append('</table><h2>三、结论</h2><p style="font-size:12px;line-height:1.7">当前实现实际是「<b>方案字段 vs in 文件实现</b>」的一致性核对：系综/时间步/步数/units/pair_style 可机器验证，加载项有一段跨尺度文字但无证据；其余维度是"字段对应"或占位。它<b>不构成仿真与实验的物理一致性校核</b>。缺的正是独立的<b>文件审核层</b>：对实验方案与仿真方案两份文档逐维度审查物理假设、尺度、边界、观测量与不确定度，并要求每条结论挂 E1–E6 证据。</p>')
        parts.append('</div></body></html>')
        return "\n".join(parts)


    Path("reports/physics_consistency_audit.html").write_text(render(), encoding="utf-8")
    print("audit written")


if __name__ == "__main__":
    main()
