# -*- coding: utf-8 -*-
"""
mdproj 受控写 MCP 服务器（stdio, 零第三方协议依赖）
====================================================
功能一「仿真方案设计」+ 功能二「物理一致性校核」的落盘与状态机守卫
（docs/设计规格_仿真方案设计与物理一致性校核.md §1/§4）。

- 职责边界（三分树）:写只落 PROJECT_ROOT（本 MCP 自有配置,不进 md_config.json）,
  只暴露两个动作——PROJECT_ROOT 内 mkdir、写 *.json/md 骨架;模拟真相源永远只读。
- 状态机铁腕:对话由人格自由驱动,但 DRAFT→CLARIFIED→DESIGNED→CONFIRMED→
  GATED_PASS/GATED_FAIL 的每次跃迁都在这里守卫,提示词 rationalize 不掉。
- 五道写闸:①realpath 限域(非字符串 `..` 检查) ②project/case_id 标识符白名单
  ③JSON 内容=分段白名单受控原语(拒绝 prod_dir_ref 等越界字段注入)
  ④独立审计落 PROJECT_ROOT/_audit/ ⑤首个写的人格侧回显确认纪律(见 _discipline)。
- 物理校核:validate_intent 调 physics_gate.run_checks(纯函数),
  模型只负责把 gate_report 翻译成人话、陪用户逐条审议(C4/C5)。
- 传输: MCP stdio = 换行分隔 JSON-RPC 2.0。日志走 stderr,绝不污染 stdout。
"""
import hashlib
import json
import os
import re
import sys
import time
import traceback

# 环境免疫(病灶 D-04):启动即锁 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL_VERSION_FALLBACK = "2024-11-05"
SCHEMA_VERSION = "3.0"  # v3.0(2026-10-04):四段(material_model/experiment_reference/
                        # abstraction/comparability)+task_grade,承载实验-仿真一致性审阅

# ---------- 状态机常量(唯一出处;与设计规格 §1.3 对齐) ----------
STATUSES = ("DRAFT", "CLARIFIED", "DESIGNED", "CONFIRMED", "GATED_PASS", "GATED_FAIL")
EDITABLE_SECTIONS = ("source", "intake_summary", "system", "objective", "constraints",
                     "ensemble_sequence", "key_params", "experiment_design",
                     "material_model", "experiment_reference", "abstraction",
                     "comparability", "design",
                     "open_questions", "exemptions", "resolutions")
DRAFT_PAYLOAD_SECTIONS = ("source", "intake_summary", "system", "objective",
                          "constraints", "ensemble_sequence", "key_params",
                          "experiment_design", "material_model", "experiment_reference",
                          "abstraction", "comparability")
DESIGN_FACETS = ("system", "forcefield", "dynamics", "resources")

IDENT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def log(msg):
    sys.stderr.write("[md_proj] %s\n" % msg)
    sys.stderr.flush()


def _cfg():
    p = os.path.join(HERE, "mdproj_config.json")
    if not os.path.exists(p):
        p += ".example"
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def project_root():
    """PROJECT_ROOT 解析(每次动态读,测试用 MDPROJ_ROOT 重定向——同 paths.py 哲学):
    环境变量 > mdproj_config.json project_root > 默认 <代码根父目录>/mdsim-projects。"""
    raw = (os.environ.get("MDPROJ_ROOT", "").strip()
           or str(_cfg().get("project_root") or "").strip())
    if not raw:
        raw = os.path.join(os.path.dirname(os.path.dirname(HERE)), "mdsim-projects")
    raw = os.path.expanduser(raw)
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.dirname(os.path.dirname(HERE)), raw)
    return os.path.abspath(raw)


# ---------- 闸①②:realpath 限域 + 标识符白名单 ----------
def _confined(path):
    """realpath 规范化后仍在 PROJECT_ROOT 内才放行(符号链接解析到根外即拒)。"""
    root = os.path.realpath(project_root())
    rp = os.path.realpath(path)
    return rp == root or rp.startswith(root + os.sep)


def _valid_ident(name):
    return isinstance(name, str) and bool(IDENT_RE.match(name))


def _require_idents(*names):
    for label, v in names:
        if not _valid_ident(v):
            return {"error": "非法 %s 标识符 %r:仅允许 [A-Za-z0-9_-],禁止 / 与 .. "
                             "(来自文献/会话的不可信文本永不直接当路径)" % (label, v)}
    return None


# ---------- 闸④:独立审计(PROJECT_ROOT/_audit/,不复用 AGENT_BASE 账本) ----------
def _audit(action, target, before=None, after=None):
    try:
        root = project_root()
        ad = os.path.join(root, "_audit")
        os.makedirs(ad, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": "mdproj",
               "action": action, "target": target,
               "before": _digest(before), "after": _digest(after)}
        with open(os.path.join(ad, "audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as ex:
        log("audit failed: %s" % ex)


def _digest(obj):
    """审计里只留摘要(状态/键集/内容哈希),不整包灌账本。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        return {"keys": sorted(obj.keys()), "status": obj.get("status"),
                "sha": hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]}
    return {"sha": hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:16]}


# ---------- 路径定位 ----------
def _project_dir(project):
    return os.path.join(project_root(), project)


def _find_run_dir(project, case_id):
    """定位 case 最新 run_folder(<case_id>_<YYYYMMDD>_<HHMMSS>,本地标识非 prod_dir)。"""
    pd = _project_dir(project)
    if not _confined(pd) or not os.path.isdir(pd):
        return None
    cands = [d for d in os.listdir(pd)
             if d == case_id or d.startswith(case_id + "_")]
    if not cands:
        return None
    # 排序键:目录名内嵌时间戳(run_folder=<case_id>_<YYYYMMDD>_<HHMMSS>)优先,
    # mtime 为退化回退——同步盘/复制会把旧目录 mtime 刷新,取错目录(2026-10-14 实跑)
    def _key(d):
        m = re.search(r"_(\d{8}_\d{6})(?:_\d+)?$", d)
        return (1, m.group(1)) if m else (0, "")
    return os.path.join(pd, max(cands, key=lambda d: (_key(d), os.path.getmtime(os.path.join(pd, d)))))


def _intent_path(run_dir):
    return os.path.join(run_dir, "intent.json")


def _load_intent(project, case_id):
    rd = _find_run_dir(project, case_id)
    if not rd:
        return None, {"error": "未找到任务 run_folder:project=%s case_id=%s "
                               "(先调 new_task 建任务夹)" % (project, case_id)}
    ip = _intent_path(rd)
    if not os.path.exists(ip):
        return None, {"error": "intent.json 缺失: %s" % ip}
    try:
        with open(ip, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as ex:
        return None, {"error": "intent.json 解析失败(%s): %s" % (ip, ex)}


def _save_intent(run_dir, intent):
    ip = _intent_path(run_dir)
    if not _confined(ip):
        return {"error": "目标路径越界拒写: %s" % ip}
    with open(ip, "w", encoding="utf-8") as f:
        json.dump(intent, f, ensure_ascii=False, indent=2)
    return None


# ---------- 归纳说明自动生成(templates 黄金标准四节,文件数/大小实测) ----------
def _dir_stat(path):
    n, size = 0, 0
    for _r, _d, files in os.walk(path):
        for fn in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(_r, fn))
            except OSError:
                pass
    return n, size


def _fmt_size(b):
    for unit in ("B", "K", "M", "G"):
        if b < 1024 or unit == "G":
            return "%.0f%s" % (b, unit) if unit == "B" else "%.1f%s" % (b, unit)
        b /= 1024.0
    return "%d B" % b


def _render_summary(dir_path, title, provenance, rows, quickfind, cautions):
    """套 templates/归纳说明_模板.md 四节结构;rows=[(name, 说明)]。"""
    lines = ["# %s" % title, "", "## 数据来源 / 说明", provenance,
             "", "> 由 mdproj 自动生成(文件数/大小程序实测);task_status 可刷新。", "",
             "## 目录结构", "",
             "| 编号 | 目录名/文件名 | 文件数 | 大小 | 内容说明 |",
             "|------|--------------|-------:|-----:|----------|"]
    for i, (name, note) in enumerate(rows):
        p = os.path.join(dir_path, name)
        if os.path.isdir(p):
            n, s = _dir_stat(p)
        elif os.path.exists(p):
            n, s = 1, os.path.getsize(p)
        else:
            n, s = 0, 0
        lines.append("| %02d | `%s` | %d | %s | %s |" % (i, name, n, _fmt_size(s), note))
    lines += ["", "## 核心资产速查", "", quickfind, "", "## 注意事项", "", cautions, ""]
    return "\n".join(lines)


def _write_file(path, text):
    if not _confined(path):
        return {"error": "目标路径越界拒写: %s" % path}
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return None


# ---------- 温度程序/实时态交叉(只读,失败静默) ----------
def schedule_for(project):
    """项目专属温度程序(md_config.json schedules[project],唯一出处);读不到返回 None。
    设计期交叉核对只用项目专属程序——不做 paths.load_schedule 的全局兜底:
    全局 schedule 属历史默认 case,拿它核对新项目会产生假阳性(2026-09-30 e2e 实测)。"""
    try:
        scheds = _mdsim_cfg().get("schedules")
        if not isinstance(scheds, dict) or not project:
            return None
        return scheds.get(project)
    except Exception:
        return None


def _kit_base():
    return (os.environ.get("MDSIM_KIT_BASE", "").strip()
            or str(_mdsim_cfg().get("local_base") or "").strip())


def _mdsim_cfg():
    try:
        with open(os.path.join(HERE, "md_config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _live_case(case_id):
    """交叉 status_bus.cases[case_id](只读);并比对 live prod_dir 与 intent.prod_dir_ref。"""
    base = _kit_base()
    if not base:
        return None
    try:
        with open(os.path.join(base, "status_bus.json"), "r", encoding="utf-8") as f:
            bus = json.load(f)
        cv = (bus.get("cases") or {}).get(case_id)
        if not isinstance(cv, dict):
            return None
        return {k: cv.get(k) for k in ("status", "step", "pct", "temp_k") if k in cv}
    except Exception:
        return None


# ---------- 状态机辅助 ----------
def _questions_resolved(intent):
    qs = intent.get("open_questions")
    if not isinstance(qs, list):
        return True
    return all(isinstance(q, dict) and q.get("answered") for q in qs)


def _design_complete(intent):
    d = intent.get("design")
    if not isinstance(d, dict):
        return False
    return all(isinstance(d.get(f), dict) and d[f].get("chosen_by") for f in DESIGN_FACETS)


def _design_setup_present(intent):
    ed = intent.get("experiment_design")
    return isinstance(ed, dict) and bool(ed.get("setup"))


def _design_evidence_missing(intent):
    """C2e 守卫:有 experiment_design.setup 但既无外部证据也无用户显式接受的存疑假设。"""
    if not _design_setup_present(intent):
        return False
    plaus = _plausibility_of(intent)
    return not (plaus.get("evidence") or plaus.get("unverified_assumptions"))


def _plausibility_of(intent):
    """证据/假设容器(2026-10-14 v3 泛化):顶层 plausibility 为唯一写入口;
    读取兼容旧位置 experiment_design.plausibility(无条件合并)。"""
    top = intent.get("plausibility")
    legacy = (intent.get("experiment_design") or {}).get("plausibility")
    if not isinstance(top, dict) and not isinstance(legacy, dict):
        return {}
    out = {}
    for src in (legacy, top):
        if isinstance(src, dict):
            for k in ("evidence", "unverified_assumptions", "checked_at"):
                v = src.get(k)
                if v:
                    if isinstance(v, list) and isinstance(out.get(k), list):
                        out[k] = out[k] + v
                    else:
                        out[k] = v
    return out


def _merge_design_evidence(intent, entries):
    """把 sim_design_research 提炼的逐字段判定并入顶层 plausibility.evidence(受控通道)。
    每条须有 field + 至少一个 http 证据 URL,否则拒收该条;返回 (存条数, 弃条数)。
    2026-10-14:从 experiment_design.plausibility 泛化到顶层——v3 证据覆盖任意字段,
    不再强制无实验设计段的案例携带空 experiment_design(v1 案例实跑发现)。"""
    if not isinstance(entries, list) or not entries:
        return 0, 0
    plaus = intent.setdefault("plausibility", {})
    kept = plaus.setdefault("evidence", [])
    existing = {e.get("field") for e in kept if isinstance(e, dict)}
    ok = bad = 0
    for e in entries:
        if not isinstance(e, dict) or not str(e.get("field") or "").strip():
            bad += 1
            continue
        urls = [u for u in (e.get("evidence") or [])
                if isinstance(u, str) and u.startswith("http")]
        if not urls:
            bad += 1
            continue
        field = str(e["field"]).strip()
        if field in existing:
            kept[:] = [k for k in kept if k.get("field") != field]
        kept.append({"field": field, "verdict": str(e.get("verdict", "")),
                     "literature_range": str(e.get("literature_range", "")),
                     "evidence": urls[:5]})
        existing.add(field)
        ok += 1
    return ok, bad


def _cited_roots(intent):
    """gate_report 硬违规点名 cited_fields 的顶层段集合(C4r 差异整改限域)。"""
    roots = set()
    for v in ((intent.get("gate_report") or {}).get("hard_violations")) or []:
        for f in v.get("cited_fields") or []:
            roots.add(str(f).split(".")[0].split(" ")[0])
    return roots


def _next_step(status, intent):
    return {
        "DRAFT": "C0:回显需求理解,逐题回答 open_questions 后 update_intent 推进 CLARIFIED",
        "CLARIFIED": "C1/C2:物理框架+材料模型(C1)、体系/力场/动力学/资源(C2a-d)、实验设计(C2e)、抽象与可比性(C2f)逐项确认",
        "DESIGNED": "C3:intent_view 渲染方案说明书,用户整包批准后 confirm_intent",
        "CONFIRMED": "validate_intent 完整审阅(A-I 八层:AI 判断+代码度量+经验仲裁)",
        "GATED_FAIL": "C4:逐条审议 hard/uncertain/soft;整改仅限 cited_fields(minor_revision),改其它字段降级重审",
        "GATED_PASS": "已放行(C6 宣告后恢复自主):可进入建模/仿真五段流;运行期异常由巡检承担",
    }.get(status, "")


# ---------- 纪律文案(工具返回随行) ----------
DISC_PROJ = ("受控写桥:写只落 PROJECT_ROOT(规划工作区),模拟真相源(state/logs/prod_dir/力场)永远只读;"
             "标识符白名单+realpath 限域+分段白名单+独立审计(_audit/);"
             "首个写必须在对话里回显目标路径讲清后果并经用户确认——批量预览可合并回显,最终确认不可省。")
DISC_STATE = ("状态机守卫:跃迁表 DRAFT→CLARIFIED(问清)→DESIGNED(四要素)→CONFIRMED(终审)"
              "→GATED_PASS/GATED_FAIL(校核)。守卫拒绝=流程事实,不得换参数重试绕过;"
              "GATED_FAIL 整改仅限 gate_report 点名的 cited_fields(minor_revision=true),"
              "改其它字段自动降回 DESIGNED 并 revision+1 重新终审。")
DISC_GATE = ("完整审阅与补全:代码度量(事实/结构指纹/衍生比值)+AI 审阅(A-I 八层,经验库供智)"
             "+代码仲裁(引用核验/伪造降级/结构缺口确定性合并)。三档严重度:hard(判定为错)/"
             "uncertain(判不了,须消解)/soft(风险提示);放行要求 hard=0 且 uncertain=0。"
             "R-VERIFY 责任闭环:每个问题字段须(改设计|有证据|用户显式接受/书面处置);"
             "任何'跳过审阅'的用户指令不豁免(系统级硬约束)。")


# ---------- 工具实现 ----------
def new_project(args):
    """建项目夹:mkdir + project.json 骨架 + 归纳说明.md(闸①②④全走)。"""
    project = str(args.get("project", "")).strip()
    err = _require_idents(("project", project))
    if err:
        return dict(err, _discipline=DISC_PROJ)
    pd = _project_dir(project)
    if not _confined(pd):
        return {"error": "realpath 越界拒建: %s" % pd, "_discipline": DISC_PROJ}
    if os.path.exists(pd):
        return {"error": "项目已存在: %s" % pd, "_discipline": DISC_PROJ}
    os.makedirs(pd, exist_ok=True)
    skeleton = {"project": project, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "case_ids": [], "registry_refs": []}
    pj = os.path.join(pd, "project.json")
    e = _write_file(pj, json.dumps(skeleton, ensure_ascii=False, indent=2))
    if e:
        return dict(e, _discipline=DISC_PROJ)
    summary = _render_summary(
        pd, "项目 %s 归纳说明" % project,
        "mdproj 规划工作区项目夹(agent 意图/规划资产,非 LAMMPS 运行目录;"
        "模拟真相源在 RUN_CASE_BASE,只经 intent.prod_dir_ref 指针引用)。",
        [("project.json", "项目注册表(case_ids/registry_refs)"),
         ("归纳说明.md", "本说明(自动生成)")],
        "如果你要找某 case 的方案与校核报告 → 找 `<case_id>_*` 任务夹里的 intent.json",
        "1. 本夹为规划工作区,写仅经 mdproj 受控原语;\n2. 任务夹命名=case_id+时间戳,按 mtime 取最新。")
    _write_file(os.path.join(pd, "归纳说明.md"), summary)
    _audit("new_project", pd, None, skeleton)
    return {"project": project, "project_dir": pd, "project_json": pj,
            "summary_md": os.path.join(pd, "归纳说明.md"),
            "next_step": "new_task 建任务夹(run_folder + intent.json 骨架)",
            "_discipline": DISC_PROJ}


def new_task(args):
    """建任务夹:<case_id>_<ts>/{inputs,logs,results} + intent.json 骨架(DRAFT) + 归纳说明 + 登记 case_ids。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_PROJ)
    pd = _project_dir(project)
    pj = os.path.join(pd, "project.json")
    if not os.path.isdir(pd) or not os.path.exists(pj):
        return {"error": "项目不存在: %s(先 new_project)" % project, "_discipline": DISC_PROJ}
    ts = time.strftime("%Y%m%d_%H%M%S")
    run_name = "%s_%s" % (case_id, ts)
    rd = os.path.join(pd, run_name)
    if os.path.exists(rd):
        rd = os.path.join(pd, "%s_%s_2" % (case_id, ts))
    for sub in ("inputs", "logs", "results"):
        os.makedirs(os.path.join(rd, sub), exist_ok=True)
    skeleton = {"schema_version": SCHEMA_VERSION, "case_id": case_id, "project": project,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "DRAFT",
                "revision": 0, "task_grade": None, "source": {}, "intake_summary": {},
                "system": {}, "objective": {}, "constraints": {},
                "material_model": {}, "experiment_reference": {}, "abstraction": {},
                "comparability": {}, "ensemble_sequence": [],
                "key_params": {}, "experiment_design": {}, "design": {},
                "open_questions": [], "decisions": [],
                "exemptions": [], "resolutions": [], "prod_dir_ref": None,
                "gate_report": None}
    ip = _intent_path(rd)
    e = _write_file(ip, json.dumps(skeleton, ensure_ascii=False, indent=2))
    if e:
        return dict(e, _discipline=DISC_PROJ)
    with open(pj, "r", encoding="utf-8") as f:
        reg = json.load(f)
    if case_id not in reg.get("case_ids", []):
        reg.setdefault("case_ids", []).append(case_id)
        _write_file(pj, json.dumps(reg, ensure_ascii=False, indent=2))
    summary = _render_summary(
        rd, "任务 %s 归纳说明" % case_id,
        "case %s 的方案设计与物理校核工作区(intent.json=意图规格+门报告;"
        "inputs/=草案非真相源不执行;results/ 只留 prod_dir_ref 指针,不建软链)。" % case_id,
        [("intent.json", "结构化意图规格 v2 + gate_report + decisions 决策链"),
         ("inputs/", "拟提交的 data/production.in 草案(非真相源,不执行)"),
         ("logs/", "起草/确认/门校验/审计流水"),
         ("results/", "空;真相只经 intent.prod_dir_ref 指针引用")],
        "如果你要找方案与校核结论 → intent.json;找决策过程 → intent.json 的 decisions[]",
        "1. run_folder 是 agent 本地标识,不保证等于 prod_dir basename,勿反推;\n"
        "2. 本夹写仅经 mdproj 受控原语。")
    _write_file(os.path.join(rd, "归纳说明.md"), summary)
    _audit("new_task", rd, None, skeleton)
    return {"project": project, "case_id": case_id, "run_dir": rd, "intent_path": ip,
            "next_step": "draft_intent 把需求理解落成 DRAFT(带 open_questions)",
            "_discipline": DISC_PROJ}


def draft_intent(args):
    """C0 前置:把对用户需求的理解落成 intent.json(DRAFT)。注入的 status/prod_dir_ref/未知段一律丢弃。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_STATE)
    rd = _find_run_dir(project, case_id)
    if not rd:
        return {"error": "未找到任务夹(先 new_task)", "_discipline": DISC_STATE}
    intent, lerr = _load_intent(project, case_id)
    if lerr:
        return dict(lerr, _discipline=DISC_STATE)
    if intent.get("status") not in ("DRAFT", "GATED_FAIL"):
        return {"error": "整包重写仅允许 DRAFT/GATED_FAIL(当前 %s);已推进的意图用 update_intent 分段改"
                         % intent.get("status"), "_discipline": DISC_STATE}
    payload = args.get("intent") or {}
    if not isinstance(payload, dict):
        return {"error": "intent 须为对象", "_discipline": DISC_STATE}
    for k in DRAFT_PAYLOAD_SECTIONS:
        if k in payload:
            intent[k] = payload[k]
    oq = args.get("open_questions")
    if isinstance(oq, list):
        intent["open_questions"] = oq
    note = str(args.get("note", "")).strip()
    intent["status"] = "DRAFT"
    # draft 即做任务形态判定(paper_grade 触发→即时升级;四段齐备性由审阅层把关)
    try:
        import physics_gate
        eff_grade, grade_reasons = physics_gate.effective_grade(intent)
        if eff_grade == "paper_grade":
            intent["task_grade"] = "paper_grade"
            intent["grade_reasons"] = grade_reasons
        elif not intent.get("task_grade"):
            intent["task_grade"] = "exploratory"
    except Exception:
        pass
    intent["decisions"].append({"cp": "C0", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "note": note or "需求理解落稿(DRAFT)"})
    e = _save_intent(rd, intent)
    if e:
        return dict(e, _discipline=DISC_STATE)
    _audit("draft_intent", _intent_path(rd), None, intent)
    open_n = sum(1 for q in intent["open_questions"] if not q.get("answered"))
    return {"case_id": case_id, "intent_path": _intent_path(rd), "status": "DRAFT",
            "open_question_count": open_n,
            "dropped_keys": sorted(set(payload) - set(DRAFT_PAYLOAD_SECTIONS)),
            "next_step": ("C0:回显 intake_summary 并逐题澄清(%d 题未答),"
                          "答完 update_intent 推进 CLARIFIED" % open_n)
                         if open_n else "open_questions 已清空,update_intent 后自动推进 CLARIFIED",
            "_discipline": DISC_STATE}


def update_intent(args):
    """分段白名单更新 + 状态机跃迁 + decisions 留痕。C1/C2 各要素确认、C4r 差异整改都走这里。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    section = str(args.get("section", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_STATE)
    if section not in EDITABLE_SECTIONS:
        return {"error": "section %r 不在分段白名单 %s(prod_dir_ref/gate_report 等越界字段拒绝)"
                         % (section, list(EDITABLE_SECTIONS)), "_discipline": DISC_STATE}
    # C2e 写权限守卫:外部证据(plausibility.evidence)只能经 design_evidence 参数入账
    # (绑 sim_design_research 研讨产出),patch 里自填一律拒——防 agent 编造证据。
    if section == "experiment_design":
        patch = args.get("patch")
        if isinstance(patch, dict):
            plaus = patch.get("plausibility")
            if isinstance(plaus, dict) and plaus.get("evidence"):
                return {"error": "plausibility.evidence(外部证据)不得经 patch 自填——"
                                 "只能用 update_intent/validate_intent 的 design_evidence 参数"
                                 "(来自 sim_design_research 返回提炼);"
                                 "unverified_assumptions(用户显式接受)可经 patch。",
                        "_discipline": DISC_STATE}
    intent, lerr = _load_intent(project, case_id)
    if lerr:
        return dict(lerr, _discipline=DISC_STATE)
    before_status = intent.get("status")
    if before_status not in ("DRAFT", "CLARIFIED", "DESIGNED", "CONFIRMED",
                             "GATED_FAIL", "GATED_PASS"):
        return {"error": "未知状态 %r" % before_status, "_discipline": DISC_STATE}

    demoted = False
    remediated = False
    if before_status in ("CONFIRMED", "GATED_FAIL"):
        cited = _cited_roots(intent)
        minor = bool(args.get("minor_revision")) and section in cited
        if minor:
            # C4r 差异整改:仅违规点名字段,回到 CONFIRMED 待重新校核(revision 不变)
            intent["status"] = "CONFIRMED"
            remediated = True
        else:
            intent["status"] = "DESIGNED"
            intent["revision"] = int(intent.get("revision", 0)) + 1
            demoted = True
    elif before_status == "GATED_PASS":
        intent["status"] = "DESIGNED"
        intent["revision"] = int(intent.get("revision", 0)) + 1
        demoted = True

    intent[section] = args.get("patch")
    ev_ok, ev_bad = _merge_design_evidence(intent, args.get("design_evidence"))

    # 任务形态分级(代码判定):paper_grade 触发→即时升级落盘;只有 AI 判定的
    # exploratory 声明会被此规则纠正(升级单向,降级须用户在 C3 显式确认)
    import physics_gate
    eff_grade, grade_reasons = physics_gate.effective_grade(intent)
    if eff_grade == "paper_grade":
        if intent.get("task_grade") != "paper_grade":
            intent["task_grade"] = "paper_grade"
            intent["grade_reasons"] = grade_reasons

    # 自动跃迁(仅向上:DRAFT→CLARIFIED→DESIGNED;CONFIRMED 及以上不自动升)
    design_blocker = None
    if intent["status"] == "DRAFT" and _questions_resolved(intent):
        intent["status"] = "CLARIFIED"
    if intent["status"] == "CLARIFIED" and _design_complete(intent):
        if _design_evidence_missing(intent):
            # C2e 守卫:实验设计未经论证不得进终审(对仿真完整负责的钉子)
            design_blocker = ("experiment_design.setup 已声明但既无外部证据"
                              "(design_evidence,来自 sim_design_research 论证)"
                              "也无用户显式接受的存疑假设(unverified_assumptions)"
                              "——先调 sim_design_research(C2e)论证,或经用户确认接受存疑项")
        else:
            intent["status"] = "DESIGNED"

    decision = {"cp": str(args.get("cp", "")).strip() or "C*",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "question_digest": str(args.get("question_digest", "")).strip() or None,
                "answer": str(args.get("answer", "")).strip() or None,
                "fields_changed": [section]}
    ev_refs = args.get("evidence_refs")
    if isinstance(ev_refs, list) and ev_refs:
        decision["evidence_refs"] = [str(u) for u in ev_refs][:8]
    intent.setdefault("decisions", []).append(decision)

    rd = _find_run_dir(project, case_id)
    e = _save_intent(rd, intent)
    if e:
        return dict(e, _discipline=DISC_STATE)
    _audit("update_intent:%s" % section, _intent_path(rd),
           {"status": before_status}, intent)
    out = {"case_id": case_id, "intent_path": _intent_path(rd),
           "status": intent["status"], "revision": intent.get("revision", 0),
           "next_step": _next_step(intent["status"], intent), "_discipline": DISC_STATE}
    if ev_ok or ev_bad:
        out["design_evidence"] = {"accepted": ev_ok, "rejected": ev_bad}
    if design_blocker:
        out["design_gate_blocker"] = design_blocker
        out["next_step"] = design_blocker
    if demoted:
        out["demoted"] = ("改动超出 gate_report 点名字段(cited=%s)或已 GATED_PASS:"
                          "降回 DESIGNED 并 revision+1,须重新 C3 终审+校核"
                          % sorted(_cited_roots(intent) or ["-"]))
    if remediated:
        out["remediated"] = "差异整改成立:仅改违规点名字段,回 CONFIRMED 待重新 validate_intent"
    return out


def confirm_intent(args):
    """C3 方案终审:仅 DESIGNED 可调;须带用户批准原话 confirm_quote → CONFIRMED。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    quote = str(args.get("confirm_quote", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_STATE)
    intent, lerr = _load_intent(project, case_id)
    if lerr:
        return dict(lerr, _discipline=DISC_STATE)
    if intent.get("status") != "DESIGNED":
        return {"error": "终审仅 DESIGNED 可调(当前 %s)。%s" % (
                    intent.get("status"), _next_step(intent.get("status"), intent)),
                "_discipline": DISC_STATE}
    if not quote:
        return {"error": "需要 confirm_quote(用户批准原话摘录,进决策链与审计)", "_discipline": DISC_STATE}
    intent["status"] = "CONFIRMED"
    intent["decisions"].append({"cp": "C3", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "confirm_quote": quote})
    rd = _find_run_dir(project, case_id)
    e = _save_intent(rd, intent)
    if e:
        return dict(e, _discipline=DISC_STATE)
    _audit("confirm_intent", _intent_path(rd), {"status": "DESIGNED"}, intent)
    return {"case_id": case_id, "intent_path": _intent_path(rd), "status": "CONFIRMED",
            "next_step": "validate_intent 物理一致性校核(H1-H5/C1-C4 硬 + S/C 软)",
            "_discipline": DISC_STATE}


def validate_intent(args):
    """物理一致性校核:仅 CONFIRMED 可调;跑 physics_gate 全 PRE 门,写 gate_report 与状态。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_GATE)
    intent, lerr = _load_intent(project, case_id)
    if lerr:
        return dict(lerr, _discipline=DISC_GATE)
    if intent.get("status") != "CONFIRMED":
        return {"error": "校核仅 CONFIRMED 可调(当前 %s)。%s" % (
                    intent.get("status"), _next_step(intent.get("status"), intent)),
                "_discipline": DISC_GATE}

    rd = _find_run_dir(project, case_id)
    inputs_dir = os.path.join(rd, "inputs")
    ctx = {}
    data_file = args.get("data_file")
    if not data_file and os.path.isdir(inputs_dir):
        cands = sorted((fn for fn in os.listdir(inputs_dir)
                        if fn.endswith((".data", ".lmpdat"))),
                       key=lambda fn: os.path.getmtime(os.path.join(inputs_dir, fn)))
        if cands:
            data_file = os.path.join(inputs_dir, cands[-1])
    if data_file and os.path.exists(str(data_file)):
        ctx["data_file"] = data_file
    ffield_file = args.get("ffield_file")
    if not ffield_file:
        preferred = str(((intent.get("key_params") or {}).get("forcefield") or {})
                        .get("preferred") or "")
        base = os.path.basename(preferred)
        for cand_dir in (inputs_dir, os.path.join(HERE, "..", "fields")):
            p = os.path.join(cand_dir, base)
            if base and os.path.exists(p):
                ffield_file = p
                break
    if ffield_file and os.path.exists(str(ffield_file)):
        ctx["ffield_file"] = ffield_file
    # in 脚本进审阅面(2026-10-23):inputs/ 下最近的 .in/.lmp 随 ctx 交审阅层,
    # 与方案声明做一致性核对(单位/步长/系综/控温/加载)。只读,限 2 文件各 3000 字符。
    if os.path.isdir(inputs_dir):
        scripts = []
        sc = sorted((fn for fn in os.listdir(inputs_dir)
                     if fn.endswith((".in", ".lmp"))),
                    key=lambda fn: os.path.getmtime(os.path.join(inputs_dir, fn)))
        for fn in sc[-2:]:
            try:
                with open(os.path.join(inputs_dir, fn), "r",
                          encoding="utf-8", errors="replace") as f:
                    scripts.append({"name": fn, "content": f.read(3000)})
            except OSError:
                continue
        if scripts:
            ctx["input_texts"] = scripts
    sched = schedule_for(project)
    if sched:
        ctx["schedule"] = sched

    import design_review
    report = design_review.run(intent, ctx)
    accepted = args.get("accepted_warnings")
    if isinstance(accepted, list):
        report["accepted_warnings"] = [str(a) for a in accepted]

    # R-VERIFY(2026-10-04,由 E-VERIFY 泛化):审阅 findings(hard/uncertain/soft)
    # 点名的问题字段,必须有外部证据(design_evidence)、用户显式接受的存疑假设
    # (unverified_assumptions),或 resolutions[](书面处置)三者之一,否则合成硬拦。
    # 责任闭环 = 改设计 | 有证据 | 用户显式接受 / 书面处置。
    ev_ok, ev_bad = _merge_design_evidence(intent, args.get("design_evidence"))
    plaus = _plausibility_of(intent)
    ev_fields = {str(e.get("field")) for e in plaus.get("evidence") or []
                 if isinstance(e, dict)}
    as_fields = {str(a.get("field")) for a in plaus.get("unverified_assumptions") or []
                 if isinstance(a, dict)}
    res_fields = {str(r.get("field")) for r in intent.get("resolutions") or []
                  if isinstance(r, dict) and r.get("resolution") in
                  ("fixed", "evidence", "accepted")}

    def _covered(cited):
        # 段级吸收(2026-10-14 实跑修正):处置/证据写到顶层段(如 material_model)时,
        # 覆盖该段下所有字段——用户裁定的是"问题"而非"字段路径",字段级匹配会让
        # 逐轮审阅永远追新字段(案例1/2 各 8/2 轮验证);段级=裁定语义的正确颗粒。
        return any(cited == f or cited.endswith("." + f) or f.endswith("." + cited)
                   or cited.startswith(str(f) + ".")
                   for f in (ev_fields | as_fields | res_fields))

    # uncertain findings 语义="还没判完",其点名字段必须在闭环内(证据/接受/处置),
    # 无法消解的 uncertain 直接阻断放行(见放行闸);soft 点名的 cited 同理覆盖。
    # 过程字段(处置记录/决策链/门报告/豁免/证据容器)是流程自身产物,不是设计内容,
    # 审阅对其引用不计"待处置"(2026-10-14 实跑:审阅引用 resolutions 自身导致死循环风险)
    _PROCESS_ROOTS = {"resolutions", "decisions", "gate_report", "exemptions",
                      "plausibility", "open_questions", "status", "schema_version",
                      "case_id", "project", "created", "revision", "task_grade",
                      "grade_reasons", "prod_dir_ref", "design"}  # 元/流程字段
    uncovered = []
    for lst in (report.get("uncertain_findings") or [], report.get("soft_warnings") or []):
        for w in lst:
            for cited in w.get("cited_fields") or []:
                if str(cited).split(".")[0] in _PROCESS_ROOTS:
                    continue
                if not _covered(cited) and cited not in uncovered:
                    uncovered.append(cited)
    if uncovered:
        report.setdefault("hard_violations", []).append({
            "gate": "R-VERIFY", "severity": "hard",
            "title": "审阅问题字段未经处置(%d 项)" % len(uncovered),
            "detail": "以下被审阅 findings 点名的字段既无外部证据、无用户显式接受的存疑假设、"
                      "也无书面处置(resolutions): %s" % "; ".join(uncovered),
            "rationale": "责任闭环:每个审阅问题必须(改设计 | 有证据 | 用户显式接受/书面处置)三者居一。",
            "remediation_hints": ["调 sim_design_research 论证后经 design_evidence 入账",
                                  "或改设计消除该问题",
                                  "或经用户确认后写入 plausibility.unverified_assumptions / resolutions"],
            "cited_fields": uncovered})
        report["passed"] = False
    # uncertain 消解闸(2026-10-14 修:三选一闭环补全——案例1实跑发现"用户裁定"
    # 路径断裂):存疑 finding 的 cited_fields 全部被 证据(ev)/用户假设(as)/书面处置(res)
    # 覆盖 → 视为已消解不阻断;无 cited_fields 或存在未覆盖字段的 uncertain 阻断放行。
    unresolved_uncertain = []
    for u in report.get("uncertain_findings") or []:
        cited = [str(c) for c in u.get("cited_fields") or []]
        if not cited or not all(_covered(c) for c in cited):
            unresolved_uncertain.append(u)
    # fail-closed 铁律(2026-10-17 修,e2e 场景四抓到回归):审阅层不可用(REVIEW_ERROR)
    # 或输出不可解析(DEGRADED)时违规清单为空——那不是"干净"而是"没审",重算只数
    # 违规会把内部错误洗成放行。此处合成显性硬拦(代码铁闸,非 LLM 判断):
    # 校验器任何内部错误→BLOCK,不放行。
    if report.get("status") in ("REVIEW_ERROR", "DEGRADED"):
        report.setdefault("hard_violations", []).append({
            "gate": "REVIEW-INFRA", "severity": "hard",
            "title": "审阅层不可用,fail-closed 阻断(%s)" % report.get("status"),
            "detail": str(report.get("error") or "审阅器不可用/输出不可解析"),
            "rationale": "内部错误≠通过:无审阅结论时只能阻断,不得默认放行。",
            "remediation_hints": ["检查 LLM 凭证/网络后重试 validate_intent"],
            "cited_fields": []})
    # 终审重算:审阅层 passed 是"原始判定"(uncertain 即 False);流程层(含消解闭环:
    # 证据/假设/书面处置)是最终裁决者——hard 清零且 uncertain 全消解 → 放行。
    report["unresolved_uncertain_count"] = len(unresolved_uncertain)
    report["passed"] = not (report.get("hard_violations") or []) and not unresolved_uncertain
    report["design_review"] = {
        "setup_present": _design_setup_present(intent),
        "evidence_fields": sorted(ev_fields),
        "assumption_fields": sorted(as_fields),
        "resolution_fields": sorted(res_fields),
        "unverified_critical": uncovered,
        "review_status": report.get("status"),
        "task_grade": report.get("task_grade"),
        "layer_summary": report.get("layer_summary"),
        "verdict": report.get("verdict")}

    intent["gate_report"] = report
    if _design_setup_present(intent):
        intent["experiment_design"].setdefault("plausibility", {})["checked_at"] = \
            time.strftime("%Y-%m-%dT%H:%M:%S")
    intent["status"] = "GATED_PASS" if report.get("passed") else "GATED_FAIL"
    e = _save_intent(rd, intent)
    if e:
        return dict(e, _discipline=DISC_GATE)
    _audit("validate_intent", _intent_path(rd), {"status": "CONFIRMED"}, intent)
    out = {"case_id": case_id, "status": intent["status"], "gate_report": report,
           "hard_gates": [v["gate"] for v in report.get("hard_violations") or []],
           "uncertain_gates": [w["gate"] for w in report.get("uncertain_findings") or []],
           "soft_gates": [w["gate"] for w in report.get("soft_warnings") or []],
           "task_grade": report.get("task_grade"),
           "design_review": report.get("design_review"),
           "ctx_sources": {k: bool(v) for k, v in ctx.items()},
           "_discipline": DISC_GATE}
    if ev_ok or ev_bad:
        out["design_evidence"] = {"accepted": ev_ok, "rejected": ev_bad}
    if intent["status"] == "GATED_PASS":
        out["next_step"] = ("C6 放行宣告(方案+门报告+决策链);此后恢复红线 7 自主,"
                            "进入建模/仿真五段流;H6/H7 运行期门由 sim_pipeline_check 承担")
    else:
        out["next_step"] = ("C4:逐条审议硬违规(机理+整改选项,ask_user);整改走 "
                            "update_intent(minor_revision=true,仅 cited_fields=%s)"
                            % sorted(_cited_roots(intent) or ["-"]))
    return out


def list_projects(args):
    """列 PROJECT_ROOT 项目 + project.json 摘要(只读)。"""
    root = project_root()
    out = []
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            pj = os.path.join(root, d, "project.json")
            if not os.path.exists(pj):
                continue
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            out.append({"project": d, "created": meta.get("created"),
                        "case_ids": meta.get("case_ids", []),
                        "case_count": len(meta.get("case_ids", []))})
    return {"project_root": root, "projects": out, "count": len(out),
            "_discipline": DISC_PROJ}


def task_status(args):
    """一屏任务态:intent 状态/门摘要/决策链尾 + status_bus 实时交叉(指针一致才并)。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_PROJ)
    intent, lerr = _load_intent(project, case_id)
    if lerr:
        return dict(lerr, _discipline=DISC_PROJ)
    try:
        n = max(1, min(int(args.get("n", 8)), 50))
    except (TypeError, ValueError):
        n = 8
    gr = intent.get("gate_report") or {}
    live = _live_case(case_id)
    return {"case_id": case_id, "project": project, "status": intent.get("status"),
            "revision": intent.get("revision", 0),
            "gate": {"passed": gr.get("passed"),
                     "checked_at": gr.get("checked_at"),
                     "hard": [v.get("gate") for v in gr.get("hard_violations") or []],
                     "soft": [w.get("gate") for w in gr.get("soft_warnings") or []]},
            "decisions": intent.get("decisions", [])[-n:],
            "live_run": live if live else "无对应活动 run(status_bus 无此 case)",
            "prod_dir_ref": intent.get("prod_dir_ref"),
            "next_step": _next_step(intent.get("status"), intent),
            "_discipline": DISC_PROJ}


def intent_view(args):
    """渲染人读方案说明书(C3 终审物料):字段表+四要素痕迹+决策链+下一步。"""
    project = str(args.get("project", "")).strip()
    case_id = str(args.get("case_id", "")).strip()
    err = _require_idents(("project", project), ("case_id", case_id))
    if err:
        return dict(err, _discipline=DISC_PROJ)
    intent, lerr = _load_intent(project, case_id)
    if lerr:
        return dict(lerr, _discipline=DISC_PROJ)
    L = ["# 仿真方案说明书 · case %s" % case_id,
         "", "状态: **%s**(revision %s)  任务形态: **%s**" % (
             intent.get("status"), intent.get("revision", 0),
             intent.get("task_grade") or "未判定"),
         "", "## 体系(system)"]
    sysd = intent.get("system") or {}
    for k in ("molecule", "target_elements", "size", "declared_net_charge", "packmol_input"):
        L.append("- %s: %s" % (k, sysd.get(k)))
    mm = intent.get("material_model") or {}
    if mm:
        L += ["", "## 材料模型(material_model,B 层)"]
        for k in ("class", "composition", "lattice", "supercell", "solid_solution",
                  "defects_microstructure", "surface", "construction",
                  "characterization_link"):
            if mm.get(k) is not None:
                L.append("- %s: %s" % (k, json.dumps(mm[k], ensure_ascii=False)))
    er = intent.get("experiment_reference") or {}
    if er:
        L += ["", "## 实验参照(experiment_reference,%s)" % er.get("kind")]
        for k in ("sources", "conditions", "measured", "characterization"):
            if er.get(k):
                L.append("- %s: %s" % (k, json.dumps(er[k], ensure_ascii=False)[:400]))
    ab = intent.get("abstraction") or {}
    if ab:
        L += ["", "## 抽象映射(abstraction,C 层)"]
        for mp in ab.get("mappings") or []:
            L.append("- 映射: %s → %s [%s] %s" % (
                mp.get("experiment"), mp.get("simulation"), mp.get("fidelity"),
                mp.get("formula") or ""))
        for sp in ab.get("simplifications") or []:
            L.append("- 简化: %s → %s (%s; 伤可比性=%s)" % (
                sp.get("aspect"), sp.get("simplified_to"), sp.get("expected_impact"),
                sp.get("compromises_comparability")))
        if ab.get("scale_bridging"):
            L.append("- 尺度桥接: %s" % json.dumps(ab["scale_bridging"], ensure_ascii=False))
    cm = intent.get("comparability") or {}
    if cm:
        L += ["", "## 对照协议(comparability,H/A 层)"]
        L.append("- 解释角色: %s" % cm.get("explanation_role"))
        for t in cm.get("targets") or []:
            L.append("- 目标[%s] %s: 锚点=%s 协议=%s 容差=%s 噪声=%s" % (
                t.get("correspondence"), t.get("property"),
                json.dumps(t.get("experimental_anchor"), ensure_ascii=False),
                t.get("comparison_protocol"), t.get("tolerance"), t.get("noise_floor")))
        if cm.get("explicitly_out_of_scope"):
            L.append("- 明确不做: %s" % ", ".join(map(str, cm["explicitly_out_of_scope"])))
    L += ["", "## 目标(objective)"]
    obj = intent.get("objective") or {}
    L.append("- simulation_goal: %s" % obj.get("simulation_goal"))
    L.append("- target_property: %s" % obj.get("target_property"))
    L += ["", "## 约束(constraints)"]
    con = intent.get("constraints") or {}
    for k in ("temperature_range", "declared_density_range", "protocol_flags",
              "walltime_hours", "cores"):
        L.append("- %s: %s" % (k, con.get(k)))
    L += ["", "## 系综序列(ensemble_sequence,H2/H3 真相源)"]
    for s in intent.get("ensemble_sequence") or []:
        L.append("- %s" % json.dumps(s, ensure_ascii=False))
    ed = intent.get("experiment_design") or {}
    if ed.get("setup") or ed.get("experiment_type"):
        L += ["", "## 实验设计(experiment_design,C2e 论证对象)"]
        L.append("- experiment_type: %s" % ed.get("experiment_type"))
        for k, v in sorted((ed.get("setup") or {}).items()):
            L.append("- setup.%s: %s" % (k, json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v))
        L.append("- design_rationale: %s" % ed.get("design_rationale"))
        plaus = _plausibility_of(intent)
        for e in plaus.get("evidence") or []:
            L.append("- 证据[%s]: %s (%s) %s" % (e.get("verdict"), e.get("field"),
                                                  e.get("literature_range"),
                                                  "; ".join(e.get("evidence") or [])))
        for a in plaus.get("unverified_assumptions") or []:
            L.append("- 存疑假设[%s]: %s (接受人: %s)" % (a.get("field"), a.get("assumption"),
                                                          a.get("accepted_by")))
    L += ["", "## 关键参数(key_params)"]
    kp = intent.get("key_params") or {}
    for k, v in sorted(kp.items()):
        L.append("- %s: %s" % (k, json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v))
    L += ["", "## 四要素确认痕迹(design)"]
    d = intent.get("design") or {}
    for f in DESIGN_FACETS:
        L.append("- %s: %s" % (f, (d.get(f) or {}).get("chosen_by", "未确认")))
    ex = intent.get("exemptions") or []
    if ex:
        L += ["", "## 声明式豁免"]
        for e in ex:
            L.append("- %s: %s" % (e.get("gate"), e.get("reason")))
    gr = intent.get("gate_report") or {}
    if gr:
        L += ["", "## 审阅报告摘要(AI 审阅+代码仲裁)",
              "- passed: %s  status: %s  checked_at: %s" % (
                  gr.get("passed"), gr.get("status"), gr.get("checked_at"))]
        if gr.get("verdict"):
            L.append("- verdict: %s" % gr["verdict"])
        for v in gr.get("hard_violations") or []:
            L.append("- [hard|%s] %s%s" % (v.get("gate"), v.get("title"),
                                           "(未核验)" if v.get("unverified") else ""))
        for w in gr.get("soft_warnings") or []:
            L.append("- [soft|%s] %s" % (w.get("gate"), w.get("title")))
        for cm in gr.get("completion_suggestions") or []:
            L.append("- [补全] %s: %s → %s" % (cm.get("aspect"), cm.get("why"), cm.get("proposal")))
        exp = (gr.get("experience") or {})
        if exp.get("used_ids"):
            L.append("- 引用经验: %s" % ", ".join(exp["used_ids"][:12]))
        L.append("- deferred_to_runtime: %s" % gr.get("deferred_to_runtime"))
    dec = intent.get("decisions") or []
    if dec:
        L += ["", "## 决策链(decisions,%d 条)" % len(dec)]
        for x in dec[-12:]:
            L.append("- [%s] %s %s" % (x.get("cp"), x.get("answer") or x.get("note") or
                                        x.get("confirm_quote") or "",
                                        x.get("fields_changed") or ""))
    L += ["", "## 下一步", _next_step(intent.get("status"), intent) or "-", ""]
    return {"case_id": case_id, "status": intent.get("status"),
            "render": "\n".join(L), "_discipline": DISC_PROJ}


# ---------- 工具注册表(面向模型的说明即流程约束) ----------
TOOLS = [
    {"name": "draft_intent",
     "description": "【前置双门·C0 前置必调】把对用户需求的理解落成 intent.json(DRAFT):"
                    "任何新仿真需求(自由文本/文献/参照case)必须先经本工具落白纸黑字,再谈方案。"
                    "intent 传结构化理解(system/objective/constraints/ensemble_sequence/key_params),"
                    "歧义点全部进 open_questions(带选项),不臆测。注入 status/prod_dir_ref/未知段一律丢弃。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名([A-Za-z0-9_-])"},
         "case_id": {"type": "string", "description": "语义 case 标识([A-Za-z0-9_-])"},
         "intent": {"type": "object", "description": "结构化意图(payload 段白名单过滤)"},
         "open_questions": {"type": "array", "description": "澄清问题[{id,question,options,answered}]"},
         "note": {"type": "string", "description": "起草说明(进决策链)"}
     }, "required": ["project", "case_id", "intent"], "additionalProperties": False}},
    {"name": "update_intent",
     "description": "【状态机写口】分段白名单更新意图(C1 物理框架/C2 四要素/C0 答疑/C4r 差异整改)。"
                    "section 仅限白名单段;patch 整段替换。跃迁自动判定:DRAFT→CLARIFIED 需 open_questions "
                    "全答,CLARIFIED→DESIGNED 需 design 四要素(system/forcefield/dynamics/resources)均含 "
                    "chosen_by;CONFIRMED/GATED_FAIL 后改动仅当 minor_revision=true 且 section 属 "
                    "gate_report 点名字段才保留 CONFIRMED,否则降回 DESIGNED(revision+1)。每次调用把 "
                    "cp/question_digest/answer 落 decisions 决策链。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "case_id": {"type": "string"},
         "section": {"type": "string", "description": "白名单段名(source/intake_summary/system/objective/constraints/ensemble_sequence/key_params/design/open_questions/exemptions)"},
         "patch": {"description": "该段新值(整段替换)。experiment_design 的 plausibility.evidence 禁止自填(只能走 design_evidence 参数);unverified_assumptions(用户显式接受)可写"},
         "cp": {"type": "string", "description": "检查点标记(C0/C1/C2a-e/C4r)"},
         "question_digest": {"type": "string"}, "answer": {"type": "string"},
         "minor_revision": {"type": "boolean", "description": "C4r 差异整改:仅违规点名字段合法"},
         "design_evidence": {"type": "array", "description": "C2e 外部论证入账:sim_design_research 返回提炼的逐字段判定 [{field,verdict,literature_range,evidence:[url]}](每条须带 http 证据 URL)"},
         "evidence_refs": {"type": "array", "items": {"type": "string"}, "description": "本次决策的证据出处 URL(留痕进 decisions)"}
     }, "required": ["project", "case_id", "section", "patch"], "additionalProperties": False}},
    {"name": "confirm_intent",
     "description": "【C3 方案终审·唯一 CONFIRMED 入口】仅 DESIGNED 可调;必须带用户批准原话 "
                    "confirm_quote(逐字摘录,进决策链与审计)。调用前应先 intent_view 渲染完整方案说明书"
                    "并向用户回显(含与文献/参照 case 的差异表)。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "case_id": {"type": "string"},
         "confirm_quote": {"type": "string", "description": "用户批准原话摘录"}
     }, "required": ["project", "case_id", "confirm_quote"], "additionalProperties": False}},
    {"name": "validate_intent",
     "description": "【功能二·物理一致性校核】仅 CONFIRMED 可调;确定性跑 physics_gate 全 PRE 门"
                    "(H1 步长/H2 裸NVE/H3 NPT硬压/H4 近距/H5 净电荷/C1 单位/C2 元素覆盖/C4 温度程序 + S/C 软门),"
                    "写 gate_report 并置 GATED_PASS/GATED_FAIL。hard_violations 的 cited_fields 是 C4r "
                    "整改的唯一合法改动域。H6/H7 运行期门延后给 sim_pipeline_check。"
                    "accepted_warnings 传 C5 处置留痕(接受警示的门 id)。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "case_id": {"type": "string"},
         "accepted_warnings": {"type": "array", "items": {"type": "string"}},
         "design_evidence": {"type": "array", "description": "C2e 外部论证补账(同 update_intent;E-VERIFY 整改通道) [{field,verdict,literature_range,evidence:[url]}]"},
         "data_file": {"type": "string", "description": "data 草案路径(缺省自动取 inputs/ 最新 *.data)"},
         "ffield_file": {"type": "string", "description": "力场文件路径(缺省按 forcefield.preferred 搜索)"}
     }, "required": ["project", "case_id"], "additionalProperties": False}},
    {"name": "new_project",
     "description": "建项目夹(PROJECT_ROOT 内 mkdir + project.json + 归纳说明自动生成)。"
                    "首个写必须在对话里回显目标路径并经用户确认(批量预览可合并,最终确认不可省)。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}}, "required": ["project"], "additionalProperties": False}},
    {"name": "new_task",
     "description": "建任务夹(run_folder=case_id+时间戳,{inputs,logs,results} + intent.json DRAFT 骨架"
                    " + 归纳说明 + 登记 project.json.case_ids)。run_folder 是本地标识,勿反推 prod_dir。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "case_id": {"type": "string"}},
         "required": ["project", "case_id"], "additionalProperties": False}},
    {"name": "list_projects",
     "description": "列 PROJECT_ROOT 下项目与 project.json 摘要(只读)。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "task_status",
     "description": "一屏任务态:intent 状态/revision/门摘要/决策链尾(默认8条)/status_bus 实时交叉"
                    "(指针一致才并,否则'无对应活动 run')/下一步检查点指引。"
                    "回答'这个 case 方案到哪了/还差什么确认'用它。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "case_id": {"type": "string"},
         "n": {"type": "integer", "description": "决策链尾条数(默认8,最大50)"}},
         "required": ["project", "case_id"], "additionalProperties": False}},
    {"name": "intent_view",
     "description": "渲染人读方案说明书(C3 终审物料):体系/目标/约束/系综序列/关键参数字段表"
                    "+四要素确认痕迹+豁免+门报告摘要+决策链+下一步指引。",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "case_id": {"type": "string"}},
         "required": ["project", "case_id"], "additionalProperties": False}},
]
HANDLERS = {
    "draft_intent": draft_intent,
    "update_intent": update_intent,
    "confirm_intent": confirm_intent,
    "validate_intent": validate_intent,
    "new_project": new_project,
    "new_task": new_task,
    "list_projects": list_projects,
    "task_status": task_status,
    "intent_view": intent_view,
}


# ---------- JSON-RPC over stdio(与 md_mcp 同构) ----------
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION_FALLBACK
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mdproj", "version": "0.1.0"}}})
    elif method in ("notifications/initialized", "initialized"):
        pass
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}})
            return
        try:
            data = fn(args)
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                "structuredContent": data}})
        except Exception as ex:
            log("tool error: " + traceback.format_exc())
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "工具执行出错: %s" % ex}], "isError": True}})
    else:
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "method not found: %s" % method}})


def main():
    log("mdproj MCP server 启动(受控写,PROJECT_ROOT=%s)" % project_root())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as ex:
            log("bad json: %s" % ex)
            continue
        try:
            handle(msg)
        except Exception:
            log("handle error: " + traceback.format_exc())


if __name__ == "__main__":
    main()
