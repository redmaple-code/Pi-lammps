# -*- coding: utf-8 -*-
"""
度量与结构层(physics_gate)—— 代码度量、AI 判断、经验供智
================================================================================
- 定位(2026-10-04 架构定稿):本模块【只测量,不判断】。物理对错由 design_review.py
  的 AI 审阅基于可注入经验库(review_experience/)推理;阈值判定不再存在于代码中
  ——后续海量经验注入只落经验库,永不改这里。
- 职责:
  ① 纯测量:parse_data_file / compute_dmin / parse_ffield_elements / structure_metrics;
  ② extract_facts:intent+ctx → 度量事实与衍生比值(dmin/电荷差/压深比/单步位移/
     盒截断比/系综序列结构/组分统计/最近邻指纹…),只算比值不给结论;
  ③ structural_scan:按可注入的完整性清单(family 或 match 路由)做存在性扫描;
  ④ effective_grade:任务形态分级(exploratory|paper_grade)判定——纯代码可判,
     键全来自 intent 字段与关键词。
- 纪律:纯函数、绝不抛、缺字段返回 null/False,不在本层产生任何"违规"概念。
"""
import math

DEFERRED_TO_RUNTIME = ["H6", "H7"]  # 运行期巡检(sim_pipeline_check)承担,报告仅注记

# paper_grade 触发关键词(A 层"解释角色"类声明;判定是代码事实,含义归 AI 审阅)
PAPER_GRADE_KEYWORDS = ("复现", "对照", "解释实验", "与实验", "实验一致", "机理解释",
                        "validate", "validation", "benchmark", "compare with experiment")


def _num(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


# ---------- LAMMPS data 解析(纯测量) ----------
def parse_data_file(path):
    """解析 LAMMPS data 文件 → {"coords", "charges", "types", "counts", "box"}。
    失败返回 None,绝不抛。Atoms 行布局(去掉 id 后的列数):
      4列 = id type x y z(atomic)或 id q x y z(charge,按 # 提示区分);
      5列 = id type q x y z(本工程主流 6 列形态);
      6列 = id mol type q x y z(full)。
    triclinic 盒(xy/xz/yz)→ box=None(不做周期修正)。"""
    import os
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    box, coords, charges, types, style_hint = [], [], [], [], None
    in_atoms = False
    for ln in lines:
        t = ln.strip()
        low = t.lower()
        if low.endswith("xlo xhi") or low.endswith("ylo yhi") or low.endswith("zlo zhi"):
            parts = t.split()
            if len(parts) == 4:
                box.append((_num(parts[0]), _num(parts[1])))
            continue
        if "xy xz yz" in low:
            box = []
            continue
        if t.startswith("Atoms"):
            style_hint = low.split("#", 1)[1].strip() if "#" in low else None
            in_atoms = True
            continue
        if in_atoms:
            if not t:
                if coords:
                    break
                continue
            parts = t.split()
            try:
                nums = [float(p) for p in parts[1:]]  # 去掉 id
            except ValueError:
                if coords:
                    break
                continue
            if len(nums) == 4:
                # charge 样式:id q x y z → q=nums[0];atomic 样式:id type x y z → type=nums[0]
                if style_hint == "charge":
                    q, typ = nums[0], None
                    x, y, z = nums[1], nums[2], nums[3]
                else:
                    q, typ = None, nums[0]
                    x, y, z = nums[1], nums[2], nums[3]
            elif len(nums) == 5:
                typ, q = nums[0], nums[1]
                x, y, z = nums[2], nums[3], nums[4]
            else:  # full: id mol type q x y z
                typ, q = nums[1], nums[2]
                x, y, z = nums[3], nums[4], nums[5]
            coords.append((x, y, z))
            charges.append(q)
            types.append(typ)
    if not coords:
        return None
    counts = {}
    for typ in types:
        if typ is not None:
            counts[typ] = counts.get(typ, 0) + 1
    return {"coords": coords, "charges": charges, "types": types,
            "counts": counts or None,
            "box": box if len(box) == 3 else None}


def compute_dmin(parsed):
    """最小成像距离(正交盒)。N>3000 用 numpy 分块,无 numpy 抽样 3000。
    返回 (dmin_Å, note) 或 (None, 原因)。纯测量。"""
    coords, box = parsed["coords"], parsed["box"]
    n = len(coords)
    if n < 2:
        return None, "原子数<2"

    def _mic(ax, ay, az, bx, by, bz):
        dx, dy, dz = ax - bx, ay - by, az - bz
        if box:
            (xlo, xhi), (ylo, yhi), (zlo, zhi) = box
            lx, ly, lz = xhi - xlo, yhi - ylo, zhi - zlo
            if lx > 0 and ly > 0 and lz > 0:
                dx -= lx * round(dx / lx)
                dy -= ly * round(dy / ly)
                dz -= lz * round(dz / lz)
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    note = ""
    if n > 3000:
        try:
            import numpy as np
            pts = np.asarray(coords, dtype=float)
            best = float("inf")
            L = np.array([box[i][1] - box[i][0] for i in range(3)]) if box else None
            for s in range(0, n, 512):
                blk = pts[s:s + 512]
                for t in range(s + 512, n, 512):
                    d = pts[t:t + 512][None, :, :] - blk[:, None, :]
                    if L is not None:
                        d -= L * np.round(d / L)
                    best = min(best, float(np.sqrt((d * d).sum(-1)).min()))
                d = blk[:, None, :] - blk[None, :, :]
                if L is not None:
                    d -= L * np.round(d / L)
                dm = np.sqrt((d * d).sum(-1))
                dm[np.arange(len(blk)), np.arange(len(blk))] = float("inf")
                best = min(best, float(dm.min()))
            return best, note
        except ImportError:
            coords = coords[:3000]
            note = "体系过大且无 numpy,抽样前 3000 原子"
    best = float("inf")
    for i in range(len(coords)):
        xi, yi, zi = coords[i]
        for j in range(i + 1, len(coords)):
            d = _mic(xi, yi, zi, *coords[j])
            if d < best:
                best = d
    return (best if math.isfinite(best) else None), note


def structure_metrics(parsed, shell_max=4.0):
    """结构指纹(纯测量):同类原子最近邻距离(第一壳层)统计 + 每 type 的最近邻数分布。
    返回 {"nn_dist": {type: {"mean","min","max","n"}}, "coord_numbers": {type: {n:count}}}
    或 None(原子太少/无类型)。只测量,不判结构对错(判断归 AI+经验)。"""
    coords, types, box = parsed["coords"], parsed.get("types"), parsed["box"]
    n = len(coords)
    if n < 3 or not types:
        return None

    def _mic(a, b):
        dx, dy, dz = a[0] - b[0], a[1] - b[1], a[2] - b[2]
        if box:
            (xlo, xhi), (ylo, yhi), (zlo, zhi) = box
            lx, ly, lz = xhi - xlo, yhi - ylo, zhi - zlo
            if lx > 0 and ly > 0 and lz > 0:
                dx -= lx * round(dx / lx)
                dy -= ly * round(dy / ly)
                dz -= lz * round(dz / lz)
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    sample = list(range(n)) if n <= 800 else list(range(0, n, max(1, n // 800)))
    nn = {}
    for i in sample:
        ti = types[i]
        best, cnt = float("inf"), 0
        for j in range(n):
            if j == i:
                continue
            d = _mic(coords[i], coords[j])
            if d < best:
                best = d
            if types[j] == ti and d <= shell_max:
                cnt += 1
        if ti is not None and math.isfinite(best):
            row = nn.setdefault(ti, {"sum": 0.0, "min": float("inf"),
                                     "max": 0.0, "n": 0, "cn": {}})
            row["sum"] += best
            row["min"] = min(row["min"], best)
            row["max"] = max(row["max"], best)
            row["n"] += 1
            row["cn"][cnt] = row["cn"].get(cnt, 0) + 1
    if not nn:
        return None
    out_nn, out_cn = {}, {}
    for ti, row in nn.items():
        out_nn[str(ti)] = {"mean": round(row["sum"] / row["n"], 3),
                           "min": round(row["min"], 3), "max": round(row["max"], 3),
                           "n": row["n"]}
        out_cn[str(ti)] = dict(sorted(row["cn"].items(), key=lambda kv: -kv[1])[:6])
    return {"nn_dist": out_nn, "coord_numbers": out_cn, "sampled": len(sample)}


def composition_stats(parsed):
    """组分统计(纯测量):type 计数与占比。返回 {"counts": {type:n}, "fractions": {type:p}}。"""
    counts = parsed.get("counts")
    total = sum(counts.values()) if counts else 0
    if not counts or not total:
        return None
    return {"counts": {str(k): v for k, v in counts.items()},
            "fractions": {str(k): round(v / total, 4) for k, v in counts.items()}}


def parse_ffield_elements(path):
    """解析 ReaxFF ffield 头部元素表 → [符号大写] 或 None。纯测量。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f.readlines()]
    except OSError:
        return None
    for i, ln in enumerate(lines):
        parts = ln.split()
        if not parts:
            continue
        if parts[0].isdigit() and 1 <= int(parts[0]) <= 200:
            cnt = int(parts[0])
            syms = [p for p in parts[1:] if not p.replace(".", "").isdigit()]
            if syms:
                return [s.strip("!").upper() for s in syms[:cnt]]
            out = []
            for j in range(i + 1, min(i + 1 + cnt, len(lines))):
                tok = lines[j].split()
                if tok and not tok[0].isdigit():
                    out.append(tok[0].strip("!").upper())
            if len(out) == cnt:
                return out
    return None


# ---------- 事实提取:度量+衍生比值(只算,不判) ----------
_THERMOSTATS = ("nvt", "langevin", "rescale", "berendsen", "nh", "csld")


def _stages(intent):
    seq = (intent or {}).get("ensemble_sequence")
    return [s for s in seq if isinstance(s, dict)] if isinstance(seq, list) else []


def _setup(intent):
    ed = (intent or {}).get("experiment_design")
    s = ed.get("setup") if isinstance(ed, dict) else None
    return s if isinstance(s, dict) else {}


def _setup_num(setup, path, default=None):
    d = setup
    for k in path.split("."):
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return _num(d, default)


def _sample_thickness(setup):
    thick = _setup_num(setup, "sample.thickness_A")
    if thick is not None:
        return thick
    sample = setup.get("sample")
    if isinstance(sample, dict):
        dims = sample.get("dimensions_A")
        if isinstance(dims, list) and len(dims) == 3:
            return _num(dims[2])
    return None


def _box_l(setup):
    box = _setup_num(setup, "box_l_A")
    if box is not None:
        return box
    sample = setup.get("sample")
    if isinstance(sample, dict):
        dims = sample.get("dimensions_A")
        if isinstance(dims, list) and dims:
            vals = [_num(d) for d in dims]
            vals = [v for v in vals if v is not None]
            if vals:
                return max(vals)
    return None


def _sec(intent, key):
    """段读取防护:自由文本段(string)或缺失一律按空 dict 处理——
    分级判定只关心结构化声明,不因方案里段落填成字符串而炸(2026-10-23,
    webui 示例数据暴露:update_intent 无防护直调本函数,string 段必崩)。"""
    v = intent.get(key)
    return v if isinstance(v, dict) else {}


def extract_facts(intent, ctx=None):
    """intent+ctx → 度量事实。纯测量:任何字段缺失→null/False,绝不产生"违规"。"""
    intent = intent if isinstance(intent, dict) else {}
    ctx = dict(ctx) if isinstance(ctx, dict) else {}
    if ctx.get("data_file") and "data_parsed" not in ctx:
        pp = parse_data_file(ctx["data_file"])
        if pp:
            ctx["data_parsed"] = pp
    if ctx.get("ffield_file") and "ffield_elements" not in ctx:
        ctx["ffield_elements"] = parse_ffield_elements(ctx["ffield_file"])

    facts = {}

    # data 度量
    parsed = ctx.get("data_parsed")
    if parsed:
        dmin, note = compute_dmin(parsed)
        qs = [q for q in parsed["charges"] if q is not None]
        facts["dmin_A"] = dmin
        facts["dmin_note"] = note or None
        if len(qs) == len(parsed["charges"]) and qs:
            facts["charge_sum"] = sum(qs)
        facts["atom_count"] = len(parsed["coords"])
        facts["composition"] = composition_stats(parsed)
        facts["structure"] = structure_metrics(parsed)  # 最近邻/配位数指纹(B 层度量)
    declared = _num(_sec(intent, "system").get("declared_net_charge"))
    if facts.get("charge_sum") is not None and declared is not None:
        facts["charge_delta"] = abs(facts["charge_sum"] - declared)

    # 声明组分 vs 实测组分核对(B 层度量:只报差异,不判对错)
    mm = _sec(intent, "material_model")
    declared_comp = mm.get("composition")
    if isinstance(declared_comp, dict) and facts.get("composition"):
        real = facts["composition"]["fractions"]
        diff = {}
        for k, v in declared_comp.items():
            if not isinstance(v, (int, float)):
                continue
            rv = real.get(str(k))
            if rv is not None:
                diff[str(k)] = round(float(v) - float(rv), 4)
        if diff:
            facts["composition_deviation"] = diff

    # 力场元素
    elems = ctx.get("ffield_elements")
    targets = _sec(intent, "system").get("target_elements") or []
    if isinstance(elems, list):
        facts["ffield_elements"] = elems
        if targets:
            facts["missing_elements"] = [str(e) for e in targets
                                         if str(e).strip().upper()
                                         not in [str(x).upper() for x in elems]]
    facts["schedule"] = ctx.get("schedule")

    # 系综序列结构(结构事实,非判断)
    seq = _stages(intent)
    facts["seq_n_stages"] = len(seq)
    facts["has_minimize"] = any(str(s.get("stage", "")).startswith("minim") or s.get("soft")
                               for s in seq)
    dyn_idx = [i for i, s in enumerate(seq) if s.get("ensemble")]
    facts["first_dyn_ensemble"] = (str(seq[dyn_idx[0]].get("ensemble")).lower()
                                   if dyn_idx else None)
    if dyn_idx:
        facts["relax_before_first_dyn"] = any(
            str(s.get("stage", "")).startswith("minim")
            or str(s.get("ensemble", "")).lower() in _THERMOSTATS
            or s.get("thermostat")
            for s in seq[:dyn_idx[0]])
    else:
        facts["relax_before_first_dyn"] = False
    facts["any_nve"] = any(str(s.get("ensemble", "")).lower() == "nve" for s in seq)
    facts["nve_unrelaxed"] = False
    for i, s in enumerate(seq):
        if str(s.get("ensemble", "")).lower() == "nve":
            relaxed = any(str(x.get("stage", "")).startswith("minim")
                          or str(x.get("ensemble", "")).lower() in _THERMOSTATS
                          or x.get("thermostat")
                          for x in seq[:i])
            if not relaxed:
                facts["nve_unrelaxed"] = True
    facts["continuation"] = bool(_sec(intent, "source").get("base_case_id")) and any(
        s.get("restart") or s.get("read_restart") for s in seq)

    # 实验设计衍生比值(只算比,不判)
    setup = _setup(intent)
    ed = intent.get("experiment_design")
    facts["experiment_type"] = (ed.get("experiment_type") if isinstance(ed, dict) else None)
    if setup:
        cutoff = _setup_num(setup, "pair_cutoff_A")  # 缺省值属经验,留给 AI;此处仅记录显式值
        box = _box_l(setup)
        if cutoff and box:
            facts["box_over_cutoff"] = box / cutoff
        v = _setup_num(setup, "sliding_velocity_m_s")
        dt = _num(_sec(intent, "key_params").get("timestep_fs"))
        if v is not None and dt:
            facts["disp_per_step_A"] = v * dt * 1e-5  # 1 m/s = 1e-5 Å/fs
        depth = _setup_num(setup, "indentation_depth_A")
        thick = _sample_thickness(setup)
        if depth and thick:
            facts["depth_over_thickness"] = depth / thick
        r = _setup_num(setup, "indenter.radius_A")
        if depth and r:
            facts["depth_over_radius"] = depth / r
        L = _setup_num(setup, "sliding_length_A")
        if L is not None and box is not None:
            facts["sliding_room_A"] = box - (2 * r if r else 0.0)
    return facts


def effective_grade(intent):
    """任务形态分级判定(2026-10-04,纯代码):返回 (grade, reasons)。
    paper_grade 触发(任一):source.kind=paper / experiment_reference.kind 非 none /
    comparability.targets 非空 / objective 含对照解释类关键词 / task_grade 显式填 paper_grade。
    exploratory 一旦填 experiment_reference 或 comparability → 即时升 paper_grade(升级由
    mdproj update_intent 调用本函数落盘,不靠提示词自觉)。"""
    intent = intent if isinstance(intent, dict) else {}
    reasons = []
    if str(_sec(intent, "source").get("kind") or "") == "paper":
        reasons.append("source.kind=paper")
    er = _sec(intent, "experiment_reference")
    if er and str(er.get("kind") or "none") != "none":
        reasons.append("experiment_reference.kind=%s" % er.get("kind"))
    cmp_ = _sec(intent, "comparability")
    if isinstance(cmp_.get("targets"), list) and cmp_["targets"]:
        reasons.append("comparability.targets 非空(%d 项)" % len(cmp_["targets"]))
    goal = str(_sec(intent, "objective").get("simulation_goal") or "").lower()
    for kw in PAPER_GRADE_KEYWORDS:
        if kw in goal:
            reasons.append("objective 含关键词:%s" % kw)
            break
    if str(intent.get("task_grade") or "") == "paper_grade" and not reasons:
        reasons.append("task_grade 显式声明")
    return ("paper_grade" if reasons else "exploratory"), reasons


# ---------- 结构扫描:按完整性清单查字段存在性(存在性是事实) ----------
def _path_exists(obj, path):
    cur = obj
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def structural_scan(intent, checklists, grade=None):
    """按完整性清单扫描缺失字段。清单路由(2026-10-04 升级):
    - 旧形态 {"family": "friction"}:experiment_type 子串匹配(* 通配);
    - 新形态 {"match": {"experiment_type": "friction", "material_class": "solid_solution",
                        "task_grade": "paper_grade"}}:任一命中生效(键缺=该键不参与匹配,
    "*" 通配);min_grade="paper_grade" 类清单只在 paper_grade 任务生效。
    返回 [{"path","family"}]——缺口=事实,交 design_review 确定性合并。
    grade 为 None 时自算(physics_gate.effective_grade)。"""
    intent = intent if isinstance(intent, dict) else {}
    if grade is None:
        grade = effective_grade(intent)[0]
    et = str(_sec(intent, "experiment_design").get("experiment_type") or "").lower()
    mclass = str(_sec(intent, "material_model").get("class") or "").lower()
    gaps = []
    for cl in checklists or []:
        if not isinstance(cl, dict):
            continue
        fam = str(cl.get("family") or "*")
        match = cl.get("match")
        applies = False
        if isinstance(match, dict):
            hit = False
            for key, want in match.items():
                want = str(want or "")
                if want == "*" or not want:
                    continue
                val = {"experiment_type": et, "material_class": mclass,
                       "task_grade": grade}.get(key, "")
                if want.lower() in val:
                    hit = True
                    break
            applies = hit
        else:
            applies = (fam == "*") or (fam.lower() in et)
        if cl.get("min_grade") == "paper_grade" and grade != "paper_grade":
            applies = False
        if not applies:
            continue
        for path in cl.get("required_fields") or []:
            if not _path_exists(intent, str(path)):
                gaps.append({"path": str(path), "family": fam})
    seen, out = set(), []
    for g in gaps:
        if g["path"] not in seen:
            seen.add(g["path"])
            out.append(g)
    return out
