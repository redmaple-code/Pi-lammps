# -*- coding: utf-8 -*-
"""GROMACS 运行扫描器(2026-09-10):看板(web_monitor)与 MCP 工具
(sim_gmx_status)共享的唯一实现。扫描根走 dashboard/monitor_roots.json
(换机只改该文件,不改代码;env MONITOR_ROOTS_FILE 可指别处)。
配置缺席/损坏 → 空列表,fail-open 不抛。"""
import json
import math
import os
import re
import time

_GMX_STAGE_LOGS = ("prod.log", "md.log", "npt.log", "nvt.log", "min.log")
_GMX_BUILD_MARKERS = ("packed.xyz", "packmol.log", "packmol.out")


def parse_gro_stats(gro_path):
    """.gro 统计:第 2 行原子数,末行盒子(nm,三斜取前 3 个对角元)。
    尽力而为,缺/坏 → {}。"""
    out = {}
    try:
        with open(gro_path, encoding="utf-8", errors="replace") as f:
            f.readline()  # title
            out["natoms"] = int(f.readline().strip())
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256))
            tail = f.read().strip().splitlines()
        box = [float(x) for x in tail[-1].split()[:3]]
        if box and all(math.isfinite(x) for x in box):
            out["box_nm"] = [round(x, 3) for x in box]
    except Exception:
        pass
    return out


def parse_top_counts(top_path, _seen=None, _depth=0):
    """topol.top + #include 链(递归,深度≤8):累计 [ bonds ]/[ angles ]/
    [ dihedrals ] 条目数(; 注释与空行剔除)。文件缺 → None。"""
    if _seen is None:
        _seen = set()
    if _depth > 8:
        return {"bonds": 0, "angles": 0, "dihedrals": 0}
    rp = os.path.realpath(top_path)
    if rp in _seen or not os.path.isfile(rp):
        return None if _depth == 0 else {"bonds": 0, "angles": 0, "dihedrals": 0}
    _seen.add(rp)
    counts = {"bonds": 0, "angles": 0, "dihedrals": 0}
    section = None
    base = os.path.dirname(rp)
    try:
        with open(rp, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.split(";", 1)[0].strip()
                if not ln:
                    continue
                if ln.startswith("#include"):
                    m = re.search(r'["<]([^">]+)[">]', ln)
                    if m:
                        sub = parse_top_counts(os.path.join(base, m.group(1)),
                                               _seen, _depth + 1)
                        if sub:
                            for k in counts:
                                counts[k] += sub[k]
                    continue
                if ln.startswith("["):
                    section = ln.strip("[] ").strip().lower()
                    continue
                if section in counts:
                    counts[section] += 1
    except Exception:
        return None if _depth == 0 else counts
    return counts


def model_stats(dirpath, filenames):
    """建模产物统计:最新 .gro 的原子数/盒子 + 拓扑键/角/二面角计数 +
    model_spec.json 对照值(缺则 None)。无任何产物 → None。"""
    files = set(filenames)
    gros = [f for f in files if f.endswith(".gro")]
    tops = [f for f in files if f.endswith(".top")]
    out = {}
    if gros:
        newest = max(gros, key=lambda f: os.path.getmtime(os.path.join(dirpath, f)))
        out.update(parse_gro_stats(os.path.join(dirpath, newest)))
        out["gro_file"] = newest
    if tops:
        newest_top = max(tops, key=lambda f: os.path.getmtime(os.path.join(dirpath, f)))
        c = parse_top_counts(os.path.join(dirpath, newest_top))
        if c:
            out.update(c)
        out["top_file"] = newest_top
    spec_path = os.path.join(dirpath, "model_spec.json")
    if os.path.isfile(spec_path):
        try:
            with open(spec_path, encoding="utf-8") as f:
                out["spec"] = json.load(f)
        except Exception:
            out["spec"] = None
    return out or None


def _gmx_stage_hit(dirpath, filenames):
    """阶段日志命中:[(stage, filename)],按 _GMX_STAGE_LOGS 优先级排序。
    精确名(prod.log)优先;无精确名时认自定义前缀的后缀匹配(2026-09-10
    paper-07 实例:s8prod.log/vmin.log),同级多候选取 mtime 最新(当前在跑的)。
    #x.log.1# 备份不以 .log 结尾,天然排除。"""
    files = set(filenames)
    hit = [(s.split('.')[0], s) for s in _GMX_STAGE_LOGS if s in files]
    if hit:
        return hit
    out = []
    for s in _GMX_STAGE_LOGS:
        cands = [f for f in files if f != s and f.endswith(s)]
        if cands:
            out.append((s.split('.')[0], max(
                cands, key=lambda f: os.path.getmtime(os.path.join(dirpath, f)))))
    return out


def _gmx_pipeline(files):
    """建模流水线徽章:建模(.gro/packmol 产物)→grompp(.tpr/grompp 日志)
    →min→平衡(nvt/npt/md)→prod。阶段档后缀匹配(自定义前缀同认)。"""
    sfx = lambda s: any(f.endswith(s) for f in files)
    return {
        "build": bool(files & set(_GMX_BUILD_MARKERS)) or \
            any(f.endswith(".gro") for f in files),
        "grompp": any(f.endswith(".tpr") for f in files) or \
            any(f.startswith("grompp") and f.endswith(".log") for f in files),
        "min": sfx("min.log"),
        "eq": sfx("nvt.log") or sfx("npt.log") or sfx("md.log"),
        "prod": sfx("prod.log"),
    }


def get_gmx_build_pids(run_dir):
    """建模进程判活:cmdline 含 packmol 或 (gmx 且 grompp),cwd 匹配。
    只展示,不在野跑执法范围(短任务,执法只针对 mdrun)。"""
    pids = []
    target = os.path.realpath(run_dir)
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid_dir, 'rb') as f:
                    cmd = f.read().replace(b'\x00', b' ')
                if b"packmol" in cmd or (b"gmx" in cmd and b"grompp" in cmd):
                    if os.path.realpath(os.readlink("/proc/%s/cwd" % pid_dir)) == target:
                        pids.append(int(pid_dir))
            except (PermissionError, FileNotFoundError, OSError):
                pass
    except Exception:
        pass
    return pids


def monitor_roots():
    """读 monitor_roots.json:[{path,type,label,enabled}];缺席/损坏 → []。
    path 支持 ~ 展开;env MONITOR_ROOTS_FILE 覆盖文件位置。"""
    cfg = os.environ.get("MONITOR_ROOTS_FILE") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_roots.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    roots = []
    for r in (data.get("roots") or []) if isinstance(data, dict) else []:
        if not isinstance(r, dict) or not r.get("enabled", True):
            continue
        p = os.path.expanduser(str(r.get("path", "")).strip())
        if p and os.path.isdir(p):
            roots.append({"path": p, "type": str(r.get("type", "gromacs")),
                          "label": str(r.get("label", ""))})
    return roots


def parse_gmx_log(log_path):
    """解析 GROMACS mdrun 日志尾部:最后一个 Step/Time 块 + 能量表的
    Temperature/Total Energy。缺块/非有限值 → 尽力返回(字段可缺)。"""
    out = {}
    if not log_path or not os.path.isfile(log_path):
        return out
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            buf = f.read().decode('utf-8', errors='ignore')
        m = None
        for m in re.finditer(r'Step\s+Time[^\n]*\n\s*(\d+)\s+([\d.eE+-]+)', buf):
            pass  # 取最后一个匹配
        if m:
            out['step'] = int(m.group(1))
            out['time_ps'] = float(m.group(2))
        # 能量表:列头/数据均为 15 字符定宽列;2018.1 起按 5 列一组换行
        # (Temperature 与 Kinetic En. 可能不在同一行组,旧单行正则抓不到),
        # 按列名索引从下一行同位取值,缓冲尾最后一次覆盖,两种排版通吃。
        lines = buf.splitlines()
        for i in range(len(lines) - 1):
            ln = lines[i]
            if ('Temperature' not in ln and 'Total Energy' not in ln
                    and 'Kinetic En.' not in ln):
                continue
            names = [ln[j:j + 15].strip() for j in range(0, len(ln), 15)]
            row = [lines[i + 1][j:j + 15].strip()
                   for j in range(0, len(lines[i + 1]), 15)]
            names = [n for n in names if n]
            try:
                vals = [float(x) for x in row if x]
            except ValueError:
                continue
            if len(names) != len(vals):
                continue
            for want, key in (('Kinetic En.', 'ke'), ('Total Energy', 'etotal'),
                              ('Temperature', 'temp'), ('Pressure (bar)', 'press')):
                if want in names:
                    v = vals[names.index(want)]
                    if math.isfinite(v):
                        out[key] = v
    except Exception:
        pass
    return out


def _gmx_total_steps(run_dir, log_path=None):
    """总步数:<x>_stdout.log 头 'N steps,  M ps.' 优先;阶段日志自带
    Input Parameters nsteps(反映实际在跑的 tpr,自定义前缀命名也能中)次之;
    prod.mdp nsteps 兜底。"""
    try:
        for name in os.listdir(run_dir):
            if not name.endswith("_stdout.log"):
                continue
            with open(os.path.join(run_dir, name), 'rb') as f:
                head = f.read(8192).decode('utf-8', errors='ignore')
            m = re.search(r'(\d+)\s+steps,\s*([\d.]+)\s*ps', head)
            if m:
                return int(m.group(1)), float(m.group(2))
    except Exception:
        pass
    if log_path and os.path.isfile(log_path):
        try:
            with open(log_path, 'rb') as f:
                head = f.read(16384).decode('utf-8', errors='ignore')
            m = re.search(r'^\s*nsteps\s*=\s*(\d+)', head, re.M)
            if m:
                return int(m.group(1)), None
        except Exception:
            pass
    for mdp in ("prod.mdp", "md.mdp"):
        p = os.path.join(run_dir, mdp)
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8', errors='replace') as f:
                    m = re.search(r'^\s*nsteps\s*=\s*(\d+)', f.read(), re.M)
                if m:
                    return int(m.group(1)), None
            except Exception:
                pass
    return None, None


def get_gmx_pids(run_dir):
    """/proc 判活:cmdline 含 gmx+mdrun 且 cwd == run_dir(与 get_lmp_pids 同纪律)。"""
    pids = []
    target = os.path.realpath(run_dir)
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid_dir, 'rb') as f:
                    cmd = f.read().replace(b'\x00', b' ')
                if b"gmx" not in cmd or b"mdrun" not in cmd:
                    continue
                if os.path.realpath(os.readlink("/proc/%s/cwd" % pid_dir)) == target:
                    pids.append(int(pid_dir))
            except (PermissionError, FileNotFoundError, OSError):
                pass
    except Exception:
        pass
    return pids


def _adopted_file():
    return os.environ.get("GMX_ADOPTED_FILE") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmx_adopted.json")


def adopt(run_dir, auto_kill_wild=None):
    """把 run 目录写入接管注册表(启动即接管的唯一代码入口;tmp+os.replace
    原子写,崩溃不撕裂)。auto_kill_wild 非 None 时一并设置开关。返回表内路径集。"""
    cfg = _adopted_file()
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    paths = [str(p) for p in (data.get("adopted") or []) if isinstance(p, str)]
    rp = os.path.realpath(run_dir)
    if rp not in [os.path.realpath(p) for p in paths if p.strip()]:
        paths.append(rp)
    data["adopted"] = paths
    if auto_kill_wild is not None:
        data["auto_kill_wild"] = bool(auto_kill_wild)
    tmp = cfg + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg)
    return set(paths)


def load_adopted():
    """接管注册表 gmx_adopted.json:{"adopted":[run 目录绝对路径,...]}。
    缺席/损坏 → 空集(宁多报不漏野跑——"不走编排器不能跑"2026-09-10 用户拍板)。
    env GMX_ADOPTED_FILE 覆盖文件位置。"""
    try:
        with open(_adopted_file(), encoding="utf-8") as f:
            data = json.load(f)
        return set(os.path.realpath(str(p)) for p in (data.get("adopted") or [])
                   if isinstance(p, str) and p.strip())
    except Exception:
        return set()


def wild_auto_kill():
    """gmx_adopted.json 的 auto_kill_wild 开关(默认 False):开则推送驱动发现
    野跑直接 SIGTERM 停掉(2026-09-10 用户拍板"gromacs 有 .cpt 可续跑,野跑直接停")。"""
    try:
        with open(_adopted_file(), encoding="utf-8") as f:
            return bool(json.load(f).get("auto_kill_wild"))
    except Exception:
        return False


def gmx_run_detail(dir_path):
    """单个 run 的点开详情(只读,2026-09-10 用户:"不能只显示,绑定和 lammps
    一样的功能")。dir 必须落在某个 enabled gromacs 根下(realpath 前缀校验,
    防路径逃逸);越根/不存在/不可读 → None。log_tail 取当前阶段日志尾 8KB;
    files 为模拟相关文件清单(新→旧,≤60)。"""
    rp = os.path.realpath(os.path.expanduser(str(dir_path or "").strip()))
    under = False
    for root in monitor_roots():
        if root["type"] != "gromacs":
            continue
        base = os.path.realpath(root["path"])
        if rp == base or rp.startswith(base + os.sep):
            under = True
            break
    if not under or not os.path.isdir(rp):
        return None
    try:
        filenames = os.listdir(rp)
    except Exception:
        return None
    hit = _gmx_stage_hit(rp, filenames)
    stage = hit[0][0] if hit else "build"
    log_path = os.path.join(rp, hit[0][1]) if hit else None
    info = parse_gmx_log(log_path) if log_path else {}
    total_steps, _total_ps = _gmx_total_steps(rp, log_path)
    pids = get_gmx_pids(rp)
    build_pids = get_gmx_build_pids(rp)
    step = info.get('step')
    log_tail = ""
    if log_path:
        try:
            with open(log_path, 'rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 8192))
                log_tail = f.read().decode('utf-8', errors='replace')
        except Exception:
            pass
    keep = (".gro", ".top", ".itp", ".mdp", ".tpr", ".cpt", ".xtc", ".trr",
            ".edr", ".xvg", ".ndx", ".xyz", ".log", ".out", ".json")
    files = []
    for name in filenames:
        if not name.endswith(keep):
            continue
        fp = os.path.join(rp, name)
        if not os.path.isfile(fp):
            continue
        try:
            st = os.stat(fp)
        except Exception:
            continue
        files.append((st.st_mtime, {"name": name, "size": st.st_size,
                                    "mtime": time.strftime(
                                        "%m-%d %H:%M:%S", time.localtime(st.st_mtime))}))
    files.sort(key=lambda x: x[0], reverse=True)
    return {
        "dir": rp,
        "stage": stage,
        "pipeline": _gmx_pipeline(set(filenames)),
        "model": model_stats(rp, filenames),
        "step": step,
        "time_ns": (round(info['time_ps'] / 1000.0, 2)
                    if info.get('time_ps') is not None else None),
        "total_steps": total_steps,
        "pct": (round(100.0 * step / total_steps, 1)
                if step is not None and total_steps else None),
        "temp": (round(info['temp'], 1)
                 if info.get('temp') is not None else None),
        "etotal": (round(info['etotal'], 1)
                   if info.get('etotal') is not None else None),
        "ke": (round(info['ke'], 1) if info.get('ke') is not None else None),
        "press": (round(info['press'], 2)
                  if info.get('press') is not None else None),
        "alive": bool(pids or build_pids),
        "mdrun_alive": bool(pids),
        "pids_count": len(pids) + len(build_pids),
        "adopted": rp in load_adopted(),
        "log_name": hit[0][1] if hit else None,
        "log_tail": log_tail,
        "files": [f for _mt, f in files[:60]],
    }


def scan_gromacs_runs():
    """扫 monitor_roots 里 type=gromacs 的根(深度 ≤3):run 目录 = 含任一
    阶段日志(精确名或自定义前缀后缀)的目录。返回活跃优先、mtime 倒序的条目。
    adopted=False 且 alive 的即野跑(未经编排器接管)。"""
    adopted_set = load_adopted()
    runs = []
    for root in monitor_roots():
        if root["type"] != "gromacs":
            continue
        base = root["path"]
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                depth = os.path.relpath(dirpath, base).count(os.sep)
                if depth >= 3:  # 三级封顶(论文根 paper-XX/gromacs_full/build/base 要三级)
                    dirnames[:] = []
                hit = _gmx_stage_hit(dirpath, filenames)
                pipeline = _gmx_pipeline(set(filenames))
                if not hit and not any(pipeline.values()):
                    continue  # 既无阶段日志也无建模产物,不是 run 目录
                dirnames[:] = []  # run 目录内不再嵌套
                stage = hit[0][0] if hit else "build"
                log_path = os.path.join(dirpath, hit[0][1]) if hit else None
                info = parse_gmx_log(log_path) if log_path else {}
                total_steps, _total_ps = _gmx_total_steps(dirpath, log_path)
                pids = get_gmx_pids(dirpath)          # mdrun(执法对象)
                build_pids = get_gmx_build_pids(dirpath)  # packmol/grompp(只展示)
                step = info.get('step')
                # 最近变动:阶段日志(含自定义前缀后缀)与建模产物里最新的 mtime
                mt_candidates = [os.path.join(dirpath, f) for f in filenames
                                 if any(f.endswith(s) for s in _GMX_STAGE_LOGS)
                                 or f in _GMX_BUILD_MARKERS
                                 or f.endswith(
                                     (".gro", ".top", ".itp", ".mdp", ".xyz"))]
                if log_path:
                    mt_candidates.append(log_path)
                mtime = max((os.path.getmtime(p) for p in mt_candidates
                             if os.path.isfile(p)), default=None)
                if mtime is None:
                    continue
                runs.append({
                    'run_id': os.path.relpath(dirpath, base),
                    'root_label': root["label"],
                    'dir': dirpath,
                    'stage': stage,
                    'pipeline': pipeline,
                    'model': model_stats(dirpath, filenames),
                    'step': step,
                    'time_ns': (round(info['time_ps'] / 1000.0, 2)
                                if info.get('time_ps') is not None else None),
                    'total_steps': total_steps,
                    'pct': (round(100.0 * step / total_steps, 1)
                            if step is not None and total_steps else None),
                    'temp': (round(info['temp'], 1)
                             if info.get('temp') is not None else None),
                    'etotal': (round(info['etotal'], 1)
                               if info.get('etotal') is not None else None),
                    'alive': bool(pids or build_pids),
                    'mdrun_alive': bool(pids),
                    'pids_count': len(pids) + len(build_pids),
                    'adopted': os.path.realpath(dirpath) in adopted_set,
                    '_mtime': mtime,
                    'last_modified': time.strftime(
                        '%m-%d %H:%M:%S', time.localtime(mtime)),
                })
        except Exception:
            continue
    # 稳定排序两级:先 mtime 倒序,再活跃提前
    runs.sort(key=lambda r: r['_mtime'], reverse=True)
    runs.sort(key=lambda r: not r['alive'])
    for r in runs:
        r.pop('_mtime', None)
    return runs
