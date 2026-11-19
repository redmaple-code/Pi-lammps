"""Design-plan reviewer server (restored 8900 page).

Serves the interactive review page and APIs from real MDagent artifacts.
Lives OUTSIDE MDagent-src: read-only access only.
"""
from __future__ import annotations

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve()
LAYER = HERE.parents[1]
sys.path.insert(0, str(LAYER))
from review_layer.consistency import build_consistency  # noqa: E402
from review_layer.full_report import parse_infile, sim_sections  # noqa: E402
from review_layer import common_issues, ukb_store  # noqa: E402
from review_layer.discovery import map_dimension  # noqa: E402
from review_layer.template_check import check as template_check  # noqa: E402

WORKSPACE = HERE.parents[2]
_SRC = WORKSPACE / "MDagent-src"
# Inside the MDagent repository the repo root is the source tree; keep the
# sibling layout used by the development workspace working as well.
MDAGENT = _SRC if _SRC.exists() else WORKSPACE
STATIC = HERE.parent / "static"

CASES = {
    "rt_a3_al_npt": {
        "title": "rt_a3_al_npt · fcc Al 300K 晶格常数与密度",
        "intent": MDAGENT / "case_runs/rt_a3/proj-root/cases/rt_a3_al_npt_20261022_013637/intent.json",
        "infile": MDAGENT / "case_runs/rt/a3_npt/in.case",
        "gate": MDAGENT / "case_runs/rt/rt_a3_validate.json",
    },
    "rt_a1_ni_lattice": {
        "title": "rt_a1_ni_lattice · Ni 0K 晶格静态扫描",
        "intent": MDAGENT / "case_runs/rt_a1/proj-root/cases/rt_a1_ni_lattice_20261022_011810/intent.json",
        "infile": MDAGENT / "case_runs/rt/a1_scan/in.case",
        "gate": MDAGENT / "case_runs/rt/rt_a1_validate.json",
    },
}
LAYER_ROOT = LAYER
for _i in range(1, 6):
    CASES["case%d" % _i] = {
        "title": "case%d · 测试案例" % _i,
        "intent": LAYER_ROOT / ("cases/case%d/intent.json" % _i),
        "infile": LAYER_ROOT / ("cases/case%d/in.case" % _i),
        "gate": None,
    }


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _j(p):
    return json.loads(p.read_text(encoding="utf-8"))


def _fmt(v):
    if v is None:
        return "（未填写）"
    if isinstance(v, (list, tuple)):
        return "、".join(map(str, v))
    if isinstance(v, dict):
        return "、".join("%s=%s" % (k, v[k]) for k in v)
    return str(v)


def _guess_type(goal):
    g = (goal or "").lower()
    if "剪切" in g or "shear" in g: return "shear"
    if "磨" in g or "摩擦" in g or "friction" in g: return "friction"
    if "拉伸" in g or "tensile" in g or "拉拔" in g: return "tension"
    if "压痕" in g or "indent" in g: return "indentation"
    if "熔" in g or "热膨胀" in g or "thermal" in g or "晶格" in g or "密度" in g: return "thermal"
    return "generic"


def _schematic(intent):
    obj = intent.get("objective") or {}
    sysd = intent.get("system") or {}
    cons = intent.get("constraints") or {}
    ed = intent.get("experiment_design") or {}
    seq = intent.get("ensemble_sequence") or []
    sections = (intent.get("experiment_plan") or {}).get("sections") or []
    loading = next((s.get("text") for s in sections if s.get("id") == "E-04"), "") or (obj.get("simulation_goal") or "")
    return {
        "experiment_type": ed.get("experiment_type") or _guess_type(obj.get("simulation_goal") or ""),
        "material": sysd.get("molecule") or "（材料未声明）",
        "loading": loading[:80],
        "temperature": cons.get("temperature_range"),
        "model": sysd.get("molecule") or "",
        "atoms": (sysd.get("size") or {}).get("atoms_target"),
        "potential": (intent.get("key_params") or {}).get("forcefield", {}).get("preferred") if isinstance((intent.get("key_params") or {}).get("forcefield"), dict) else None,
        "ensemble": "、".join(s.get("ensemble") for s in seq if s.get("ensemble")),
    }


def build_plan(cid):
    cfg = CASES[cid]
    intent = _j(cfg["intent"])
    gate = _j(cfg["gate"]) if cfg.get("gate") and cfg["gate"].exists() else {}
    if (intent.get("experiment_plan") or {}).get("sections"):
        return _build_case_plan(cid, cfg, intent, gate)
    kp = intent.get("key_params") or {}
    sysd = intent.get("system") or {}
    obj = intent.get("objective") or {}
    cons = intent.get("constraints") or {}
    seq = intent.get("ensemble_sequence") or []
    design = intent.get("design") or {}
    kv = obj.get("key_values") or []
    kv_text = "；".join("%s=%s（%s）" % (k.get("name"), k.get("value"), k.get("source")) for k in kv) or "（未填写）"
    stage_text = "；".join(
        "%s=%s/%s/%sK/%sps" % (s.get("stage"), s.get("ensemble"), s.get("thermostat"), s.get("T_K"), s.get("duration_ps"))
        if s.get("ensemble") else str(s.get("stage")) for s in seq) or "（未声明）"

    exp = [
        {"id": "E-01", "title": "目的与对照量", "text": "%s。对照量：%s。实验基准：%s。" % (obj.get("simulation_goal") or "（未填写）", _fmt(obj.get("target_property")), kv_text)},
        {"id": "E-02", "title": "对象与材料", "text": "%s；元素 %s；规模 %s 原子；密度 %s g/cm³。" % (sysd.get("molecule") or "（未填写）", _fmt(sysd.get("target_elements")), _fmt((sysd.get("size") or {}).get("atoms_target")), _fmt((sysd.get("size") or {}).get("density_target")))},
        {"id": "E-03", "title": "装置与几何", "text": "实验装置与几何未在 intent 中单列（若为文献对照，见实验基准来源）。"},
        {"id": "E-04", "title": "加载与条件", "text": "温度范围 %s；淬火=%s；升温 ramp=%s；机时 %s h；核数 %s。" % (_fmt(cons.get("temperature_range")), _fmt((cons.get("protocol_flags") or {}).get("is_quench")), _fmt((cons.get("protocol_flags") or {}).get("has_heating_ramp")), _fmt(cons.get("walltime_hours")), _fmt(cons.get("cores")))},
        {"id": "E-05", "title": "测量与判据", "text": "目标量：%s；实验基准：%s。" % (_fmt(obj.get("target_property")), kv_text)},
        {"id": "E-06", "title": "记录与确认", "text": "确认引语：%s。" % next((d.get("confirm_quote") for d in (intent.get("decisions") or []) if d.get("confirm_quote")), "（未记录）")},
    ]
    sim = [
        {"id": "S-01", "title": "目标与对应量", "text": "%s。目标量：%s。" % (obj.get("simulation_goal") or "（未填写）", _fmt(obj.get("target_property"))), "why": "对应量按实验可测值选取：%s。" % kv_text},
        {"id": "S-02", "title": "模型与势函数", "text": "模型：%s（%s 原子）；势函数：%s；pair_style=%s；units=%s。" % (sysd.get("molecule") or "（未填写）", _fmt((sysd.get("size") or {}).get("atoms_target")), _fmt((kp.get("forcefield") or {}).get("preferred")), _fmt(kp.get("pair_style")), _fmt(kp.get("units"))), "why": "选型确认记录：%s。" % _fmt((design.get("forcefield") or {}).get("chosen_by"))},
        {"id": "S-03", "title": "系综与控温", "text": "阶段序列：%s。" % stage_text, "why": "四要素确认记录：%s。" % _fmt((design.get("dynamics") or {}).get("chosen_by"))},
        {"id": "S-04", "title": "时间步与步数", "text": "步长 %s fs；总步数 %s；核数 %s。" % (_fmt(kp.get("timestep_fs")), _fmt(kp.get("total_steps")), _fmt(kp.get("cores")))},
        {"id": "S-05", "title": "简化假设", "text": "（intent 未单列简化假设字段；以实验/模型对照项为准。）"},
        {"id": "S-06", "title": "对照协议", "text": "对照量：%s；实验基准：%s。" % (_fmt(obj.get("target_property")), kv_text)},
    ]

    comparisons = []
    goal = (obj.get("simulation_goal") or "").lower()
    declared = {s.get("ensemble", "").lower() for s in seq if s.get("ensemble")}
    if "npt" in goal and "npt" not in declared:
        comparisons.append({"id": "cmp-ens", "dim": "系综", "exp": "目标声明：NPT 测量平衡晶格与密度", "sim": "ensemble_sequence 仅声明：%s" % ("、".join(sorted(declared)) or "无"), "diff": "目标 NPT、序列 nvt、in 文件 NPT——方案字段过期，需修订（不是仿真不成立）", "verdict": "plan_conflict"})
    comparisons.append({"id": "cmp-temp", "dim": "温度", "exp": "实验基准 300 K（%s）" % kv_text, "sim": "控温设定 %s" % _fmt([s.get("T_K") for s in seq if s.get("T_K")]), "diff": "温度口径一致" if any(s.get("T_K") == 300 for s in seq) else "温度未对齐", "verdict": "ok" if any(s.get("T_K") == 300 for s in seq) else "q"})
    comparisons.append({"id": "cmp-size", "dim": "体系规模", "exp": "块体实验样品（宏观）", "sim": "%s 原子周期单胞" % _fmt((sysd.get("size") or {}).get("atoms_target")), "diff": "尺寸差约 10^20；需声明尺寸效应", "verdict": "q"})
    comparisons.append({"id": "cmp-ff", "dim": "材料与势", "exp": "%s（实验）" % (sysd.get("molecule") or "金属"), "sim": "%s / %s" % (_fmt((kp.get("forcefield") or {}).get("preferred")), _fmt(kp.get("pair_style"))), "diff": "同元素标准 EAM；文献论证未记录则存疑", "verdict": "q"})
    comparisons.append({"id": "cmp-obs", "dim": "观测量", "exp": _fmt(obj.get("target_property")), "sim": "输出量未在 intent 单列", "diff": "输出统计与判据未显式声明", "verdict": "q"})

    gate_summary = {"status": gate.get("status") or "UNKNOWN", "checked_at": gate.get("checked_at"),
                    "hard": len(gate.get("hard_violations") or []), "uncertain": len(gate.get("uncertain_findings") or []),
                    "soft": len(gate.get("soft_warnings") or [])}
    prov = {"intent": str(cfg["intent"].relative_to(WORKSPACE)), "intent_sha256": _sha(cfg["intent"]),
            "infile": str(cfg["infile"].relative_to(WORKSPACE)), "infile_sha256": _sha(cfg["infile"]),
            "gate": str(cfg["gate"].relative_to(WORKSPACE)) if cfg["gate"].exists() else "",
            "gate_sha256": _sha(cfg["gate"]) if cfg["gate"].exists() else "",
            "generated_by": "MDagent artifacts (read-only)"}
    return {"case_id": cid, "title": cfg["title"], "experiment_plan": {"sections": exp},
            "simulation_plan": {"sections": sim}, "comparisons": comparisons,
            "gate": gate_summary, "provenance": prov, "schematic": _schematic(intent)}


def _build_case_plan(cid, cfg, intent, gate):
    text = cfg["infile"].read_text(encoding="utf-8")
    info = parse_infile(text)
    exp = intent["experiment_plan"]["sections"]
    sim = sim_sections(intent, info)
    expmap = {s["id"]: s["text"] for s in exp}
    simmap = {s["id"]: s["text"] for s in sim}
    pairs = [("目的与判据", "E-01/E-05", "S-01/S-09", "ok"),
             ("材料与势", "E-02", "S-02", None),
             ("装置与几何", "E-03", "S-03", "q"),
             ("加载", "E-04", "S-05", None),
             ("环境", "E-05", "S-04", None),
             ("观测", "E-05", "S-07", None)]
    engine = {i.element: i for i in build_consistency(intent, text)}
    comparisons = []
    loading = (intent.get("cross_scale") or {}).get("loading") or {}
    vmap = {"ok": "ok", "questionable": "q", "unproven": "q", "invalid": "bad", "plan_conflict": "q"}
    for i, (dim, eid, sid, v) in enumerate(pairs, 1):
        etext = next((expmap[k] for k in eid.split("/") if k in expmap), "")
        stext = next((simmap[k] for k in sid.split("/") if k in simmap), "")
        if dim == "加载" and loading:
            hit = engine.get("加载速率")
            comparisons.append({"id": "cmp-%d" % i, "dim": dim,
                                "exp": loading.get("macro", etext), "sim": loading.get("micro", stext),
                                "diff": "差距：%s；%s%s" % (loading.get("gap", ""), loading.get("status", ""),
                                                          ("；实测：" + hit.detail) if hit else ""),
                                "verdict": vmap.get(hit.verdict, "q") if hit else loading.get("verdict", "q"),
                                "rationale": loading.get("rationale", "")})
            continue
        if dim == "材料与势" and engine.get("势函数"):
            comparisons.append({"id": "cmp-%d" % i, "dim": dim, "exp": etext, "sim": stext,
                                "diff": engine["势函数"].detail,
                                "verdict": vmap.get(engine["势函数"].verdict, "q")})
            continue
        if dim == "环境" and engine.get("温度"):
            comparisons.append({"id": "cmp-%d" % i, "dim": dim, "exp": etext, "sim": stext,
                                "diff": engine["温度"].detail,
                                "verdict": vmap.get(engine["温度"].verdict, "q")})
            continue
        if dim == "观测" and engine.get("观测量"):
            comparisons.append({"id": "cmp-%d" % i, "dim": dim, "exp": etext, "sim": stext,
                                "diff": engine["观测量"].detail,
                                "verdict": vmap.get(engine["观测量"].verdict, "q")})
            continue
        comparisons.append({"id": "cmp-%d" % i, "dim": dim, "exp": etext, "sim": stext,
                            "diff": "字段对应一致" if v == "ok" else "对应关系已建立；宏观实验为 AI 补写，待确认",
                            "verdict": v or "q"})
    gate_summary = {"status": (gate.get("status") or "UNKNOWN"), "checked_at": gate.get("checked_at"),
                    "hard": len(gate.get("hard_violations") or []), "uncertain": len(gate.get("uncertain_findings") or []),
                    "soft": len(gate.get("soft_warnings") or [])}
    prov = {"intent": str(cfg["intent"].relative_to(LAYER_ROOT)), "intent_sha256": _sha(cfg["intent"]),
            "infile": str(cfg["infile"].relative_to(LAYER_ROOT)), "infile_sha256": _sha(cfg["infile"]),
            "gate": "", "gate_sha256": "", "generated_by": "WSL LAMMPS case (read-only copy); macro plan AI-drafted"}
    return {"case_id": cid, "title": cfg["title"], "experiment_plan": {"sections": exp},
            "simulation_plan": {"sections": sim}, "comparisons": comparisons,
            "gate": gate_summary, "provenance": prov, "schematic": _schematic(intent)}


def _discovery_path(cid):
    return LAYER / "reports" / "discovery" / ("%s_points.json" % cid)


def build_discovery(cid):
    p = _discovery_path(cid)
    if not p.exists():
        return {"case": cid, "available": False, "points": [], "counts": {}}
    data = _j(p)
    points = data.get("points", [])
    counts = common_issues.annotate(points)
    slim = [{"id": x["point_id"], "title": x["title"], "dimension": x["dimension_guess"],
             "locations": x["locations"], "severity": x["severity_hint"],
             "disposition": x.get("disposition"), "common_issue_id": x.get("common_issue_id"),
             "common_issue_name": x.get("common_issue_name")} for x in points]
    return {"case": cid, "available": True, "provider": data.get("provider", ""),
            "points": slim, "counts": counts}


def build_ukb_debug(cid, point_id):
    p = _discovery_path(cid)
    if not p.exists():
        return {"ok": False, "error": "no discovery data for " + cid}
    data = _j(p)
    point = next((x for x in data.get("points", []) if x["point_id"] == point_id), None)
    if not point:
        return {"ok": False, "error": "point not found: " + point_id}
    common_issues.annotate([point])
    trace = common_issues.match_trace(point)
    dim = map_dimension(point)
    baseline = ukb_store.load_baseline() if ukb_store.enabled() else []
    try:
        stats = dict(ukb_store.stats()) if ukb_store.enabled() else {}
        root = str(ukb_store.root())
    except Exception:
        stats, root = {}, ""
    return {"ok": True, "case": cid, "point": {
                "id": point["point_id"], "title": point["title"],
                "dimension": point["dimension_guess"], "locations": point["locations"],
                "question": point["physical_question"], "severity": point["severity_hint"],
                "disposition": point.get("disposition")},
            "trace": trace, "baseline": {"mapped_dimension": dim,
                                         "in_baseline": bool(dim and dim in baseline),
                                         "dimensions": baseline},
            "ukb": {"root": root, "stats": stats},
            "retrieval_path": [
                "UKB catalog(profile=agent, types=[lesson])",
                "UKB read(entry_refs=[...])  # 12 条常见问题",
                "keyword match: title+dimension+why+question",
                "selected: " + (trace["selected"]["id"] if trace["selected"] else "none"),
            ]}


def build_check(cid):
    cfg = CASES[cid]
    intent = _j(cfg["intent"])
    text = cfg["infile"].read_text(encoding="utf-8")
    items = build_consistency(intent, text)
    lines = text.splitlines()
    vmap = {"ok": "match", "plan_conflict": "warn", "questionable": "warn",
            "unproven": "warn", "invalid": "fail"}
    out = []
    for it in items:
        out.append({"id": it.id, "element": it.element, "claim": it.claim, "detail": it.detail,
                    "anchors": it.anchors, "verdict": vmap.get(it.verdict, "warn"),
                    "file": cfg["infile"].name,
                    "anchor": "L" + ",L".join(map(str, it.anchors)) if it.anchors else "—",
                    "code": " / ".join(lines[n - 1].strip() for n in it.anchors if 0 < n <= len(lines))[:160]})
    status = "FAIL" if any(i["verdict"] == "fail" for i in out) else "PASS"
    return {"status": status, "items": out, "summary": {"total": len(out),
            "fail": sum(1 for i in out if i["verdict"] == "fail")}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name, ctype):
        p = STATIC / name
        if not p.exists():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._file("design-plan.html", "text/html; charset=utf-8")
        if u.path == "/api/cases":
            return self._json([{"id": k, "title": v["title"]} for k, v in CASES.items()])
        cid = (q.get("case") or ["rt_a3_al_npt"])[0]
        if cid not in CASES:
            return self._json({"error": "unknown case"}, 404)
        if u.path == "/api/plan":
            return self._json(build_plan(cid))
        if u.path == "/api/infile":
            return self._json({"name": CASES[cid]["infile"].name,
                               "lines": CASES[cid]["infile"].read_text(encoding="utf-8").splitlines()})
        if u.path == "/api/infile_check":
            return self._json(build_check(cid))
        if u.path == "/api/discovery":
            return self._json(build_discovery(cid))
        if u.path == "/api/ukb/debug":
            pid = (q.get("point") or [""])[0]
            return self._json(build_ukb_debug(cid, pid))
        return self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    ok, msg = template_check()
    if not ok:
        sys.exit("template check failed: " + msg)
    print("Template check: " + msg, flush=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    print("Design-plan reviewer: http://127.0.0.1:%d" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
