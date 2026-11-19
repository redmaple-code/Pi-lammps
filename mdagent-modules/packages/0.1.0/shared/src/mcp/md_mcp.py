# -*- coding: utf-8 -*-
"""
分子模拟助手 只读 MCP 服务器（stdio, 零第三方协议依赖）
========================================================
- 领域: LAMMPS/ReaxFF 分子动力学编排系统(agent_deploy_kit)。本桥读它的真相源:
  .v3.3_state.json / status_bus.json / logs/*.jsonl —— 不杀进程、不碰力场/输入(科研红线)。
- 唯一写门 sim_exec: 经 agent_deploy_kit 的 claude_interface.py exec 审计通道执行
  (可执行文件白名单 + shell=False 防注入 + 逐条写 logs/claude_operations.jsonl);
  是否允许执行由 dsh 审批弹窗决定(dsh-md-ops-gate 插件把该工具路由到 approval seam),
  无应答者(headless)时 fail-closed 拒绝。
- 双模式(通用 Windows/Linux):
  mode=local  直读 local_base 下的文件(dsh 装在跑模拟的机器上,或读本地快照)
  mode=ssh    paramiko 只读拉 remote_base(dsh 在 Windows、真机在远程 Linux)
- 传输: MCP stdio = 换行分隔 JSON-RPC 2.0(每行一条)。日志走 stderr,绝不污染 stdout。
- 工具名对模型呈现为 mcp__mdsim__<name>(namespace 由 dsh-mcp-client 的 serverName=mdsim 决定)。
"""
import sys, os, json, traceback

# 环境免疫:Windows 默认 GBK stdout 编不了中文/⚠ 会崩 —— 启动即锁 UTF-8(病灶 D-04 教训,来自量化役)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
_cfg_path = os.path.join(HERE, "md_config.json")
if not os.path.exists(_cfg_path):  # 开源克隆/快照:回退模板,setup.sh 会播种真配置
    _cfg_path += ".example"
CFG = json.load(open(_cfg_path, "r", encoding="utf-8"))
PROTOCOL_VERSION_FALLBACK = "2024-11-05"
MODE = CFG.get("mode", "local")
SRC = ("本地真相源(只读)" if MODE == "local" else "远程Linux真机(SSH只读)")

def log(msg):
    sys.stderr.write("[md_mcp] %s\n" % msg); sys.stderr.flush()

import paths

# GROMACS 扫描器与看板共享唯一实现(dashboard/gmx_scan.py);
# 本地扫描,与 mode=ssh 无关(gromacs 跑在哪台机,dsh 就装在哪台机)。
sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
import gmx_scan  # noqa: E402
def _toolcall_log(name, args, data):
    try:
        import time
        with open(paths.toolcalls_log(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "tool": name, "args": args,
                                "result_keys": list(data.keys()) if isinstance(data, dict) else str(type(data))},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass

# ---------- 双模式文件读取(local 直读 / ssh 拉) ----------
def _local_path(rel):
    # 环境变量优先(迁移机器时不改配置也能跑),其次 md_config.json
    return os.path.join(_kit_base(), *rel.split("/"))


def _kit_base():
    """kit 根目录:环境变量 > 配置 > 代码相对兜底。"""
    return (os.environ.get("MDSIM_KIT_BASE") or CFG.get("local_base") or
            os.path.join(os.path.dirname(HERE), "..", "..", "agent_deploy_kit_20260630"))


def _execution_gap():
    """写门可用性缺口;空列表=可执行。"""
    gaps = []
    registrations = CFG.get("evidence_execution_registrations")
    if not isinstance(registrations, list) or not registrations:
        gaps.append("md_config.json 缺少 evidence_execution_registrations")
    bridge = os.path.join(_kit_base(), "agents", "runtime", "v3.3", "claude_interface.py")
    if not os.path.isfile(bridge):
        gaps.append("审计桥缺失: %s" % bridge)
    return gaps

def _ssh_client():
    import paramiko
    s = CFG.get("ssh", {})
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": s.get("host"), "username": s.get("user"), "timeout": 20}
    if s.get("key_file"):
        kw["key_filename"] = s["key_file"]
    elif s.get("password"):
        kw["password"] = s["password"]
    cli.connect(**kw)
    return cli

def _ssh_run(cmd, timeout=45):
    cli = _ssh_client()
    try:
        _in, out, err = cli.exec_command(cmd, timeout=timeout)
        return out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")
    finally:
        cli.close()

def read_text(rel):
    """读整个文件文本(不存在返回 None)。"""
    if MODE == "local":
        p = _local_path(rel)
        if not os.path.exists(p):
            return None
        return open(p, "r", encoding="utf-8", errors="replace").read()
    remote = CFG.get("ssh", {}).get("remote_base", "").rstrip("/") + "/" + rel
    o, e = _ssh_run("cat '%s' 2>/dev/null" % remote)
    return o if o.strip() else None

def read_tail(rel, n=15):
    """读文件末 n 行(不存在返回 [])。"""
    if MODE == "local":
        p = _local_path(rel)
        if not os.path.exists(p):
            return []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    remote = CFG.get("ssh", {}).get("remote_base", "").rstrip("/") + "/" + rel
    o, e = _ssh_run("tail -n %d '%s' 2>/dev/null" % (n, remote))
    return [ln + "\n" for ln in o.splitlines()]

def read_json(rel):
    t = read_text(rel)
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception as ex:
        log("json parse %s: %s" % (rel, ex))
        return None

DISC_READONLY = ("只读真相源(state/日志文件),数字均为文件真实内容;本工具不写、不杀进程、不改 LAMMPS 输入/力场(科研红线)。"
                 "高危操作(kill_lammps/modify_data/change_forcefield 等)走唯一写门 sim_exec(claude_interface 审计通道+dsh 弹窗确认),非本工具能力。")

DISC_WRITE_GATE = ("唯一写门:命令经 claude_interface.py exec 执行(白名单+shell=False+逐条审计 logs/claude_operations.jsonl)。"
                   "执行前必须已在对话里向用户讲清后果,且用户在 dsh 弹窗里点了批准;用户拒绝即停止、绝不换路径绕过;"
                   "结果数字以本返回为准,不编造。")

DISC_PIPELINE = ("确定性巡检流水线(pipeline_check.py):log 解析/异常扫描/风险评分全在代码里完成,"
                 "模型只负责调用与把返回翻译成人话。回答状态/巡检问题时,status/step/temp/risk_score 等数字"
                 "必须以本工具返回为准,不得凭记忆或推测编造;返回 PIPELINE_ERROR 时如实转告并附 error。")

DISC_RESEARCH = ("确定性研讨流水线(model_research.py):拆题/并行联网收集/量规评分/终裁全在代码里完成,"
                 "模型只负责调用与把 verdict 翻译成人话。推荐策略必须以返回的 strategies/verdict 为准,"
                 "verified=false 的策略必须明说'缺来源支撑';返回 DEGRADED/RESEARCH_ERROR 时如实转告,不得编造结论。")

DISC_DESIGN = ("确定性实验设计论证流水线(model_research.py design 模式):对仿真实验设计(下压深度/磨头几何/"
               "载荷/滑速/样品尺度/控温方案等)做文献基准/经典判据/已知缺陷/实验对照四路论证,代码终裁。"
               "论证结果是【证据不是判决】:verified 的取值范围经 mdproj 的 design_evidence 入账,"
               "存疑参数要么改设计、要么用户显式接受为存疑假设;硬拦只来自确定性 E 门,不因论证结论绕过。"
               "返回 DEGRADED/RESEARCH_ERROR 时如实转告,不得编造结论。")

DISC_REPAIR = ("确定性修复研讨流水线(model_research.py repair 模式):风险异常→拆题→多路并行联网收集"
               "(官方病因/社区同症状案例/体系已知坑/策略副作用)→量规评测→代码终裁,全在代码里完成。"
               "修复建议必须以返回的 strategies/verdict 为准,verified=false 的必须明说'缺来源支撑';"
               "任何实际改动仍走唯一写门 sim_exec 并经用户确认,本工具本身只读不写;"
               "返回 DEGRADED/RESEARCH_ERROR 时如实转告,不得编造结论。")

DISC_HPC = ("超算提交预留接口(hpc_submit.py):只做确定性脚本渲染与校验(slurm/pbs),本身不写盘不提交。"
            "DRY_RUN 返回的 script/submit_cmd 是预览;落地必须先经用户确认,脚本用 write 工具写盘,"
            "提交命令经唯一写门 sim_exec 执行(sbatch/qsub 须在审计白名单内,启用 hpc.enabled 属人工决策)。"
            "集群参数(分区/账号/墙钟)以 md_config.json 的 hpc.* 与工具入参为准,不得编造。")

def sim_hpc_submit(args):
    """超算作业提交接口(预留):确定性渲染 sbatch/qsub 作业脚本并给出提交命令。dry_run 默认 true 只预览;真正提交仍走 sim_exec 写门+用户确认。"""
    import hpc_submit
    result = hpc_submit.run(args, cfg_hpc=CFG.get("hpc", {}))
    result["_discipline"] = DISC_HPC
    return result

def _latest_pipeline_snapshot(case_id=""):
    """读数据根 scratch/pipeline/snap_*.json:指定 case 取其快照,否则取最新一份。
    用于修复研讨自动拼题。快照不走 read_json(那个相对 deploy_kit 根)。"""
    snap_dir = paths.pipeline_scratch_dir()

    def _load(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None

    def _find(d, cid):
        if cid:
            p = os.path.join(d, "snap_%s.json" % cid)
            return p if os.path.exists(p) else None
        try:
            cands = [n for n in os.listdir(d)
                     if n.startswith("snap_") and n.endswith(".json")]
        except OSError:
            return None
        if not cands:
            return None
        return os.path.join(d, max(cands, key=lambda n: os.path.getmtime(os.path.join(d, n))))

    hit = _find(snap_dir, case_id)
    return _load(hit) if hit else None

def sim_repair_research(args):
    """风险修复研讨:巡检发现异常/高风险(CRITICAL/WARNING 或 risk_score 偏高)后,给修复建议前必须先调本工具(流程硬约束)。question 可缺省——缺省时用 anomalies 参数,再缺省取最近一次巡检快照自动拼题。耗时约 1-3 分钟属正常。"""
    question = str(args.get("question", "")).strip()
    context = str(args.get("context", "")).strip() or None
    case_id = str(args.get("case_id", "")).strip()
    anomalies = args.get("anomalies")
    if isinstance(anomalies, str):
        anomalies = [anomalies]
    if not question:
        if anomalies:
            question = "LAMMPS 模拟异常修复: %s" % "; ".join(str(a)[:120] for a in list(anomalies)[:8])
        else:
            snap = _latest_pipeline_snapshot(case_id)
            if snap:
                anoms = snap.get("anomalies") or []
                question = ("LAMMPS 模拟异常修复(case %s): status=%s, risk_score=%s, step=%s, 异常=[%s]"
                            % (snap.get("case_id", case_id or "?"), snap.get("status", "?"),
                               snap.get("risk_score", "?"), snap.get("step", "?"),
                               "; ".join(str(a)[:80] for a in list(anoms)[:6]) or "无明细"))
    if case_id:
        context = ("case=%s; " % case_id) + (context or "")
    import model_research
    result = model_research.run(question, context, mode="repair")
    result["_discipline"] = DISC_REPAIR
    return result

def sim_model_research(args):
    """确定性模型研讨:一步跑完 拆题→多路并行联网收集→量规评测→代码终裁。回答'力场/参数集/模型选型、哪个更好、要不要换'必须用本工具(流程硬约束)。耗时约 1-3 分钟属正常。"""
    question = str(args.get("question", "")).strip()
    context = str(args.get("context", "")).strip() or None
    import model_research
    result = model_research.run(question, context)
    result["_discipline"] = DISC_RESEARCH
    return result


def sim_design_research(args):
    """【前置双门·C2e 实验设计论证必调】确定性设计论证流水线:对实验设计参数(下压深度/磨头几何尺寸/载荷/滑速/样品尺度/控温方案等)做文献基准→经典判据→已知设计缺陷→实验对照四路联网论证→独立评测器按量规打分→代码终裁(零引用压分)。凡 intent 含 experiment_design.setup,给出任何设计合理性结论前必须先调本工具;结论以 verdict/strategies 为准(每个 strategy=一个设计参数的判定与文献典型范围),verified=false 的必须明说缺来源。耗时 1-3 分钟属正常。只读调研;证据经 mdproj design_evidence 入账,设计修改走 update_intent。"""
    question = str(args.get("question", "")).strip()
    context = str(args.get("context", "")).strip() or None
    if not question:
        return {"error": "需要 question 参数(把设计参数清单与单位写全,如:球磨头 R=50Å,压深 5Å,滑速 20 m/s,样品 200×200×80Å)",
                "_discipline": DISC_DESIGN}
    import model_research
    result = model_research.run(question, context, mode="design")
    result["_discipline"] = DISC_DESIGN
    return result

def sim_pipeline_check(args):
    """确定性巡检:一步跑完 log 解析→monitor_agent 风险评分→与上次快照对比(deltas)。回答'模拟跑到哪了/状态怎么样/有没有异常风险'必须用本工具(流程硬约束)。参数 prod_dir/log_file/case_id 均可缺省——缺省时从 .v3.3_state.json 当前任务兜底,log_file 再缺省则取 prod_dir 下最新 *.log。"""
    prod_dir = str(args.get("prod_dir", "")).strip()
    log_file = str(args.get("log_file", "")).strip() or None
    case_id = str(args.get("case_id", "")).strip()
    if not prod_dir or not case_id:
        task = (read_json(".v3.3_state.json") or {}).get("task") or {}
        prod_dir = prod_dir or str(task.get("prod_dir", ""))
        case_id = case_id or str(task.get("case_id", "unknown"))
    import pipeline_check
    result = pipeline_check.run(prod_dir, log_file, case_id or "unknown")
    result["_discipline"] = DISC_PIPELINE
    return result

def sim_exec(args):
    """唯一写门：当前任务单次许可与审计桥共同约束执行。"""
    if MODE != "local":
        return {"error": "write-mode-unavailable", "fail_closed": True,
                "_discipline": DISC_WRITE_GATE}
    adapter = _evidence_adapter()
    if not hasattr(adapter, "handoff"):
        gaps = _execution_gap() or ["execution adapter is unavailable"]
        return {"error": "execution-profile-not-configured", "detail": gaps,
                "fail_closed": True,
                "hint": "sim_selfcheck 查看缺失项；安装 kit 后重跑 scripts/install_config.py",
                "_discipline": DISC_WRITE_GATE}
    try:
        import evidence_api
        from evidence_v3.contracts import V3Error
        svc = evidence_api.task_service(paths.data_root(), adapter)
        task_id = args.get("task_id") or args.get("case_id")
        if args.get("action") == "reconcile":
            run_id = args.get("run_id")
            receipt = args.get("receipt", {})
            if not task_id or not run_id or not isinstance(receipt, dict):
                raise V3Error("RECONCILIATION_REQUEST_REQUIRED")
            reconciled = svc.reconcile_start(task_id, run_id, receipt, reconciler=adapter.reconcile)
            if reconciled.get("reconciliation", {}).get("execution_complete") is True:
                reconciled = svc.finish(task_id, run_id, {
                    "run_id": run_id,
                    "returncode": reconciled["reconciliation"]["returncode"],
                    "outputs": reconciled["reconciliation"]["outputs"],
                })
            reconciled["_discipline"] = DISC_WRITE_GATE
            return reconciled
        permit_id = args.get("permit_id")
        request_id = args.get("request_id")
        spec = args.get("spec")
        if not task_id or not permit_id or not request_id or not isinstance(spec, dict):
            raise V3Error("execution-permit-required",
                          "task_id, permit_id, request_id and spec are required")
        result = svc.start(task_id, permit_id, spec, request_id, adapter.handoff)
        if result.get("status") == "RUNNING" and result.get("launch", {}).get("execution_complete") is True:
            result = svc.finish(task_id, result["id"], {
                "run_id": result["id"],
                "returncode": result["launch"]["returncode"],
                "outputs": result["launch"]["outputs"],
            })
        result["_discipline"] = DISC_WRITE_GATE
        return result
    except V3Error as exc:
        return dict(exc.as_dict(), _discipline=DISC_WRITE_GATE)
    except Exception as exc:
        return {"error": "execution-internal-error", "detail": type(exc).__name__,
                "fail_closed": True, "_discipline": DISC_WRITE_GATE}


def _bridge_exec(args):
    """审计桥适配；只由已消费许可的启动回调调用。"""
    cmd = str(args.get("cmd", "")).strip()
    desc = str(args.get("description", "")).strip()
    cwd = str(args.get("cwd", "")).strip() or None
    background = bool(args.get("background", False))
    log_path = str(args.get("log", "")).strip() or None
    if not cmd:
        return {"error": "需要 cmd 参数(要执行的命令)", "_discipline": DISC_WRITE_GATE}
    if MODE != "local":
        return {"error": "写门仅在本机真机(local 模式)开放;ssh 模式只读。",
                "_discipline": DISC_WRITE_GATE}
    from evidence_bridge import BridgeExecutionError, run_bridge
    if background:
        return {"error": "bridge-background-unsupported", "fail_closed": True,
                "_discipline": DISC_WRITE_GATE}
    ci = _local_path("agents/runtime/v3.3/claude_interface.py")
    if not os.path.exists(ci):
        return {"error": "claude_interface.py 不存在: %s" % ci, "_discipline": DISC_WRITE_GATE}
    argv = [sys.executable, ci, "exec_bg" if background else "exec", cmd, desc or cmd]
    if cwd or (background and log_path):
        argv.append(cwd or ".")
    if background and log_path:
        argv.append(log_path)
    # Deadline cleanup must run as the same identity as the bridge and its tree.
    # The old sudo path needs a privileged termination broker before support.
    _bridge = CFG.get("exec_bridge") or {}
    _sudo_user = _bridge.get("sudo_user")
    if _sudo_user:
        # A different identity cannot be reliably killed by this MCP process.
        return {"error": "bridge-identity-switch-unsupported", "fail_closed": True,
                "_discipline": DISC_WRITE_GATE}
    try:
        timeout = args.get("timeout_seconds", 360)
        bridge_env = dict(os.environ, **args.get("environment", {}))
        r = run_bridge(argv, timeout=timeout, env=bridge_env)
    except BridgeExecutionError as exc:
        return dict(exc.as_dict(), cmd=cmd, _discipline=DISC_WRITE_GATE)
    except Exception as exc:
        return {"error": "bridge-execution-error", "detail": type(exc).__name__,
                "execution_state": "START_UNKNOWN", "fail_closed": True,
                "cmd": cmd, "_discipline": DISC_WRITE_GATE}
    # claude_interface stdout 末尾是 JSON(前面有 [CLAUDE_INTERFACE] 提示行)
    out = (r.stdout or "").strip()
    payload = None
    brace = out.find("{")
    if brace >= 0:
        try:
            payload = json.loads(out[brace:])
        except Exception:
            payload = None
    parsed_payload = isinstance(payload, dict)
    if not parsed_payload:
        payload = {"returncode": r.returncode, "stdout": out, "stderr": (r.stderr or "")[:2000]}
    # Capability is issued here, never accepted from the old whitelist receipt.
    payload.pop("timeout_enforced", None)
    payload.pop("timeout_seconds", None)
    if parsed_payload and r.returncode == 0:
        payload["timeout_enforced"] = True
        payload["timeout_seconds"] = timeout
    payload["cmd"] = cmd
    payload["description"] = desc or cmd
    payload["audit_log"] = "logs/claude_operations.jsonl"
    payload["_discipline"] = DISC_WRITE_GATE
    return payload

# ---------- 工具实现 ----------
def sim_status(args):
    """当前各模拟 case 的实时状态(读 status_bus.json):步数/进度/温度/阶段/异常 + 系统负载。判断'模拟跑到哪了/有没有异常'用它。"""
    bus = read_json("status_bus.json")
    if bus is None:
        return {"source": SRC, "status": "NO_ACTIVE_RUN", "cases": {}, "case_count": 0,
                "note": "status_bus.json 仅由编排器在运行期写入；无该文件表示当前没有运行的模拟"}
    cases_out = {}
    for cid, cv in (bus.get("cases") or {}).items():
        if not isinstance(cv, dict):
            cases_out[cid] = cv; continue
        # 状态总线可能是扁平(status/step/...)或含子对象(preflight_v2/monitor 等),两种都兼容
        row = {}
        for k in ("status", "step", "total", "pct", "temp_k", "stage", "speed", "eta_sec", "cores"):
            if k in cv:
                row[k] = cv[k]
        if cv.get("anomalies"):
            row["anomalies"] = cv["anomalies"]
        # 保留子阶段对象的键名(如 preflight_v2 的 issues/fixes_applied)供讲理
        subkeys = [k for k in cv.keys() if isinstance(cv.get(k), dict)]
        if subkeys and not row:
            row["_子阶段"] = {}
            for sk in subkeys:
                sv = cv[sk]
                row["_子阶段"][sk] = {kk: sv[kk] for kk in ("issues", "fixes_applied", "can_rebuild", "missing_elements") if kk in sv}
        cases_out[cid] = row or cv
    return {"source": SRC + " status_bus.json", "timestamp": bus.get("timestamp"),
            "version": bus.get("version"), "cases": cases_out, "case_count": len(cases_out),
            "system": bus.get("system"),
            "_discipline": DISC_READONLY}


def sim_selfcheck(args):
    """部署自检:写门/执行注册表/kit/知识源/人工审批箱/状态总线一次看清。只读。"""
    import human_authority
    kit = _kit_base()
    kit_files = ("claude_interface", "preflight_agent", "launch_agent",
                 "monitor_agent", "recover_agent", "analyze_agent", "research_agent")
    runtime = os.path.join(kit, "agents", "runtime", "v3.3")
    missing = [name for name in kit_files
               if not os.path.isfile(os.path.join(runtime, name + ".py"))]
    registrations = CFG.get("evidence_execution_registrations")
    configured = isinstance(registrations, list) and bool(registrations)
    executable = registrations[0].get("executable") if configured else None
    executable_present = bool(executable and os.path.isfile(executable))
    execution = {
        "mode": MODE, "kit": kit, "kit_complete": not missing,
        "missing_kit_files": missing,
        "registrations": len(registrations) if configured else 0,
        "executable_present": executable_present,
        "available": MODE == "local" and not missing and configured and executable_present,
    }
    if not execution["available"]:
        execution["remediation"] = [
            "安装 agent_deploy_kit（7 个 runtime 文件），让 local_base 或 MDSIM_KIT_BASE 指向它",
            "重跑 python scripts/install_config.py 播种 evidence_execution_registrations",
        ]
    try:
        from evolution.kb import adapter
        knowledge = {"mode": "ukb" if adapter.enabled() else "legacy"}
    except Exception as exc:
        knowledge = {"mode": "unknown", "error": type(exc).__name__}
    bus = _local_path("status_bus.json")
    return {"mode": MODE, "data_root": paths.data_root(),
            "execution": execution, "knowledge": knowledge,
            "human_inbox": {"root": str(human_authority.inbox_root()),
                            "pending": len(human_authority.pending())},
            "status_bus_present": bool(bus and os.path.isfile(bus)),
            "tools": {"sim_exec": execution["available"],
                      "sim_pipeline_check": not missing,
                      "sim_status": True,
                      "sim_evidence": True},
            "_discipline": DISC_READONLY}

def _strategy_decisions(n):
    rows = []
    for ln in read_tail("logs/strategy_decisions.jsonl", n):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
            det = d.get("details") or {}
            rows.append({"ts": d.get("ts"), "phase": "%s→%s" % (d.get("phase_from"), d.get("phase_to")),
                         "trigger": d.get("trigger"), "decision": d.get("decision"),
                         "strategy": det.get("strategy"), "attempt": det.get("attempt")})
        except Exception:
            pass
    return {"source": SRC + " strategy_decisions.jsonl", "count": len(rows), "decisions": rows,
            "_discipline": DISC_READONLY}

def _orchestrator_log(n):
    rows = []
    for ln in read_tail("logs/orchestrator_v3.3.jsonl", n):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
            rows.append({"ts": d.get("ts"), "level": d.get("level"), "msg": d.get("msg")})
        except Exception:
            pass
    return {"source": SRC + " orchestrator_v3.3.jsonl", "count": len(rows), "log": rows,
            "_discipline": DISC_READONLY}

def sim_context(args):
    """编排器上下文:section=state(默认)读 .v3.3_state.json 相位/任务/恢复+审计流水;
    section=strategy 取策略决策链;section=orchestrator 取状态机日志。
    (2026-09-09 工具面精简:sim_strategy_log/sim_orchestrator_log 并入本节。)"""
    section = str(args.get("section", "state") or "state").strip()
    if section == "strategy":
        return _strategy_decisions(int(args.get("n", 12)))
    if section == "orchestrator":
        return _orchestrator_log(int(args.get("n", 20)))
    st = read_json(".v3.3_state.json") or {}
    ops_lines = read_tail("logs/claude_operations.jsonl", int(args.get("n", 10)))
    recent = []
    for ln in ops_lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            recent.append(json.loads(ln))
        except Exception:
            pass
    task = st.get("task") or {}
    return {"source": SRC + " .v3.3_state.json + claude_operations",
            "phase": st.get("phase"), "monitor_subphase": st.get("monitor_subphase"),
            "task": {k: task.get(k) for k in ("case_id", "prod_dir", "total_steps", "cores", "target_elements") if k in task},
            "recovery_count": len(st.get("recovery_history") or []),
            "last_tick": st.get("last_tick"),
            "recent_operations": recent,
            "_discipline": DISC_READONLY}

def _review_section(fn):
    """分节容错:任一数据源缺席/损坏只影响该节,整包永不抛。"""
    try:
        return fn()
    except Exception as ex:
        return {"error": "%s: %s" % (type(ex).__name__, ex)}


def _tail_jsonl(path, n):
    # n 为 LLM 入参(24 轮审计):负数会把 lines[-n:] 反成"掐头",钳到 [1,1000]
    try:
        n = max(1, min(int(n), 1000))
    except (TypeError, ValueError):
        n = 10
    rows = []
    if not path or not os.path.exists(path):
        return rows
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
    except Exception:
        return rows
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            pass
    return rows


def sim_gmx_status(args):
    """GROMACS 运行状态:扫 dashboard/monitor_roots.json 配置的根,
    返回各 run 的阶段/步数/进度/模拟时长/温度/总能量/存活。与看板同一实现,
    本机扫描,永不抛(配置缺席/损坏 → 空列表)。"""
    try:
        runs = gmx_scan.scan_gromacs_runs()
    except Exception as e:
        return {"source": "本机 GROMACS 扫描(monitor_roots.json)", "runs": [],
                "count": 0, "alive_count": 0, "error": "扫描失败: %s" % e,
                "_discipline": DISC_READONLY}
    return {"source": "本机 GROMACS 扫描(monitor_roots.json)",
            "count": len(runs),
            "alive_count": sum(1 for r in runs if r.get("alive")),
            "runs": runs,
            "_discipline": DISC_READONLY}


def sim_review_packet(args):
    """一屏紧凑复盘包(借鉴 LoopX review-packet,2026-09-07):编排器相位+should-run 裁决、
    熔断、待人工闸队列、管道事件尾、演化提案/LEDGER 账——一次拿全"现在到哪了、
    哪步在等人"。只组合既有真相源,只读,分节容错永不抛。"""
    try:
        n = max(1, min(int(args.get("n", 10)), 1000))
    except (TypeError, ValueError):
        n = 10

    def _context():
        st = read_json(".v3.3_state.json") or {}
        return {"phase": st.get("phase"), "monitor_subphase": st.get("monitor_subphase"),
                "case_id": (st.get("task") or {}).get("case_id"),
                "should_run_last": st.get("_should_run_last"),
                "recovery_breaker": st.get("recovery_breaker"),
                "unmanaged_runs": list((st.get("_unmanaged_runs") or {}).keys())}

    def _gates():
        q = read_json(".v3.3_user_gates.json") or {}
        gates = [g for g in (q.get("gates") or []) if isinstance(g, dict)]
        pending = [g for g in gates if g.get("status") == "pending"]
        return {"pending_count": len(pending),
                "pending": [{k: g.get(k) for k in
                             ("id", "kind", "ref", "question", "created_ts", "source")}
                            for g in pending],
                "total": len(gates)}

    def _bus():
        bus = read_json("status_bus.json") or {}
        return {"timestamp": bus.get("timestamp"),
                "cases": {cid: {k: cv.get(k) for k in ("status", "step", "pct", "temp_k")
                                if isinstance(cv, dict)}
                          for cid, cv in (bus.get("cases") or {}).items()}}

    def _events():
        return {"tail": _tail_jsonl(paths.pipeline_events_log(), n)}

    def _proposals():
        pdir = os.path.join(paths.memory_dir(), "proposals")
        applied = set()
        # applied 集合须全量账(钳尾会让老 LEDGER 上已应用提案误判 human_pending)
        ledger_path = os.path.join(paths.memory_dir(), "LEDGER.jsonl")
        if os.path.exists(ledger_path):
            with open(ledger_path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(row, dict) and row.get("result") in ("APPLIED", "AUTO_APPLIED"):
                        applied.add(str(row.get("slug")))
        items, human_pending = [], 0
        if os.path.isdir(pdir):
            for d in sorted(os.listdir(pdir)):
                meta_p = os.path.join(pdir, d, "meta.json")
                if not os.path.isfile(meta_p):
                    continue
                try:
                    meta = json.load(open(meta_p, encoding="utf-8"))
                except Exception:
                    meta = {}
                done = d in applied
                if meta.get("tier") == "human" and not done:
                    human_pending += 1
                items.append({"dir": d, "class": meta.get("class"),
                              "tier": meta.get("tier"), "applied": done})
        return {"count": len(items), "human_pending": human_pending, "items": items[-n:]}

    def _ledger():
        return {"tail": _tail_jsonl(os.path.join(paths.memory_dir(), "LEDGER.jsonl"), 5)}

    return {"source": SRC + " + dsh 演化层(只读)",
            "context": _review_section(_context),
            "user_gates": _review_section(_gates),
            "bus": _review_section(_bus),
            "pipeline_events": _review_section(_events),
            "proposals": _review_section(_proposals),
            "ledger": _review_section(_ledger),
            "_discipline": DISC_READONLY}


def read_paper(args):
    """读一篇文献(PDF/txt/md/docx)返回正文文本，供 Mode B 解读意图、设计建模与动力学方案。只读。参数 path(文件绝对路径)。"""
    path = str(args.get("path", "")).strip().strip('"').strip("'")
    if not path:
        return {"error": "需要 path 参数(文献文件绝对路径，如 E:\\\\papers\\\\aerogel.pdf)"}
    # 双模式:local 读本机路径;ssh 模式先只支持本机路径(文献一般在你操作的机器上)
    if not os.path.exists(path):
        return {"error": "文件不存在: %s" % path}
    ext = os.path.splitext(path)[1].lower()
    max_chars = int(args.get("max_chars", 60000))
    text = ""; meta = {}
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(path)
            meta["pages"] = len(r.pages)
            text = "\n".join((p.extract_text() or "") for p in r.pages)
            if len(text.strip()) < 200:   # 扫描件/抽取为空 → pdfplumber 兜底
                try:
                    import pdfplumber
                    with pdfplumber.open(path) as pdf:
                        text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
                    meta["extractor"] = "pdfplumber"
                except Exception:
                    meta["extractor"] = "pypdf(可能扫描件,文本稀少)"
            else:
                meta["extractor"] = "pypdf"
        elif ext == ".docx":
            import docx
            d = docx.Document(path)
            text = "\n".join(p.text for p in d.paragraphs)
            meta["extractor"] = "python-docx"
        else:   # txt/md/in/dat/log 等按文本读
            text = open(path, encoding="utf-8", errors="replace").read()
            meta["extractor"] = "text"
    except Exception as ex:
        return {"error": "读取失败(%s): %s" % (ext, str(ex)[:160])}
    total = len(text)
    return {"source": "read_paper " + os.path.basename(path), "path": path, "ext": ext,
            "total_chars": total, "truncated": total > max_chars, "meta": meta,
            "text": text[:max_chars],
            "_discipline": ("只读文献正文，不写、不改。据此给出的建模/动力学是【方案建议】，非既成事实；"
                            "真正建模/跑模拟仍须经编排器+你确认(红线)。文献里的数值以原文为准、引用标注出处；"
                            "与已有 case 参数口径对齐时注明差异。")}

DISC_SEARCH = ("只读检索 dsh 会话存档(sessions/*/session-*/session.jsonl.zstd,zstd 流式解压):"
               "返回真实对话片段(会话/工作目录/时间/原文),不是记忆重述。用于'之前怎么解决的/上次怎么修的/"
               "历史会话说过什么'类回忆型问题——以档为准,不凭印象编造;片段里的模拟数字若要引用,"
               "仍以真相源工具(sim_status/sim_pipeline_check 等)现查为准。不写、不改任何会话文件。")

def _session_text_rows(d):
    """从一条会话记录里抽 (role, text):只取真实用户行(source.kind=user)与助手终稿行,
    排除插件提醒/tool-result(同为 user 角色但非人类输入)与 reasoning 块。"""
    t = d.get("type")
    data = d.get("data") or {}
    if t == "user/message":
        if (data.get("source") or {}).get("kind") not in (None, "user"):
            return None
        content = data.get("content")
        role = "user"
    elif t == "assistant/message":
        content = ((data.get("message") or {}).get("content"))
        role = "assistant"
    else:
        return None
    if isinstance(content, str):
        return role, content
    if not isinstance(content, list):
        return None
    parts = [b.get("text") or "" for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(p for p in parts if p)
    return (role, text) if text.strip() else None


def sim_search_sessions(args):
    """检索历史会话存档(只读):按关键词找过往对话的用户/助手消息,返回会话/工作目录/
    时间/角色/原文片段。回答'之前怎么解决的/上次那个报错怎么修的'类回忆型问题用它,
    以真实会话记录为准,不凭记忆编造。多词空格分隔=须同时命中。"""
    import subprocess
    import time as _time
    q = str(args.get("q", "")).strip()
    if not q:
        return {"error": "需要 q 参数(检索词,多个词空格分隔=须同时命中)", "_discipline": DISC_SEARCH}
    terms = [t.lower() for t in q.split() if t.strip()][:8]
    try:
        limit = max(1, min(int(args.get("limit", 20)), 50))
    except (TypeError, ValueError):
        limit = 20
    MAX_SESSIONS, MAX_PER_SESSION, BUDGET_SEC, MAX_LINE = 60, 3, 30, 400000

    root = os.path.join(paths.dsh_home(make=False), "sessions")
    cand = []
    if os.path.isdir(root):
        for ws in os.listdir(root):
            wsd = os.path.join(root, ws)
            if not os.path.isdir(wsd):
                continue
            for sid in os.listdir(wsd):
                sd = os.path.join(wsd, sid)
                if not os.path.isdir(sd):
                    continue
                f = os.path.join(sd, "session.jsonl.zstd")
                if os.path.isfile(f):
                    cand.append((os.path.getmtime(f), f, ws, sid))
    cand.sort(reverse=True)

    matches, scanned, truncated = [], 0, False
    deadline = _time.monotonic() + BUDGET_SEC
    for _mt, f, ws, sid in cand[:MAX_SESSIONS]:
        if len(matches) >= limit or _time.monotonic() > deadline:
            truncated = len(matches) < limit and len(cand) > MAX_SESSIONS
            break
        scanned += 1
        try:
            proc = subprocess.Popen(["zstd", "-dc", f], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        except OSError as e:
            return {"source": DISC_SEARCH.split(":")[0], "q": q,
                    "error": "zstd 不可用(会话存档为 zstd 压缩): %s" % e,
                    "_discipline": DISC_SEARCH}
        cwd, hits, stop = ws, 0, False
        try:
            for raw in proc.stdout:
                if _time.monotonic() > deadline:
                    truncated, stop = True, True
                    break
                if len(raw) > MAX_LINE:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("type") == "session":
                    cwd = d.get("cwd") or cwd
                    continue
                if hits >= MAX_PER_SESSION:
                    continue
                row = _session_text_rows(d)
                if not row:
                    continue
                role, text = row
                low = text.lower()
                if not all(t in low for t in terms):
                    continue
                idx = low.find(terms[0])
                a = max(0, idx - 100)
                snippet = text[a:min(len(text), idx + 240)].replace("\n", " ⏎ ")
                matches.append({
                    "session": sid.replace("session-", ""), "workspace": ws,
                    "cwd": cwd, "time": d.get("time"), "role": role,
                    "seq": d.get("seq"), "snippet": snippet,
                })
                hits += 1
                if len(matches) >= limit:
                    stop = True
                    break
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if stop:
            break
    return {"source": "dsh 会话存档(只读,zstd 流式解压) " + root,
            "q": q, "sessions_total": len(cand), "sessions_scanned": scanned,
            "match_count": len(matches), "truncated": truncated,
            "matches": matches, "_discipline": DISC_SEARCH}


# ---------- 证据门(任务级阶段状态机 + 决策校验,2026-10-13 P0/P1) ----------
_EVO_DIR = os.path.join(os.path.dirname(HERE), "evolution")
if _EVO_DIR not in sys.path:
    sys.path.insert(0, _EVO_DIR)


def _evidence_modules():
    """旧核心仅供历史读取，不作为v3授权源。"""
    try:
        import stage_state
        import stage_gate
        return stage_state, stage_gate
    except Exception as ex:
        log("evidence modules unavailable: %r" % (ex,))
        return None, None


def _evidence_adapter():
    import evidence_api
    if MODE != "local":
        return evidence_api.UnsupportedExecutionAdapter()
    def launch_worker(job_path):
        import subprocess
        worker_script = os.path.join(HERE, "evidence_worker.py")
        creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                         getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
        inherited = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                     "MDSIM_DATA_ROOT", "MDSIM_KIT_BASE"}
        worker_env = {key: value for key, value in os.environ.items() if key.upper() in inherited}
        try:
            process = subprocess.Popen([sys.executable, worker_script, str(job_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=HERE, env=worker_env, shell=False, close_fds=True, creationflags=creationflags,
                start_new_session=(os.name == "posix"))
            if process.pid <= 0:
                process.terminate()
                return {"started": False}
        except Exception:
            return {"started": False}
        return {"started": True, "pid": process.pid}
    try:
        return evidence_api.configured_action_adapter(CFG, bridge_exec=_bridge_exec,
                                                      worker_launcher=launch_worker)
    except Exception as exc:
        log("execution registrations unusable: %r" % (exc,))
        return evidence_api.UnsupportedExecutionAdapter()


def sim_evidence(args):
    import evidence_api
    import human_authority
    result = evidence_api.dispatch(args, paths.data_root(),
                                   action_adapter=_evidence_adapter(),
                                   human_verifier=human_authority.verify)
    if result.get("error") == "HUMAN_AUTHORITY_REQUIRED":
        rows = human_authority.pending(args.get("task_id") or args.get("case_id"))
        if rows:
            result["pending_request"] = str(human_authority.inbox_root() / "requests" /
                                            (rows[0]["request_hash"] + ".json"))
            result["hint"] = ("owner: python tools/human_approve.py approve <request_hash> "
                              "--actor <id> --reason <why>")
    result["_discipline"] = DISC_READONLY if args.get("action") in (
        "status", "retrieve", "recognize", "propose") else DISC_WRITE_GATE
    return result


def sim_decision_gate(args):
    """阶段判断只消费当前v3任务，不接受客户端PASS授权。"""
    return sim_evidence(dict(args, action="evaluate", task_id=args.get("task_id") or args.get("case_id")))


def sim_surface(args):
    """动态核查计划兼容入口，不再运行固定目录。"""
    action = args.get("action", "status")
    if action not in ("status", "plan", "retrieve", "propose"):
        return {"error": "legacy-surface-write-disabled", "hint": "Use sim_evidence for evidence and plan revisions", "fail_closed": True}
    return sim_evidence(dict(args, action=action, task_id=args.get("task_id") or args.get("case_id")))


def sim_task_state(args):
    """当前任务唯一状态视图。"""
    return sim_evidence(dict(args, action="status", task_id=args.get("task_id") or args.get("case_id")))


def _exec_gate_check(args):
    """旧日志授权路径已关闭。

    仅供 tests/e2e_evidence 的对抗探针调用(断言旧通道必须被拒),生产写门
    不经过本函数:sim_exec 只接受 task_id/permit_id/request_id/spec。
    """
    return {"error": "execution-permit-required", "fail_closed": True,
            "_discipline": DISC_WRITE_GATE}


import evidence_api

TOOLS = [
    evidence_api.TOOL,
    {"name": "sim_decision_gate", "description": "核验当前v3任务的阶段凭据；旧客户端PASS不作为授权。",
     "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "stage": {"type": "string"}}, "required": ["case_id"], "additionalProperties": False}},
    {"name": "sim_surface", "description": "动态核查计划状态；不再采用固定检查项，写入请用sim_evidence。",
     "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "action": {"type": "string", "enum": ["status"]}}, "required": ["case_id"], "additionalProperties": False}},
    {"name": "sim_task_state", "description": "读取任务唯一状态、内容版本、凭据和未决问题；缺失或旧包不能授权。",
     "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"], "additionalProperties": False}},
    {"name": "sim_repair_research",
     "description": "【流程硬约束·风险修复必调】确定性修复研讨流水线:风险/异常→拆题→多路并行联网收集(官方病因/社区同症状案例/体系已知坑/策略副作用)→独立评测器按量规打分→代码终裁(零引用压分)。巡检(sim_pipeline_check/sim_status)返回 CRITICAL/WARNING 或 risk_score 偏高后,给出任何修复建议前必须先调本工具,结论以 verdict/strategies 为准,verified=false 的必须标注缺来源。question 可缺省(自动用 anomalies 或最近巡检快照拼题)。耗时 1-3 分钟属正常。只读调研;实际改动仍走 sim_exec+用户确认。",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "修复问题(可缺省;如:run1 NPT 阶段温度爆炸怎么修)"},
         "anomalies": {"type": "array", "items": {"type": "string"}, "description": "异常清单(可缺省;question 缺省时优先用它拼题)"},
         "case_id": {"type": "string", "description": "case 标识(可选;拼题与快照定位用)"},
         "context": {"type": "string", "description": "可选背景(体系/力场/系综/已试过的策略)"}
     }, "additionalProperties": False}},
    {"name": "sim_model_research",
     "description": "【流程硬约束·模型选型必调】确定性研讨流水线:拆题→多路并行联网收集资料→独立评测器按量规打分→代码终裁(零引用结论压分剔除)。回答'力场/参数集/模型选型、哪个更好、要不要换、选型对比'必须先调本工具,结论以 verdict/strategies 为准,verified=false 的必须标注缺来源。耗时 1-3 分钟属正常。只读调研,不改任何文件。",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "选型问题(如:石墨烯气凝胶热导模拟用哪个 ReaxFF 参数集)"},
         "context": {"type": "string", "description": "可选背景(体系/元素/温压范围/在跑的 case)"}
     }, "required": ["question"], "additionalProperties": False}},
    {"name": "sim_design_research",
     "description": "【前置双门·C2e 实验设计论证必调】确定性设计论证流水线:文献基准→经典判据→已知设计缺陷→实验对照四路联网论证→量规评测→代码终裁(零引用压分)。凡 intent 含 experiment_design.setup(摩擦/压痕/热导等实验工况),给出任何设计合理性结论(下压深度/磨头几何尺寸/载荷/滑速/样品尺度/控温方案等)前必须先调本工具;每个 strategy=一个设计参数的判定(score=证据置信)与文献典型范围(when_to_use),verified=false 必须明说缺来源。论证是证据不是判决:verified 取值经 mdproj design_evidence 入账,存疑参数改设计或用户显式接受,硬拦只来自确定性 E 门。耗时 1-3 分钟属正常。只读调研。",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "设计论证问题(参数清单+单位写全,如:高熵合金摩擦,球磨头 R=50Å,压深 5Å,滑速 20 m/s,样品 200×200×80Å,这些取值物理合理吗)"},
         "context": {"type": "string", "description": "可选背景(体系/目标性质/intent 摘要/关注的具体参数)"}
     }, "required": ["question"], "additionalProperties": False}},
    {"name": "sim_pipeline_check",
     "description": "【流程硬约束·状态问题必调】确定性巡检流水线:一步完成 log 解析→monitor_agent(v3.4)异常扫描与风险评分→与上次快照对比(deltas:step/temp/risk 变化)。回答'模拟跑到哪了/状态怎么样/有没有异常或风险'必须先调本工具,数字以其返回为准。prod_dir/log_file/case_id 均可缺省(自动从编排器状态与最新 log 兜底)。只读、不碰进程与输入文件。",
     "inputSchema": {"type": "object", "properties": {
         "prod_dir": {"type": "string", "description": "模拟产出目录(缺省取编排器当前任务)"},
         "log_file": {"type": "string", "description": "LAMMPS stdout 日志路径(缺省取 prod_dir 下最新 *.log)"},
         "case_id": {"type": "string", "description": "case 标识(缺省取编排器当前任务)"}
     }, "additionalProperties": False}},
    {"name": "sim_status",
     "description": "当前各 LAMMPS 模拟 case 的实时状态(读 status_bus.json):运行状态/步数/进度%/温度K/阶段/异常 + 系统负载。判断'模拟跑到哪了、有没有异常/风险'先用它。只读。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "sim_context",
     "description": "编排器上下文(读 .v3.3_state.json):相位(IDLE/PREFLIGHT/LAUNCH/MONITOR/RECOVER)/监控子相位/当前任务(case_id/步数/核数/元素)/恢复次数/最近审计操作;section=strategy 取策略决策链(strategy_decisions.jsonl:为什么进 RECOVER/换了什么策略/第几次尝试),section=orchestrator 取状态机日志(orchestrator_v3.3.jsonl:OBSERVE→ANALYZE 流转/alive/frozen)。看'现在在哪个阶段、为什么恢复、编排器在干嘛'用它。只读。",
     "inputSchema": {"type": "object", "properties": {
         "section": {"type": "string", "enum": ["state", "strategy", "orchestrator"],
                     "description": "state(默认)=相位/任务/恢复/审计流水;strategy=策略决策链;orchestrator=状态机日志"},
         "n": {"type": "integer", "description": "尾条数(state 节=最近审计操作,默认10;strategy 默认12;orchestrator 默认20)"}
     }, "additionalProperties": False}},
    {"name": "sim_gmx_status",
     "description": "当前各 GROMACS 运行的实时状态(扫 dashboard/monitor_roots.json 配置的根):阶段(prod/md/npt/min)/步数/进度%/模拟时长 ns/温度K/总能量/是否存活/是否接管(adopted)。问'gromacs/gmx/水凝胶跑到哪了、跑完没有、还活着吗'用它;LAMMPS 的状态用 sim_status/sim_pipeline_check。alive=true 且 adopted=false 的是野跑(不走编排器不能跑)——推送驱动按 gmx_adopted.json 的 auto_kill_wild 开关直接 SIGTERM 停掉(可 .cpt 续跑),要放行先把 dir 加进 adopted。本机扫描(与 mode=ssh 无关),配置缺席时返回空列表。只读。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "sim_review_packet",
     "description": "一屏紧凑复盘包:编排器相位+should-run 裁决、恢复熔断状态、待人工闸队列(breaker 终态/野跑待接管/演化 human_review 提案)、管道事件尾、提案与 LEDGER 账。回答'整体到哪了、哪步在等我、有没有要人工处理的'用它。只读、分节容错。",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer", "description": "事件/提案尾条数(默认10)"}}, "additionalProperties": False}},
    {"name": "sim_search_sessions",
     "description": "【回忆型问题先查档】检索历史会话存档(只读,zstd 流式解压):按关键词找过往对话的用户/助手消息,返回会话/工作目录/时间/角色/原文片段。问'之前 QEq 不收敛怎么修好的/上次哪个会话讨论过 AWH/历史会话说过什么'用它——以真实记录为准,不凭印象编造;片段里的模拟数字若要引用仍以真相源工具现查为准。多词空格分隔=须同时命中。",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "检索词(多个词空格分隔,须同时命中;如:QEq 不收敛 降温)"},
         "limit": {"type": "integer", "description": "返回片段上限(默认20,最大50)"}
     }, "required": ["q"], "additionalProperties": False}},
     {"name": "sim_selfcheck",
     "description": "【工具报错/不确定能否执行时先调】部署自检(只读):一次返回写门可用性(sim_exec)、kit 7 个 runtime 文件缺哪些、执行注册表是否已播种、知识源模式(ukb/legacy)、人工审批箱待办数、status_bus 是否存在。返回 execution.available=false 时按 remediation 修复后再试,不要反复盲试。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {'name': 'sim_exec', 'description': '唯一审计写门：启动需task_id、permit_id、request_id和精确spec，返回短时handoff状态；使用action=reconcile和run_id对账，START_UNKNOWN不得重发启动。只消费服务器核验的当前单次许可。validation与production独立，客户端标签不授权。旧cmd调用不能执行。', 'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['start', 'reconcile']}, 'task_id': {'type': 'string'}, 'permit_id': {'type': 'string'}, 'request_id': {'type': 'string'}, 'run_id': {'type': 'string'}, 'receipt': {'type': 'object'}, 'description': {'type': 'string'}, 'spec': {'type': 'object'}}, 'required': ['task_id'], 'additionalProperties': False}},
    {"name": "sim_hpc_submit",
     "description": "【预留接口·超算作业提交】确定性渲染 slurm(sbatch)/pbs(qsub)作业脚本:给 workdir/command/cores/walltime/partition 等参数,返回脚本文本+提交命令。dry_run 默认 true 只预览不写盘不提交;真正落地必须先用户确认、write 写脚本、再经 sim_exec 审计通道提交(sbatch/qsub 须在白名单内;hpc.enabled=false 时非 dry_run 一律拒绝)。集群默认分区/账号在 md_config.json 的 hpc.* 配置。需要把 LAMMPS 任务提交到超算队列时用它。",
     "inputSchema": {"type": "object", "properties": {
         "workdir": {"type": "string", "description": "作业工作目录(case 目录)"},
         "command": {"type": "string", "description": "作业内执行的命令(如 mpirun -np 32 lmp_mpi -in production.in)"},
         "case_id": {"type": "string", "description": "case 标识(可选,用于命名作业与脚本)"},
         "cores": {"type": "integer", "description": "总核数(默认32)"},
         "nodes": {"type": "integer", "description": "节点数(默认1)"},
         "walltime": {"type": "string", "description": "墙钟 hh:mm:ss 或 d-hh:mm:ss(默认24:00:00)"},
         "partition": {"type": "string", "description": "队列/分区(可选,缺省取 hpc.default_partition)"},
         "account": {"type": "string", "description": "计费账号(可选)"},
         "scheduler": {"type": "string", "description": "slurm/pbs/auto(默认 auto 自动探测)"},
         "job_name": {"type": "string", "description": "作业名(可选,默认 mdsim_<case_id>)"},
         "dry_run": {"type": "boolean", "description": "默认 true 只预览;false 时也要求 hpc.enabled=true 才返回 READY"}
     }, "required": ["workdir", "command"], "additionalProperties": False}},
    {"name": "read_paper",
     "description": "读一篇文献(PDF/txt/md/docx)返回正文文本，供【Mode B 文献解读】:解读研究意图、设计建模思路(结构/组分/力场/盒子)与动力学思路(系综/温压程序/timestep/平衡与生产)。用户丢来一篇文献要'解读/建模思路/动力学方案'时用它。复现类任务交付硬标准(docs/文献复现交付标准.md):①论文模拟信息逐条对照并指明模型体现位置;②体系汇总描述;③跑完关键指标与文献数值对比,对不上转带参照失效样本,不许润色交付。参数 path(文件绝对路径)、可选 max_chars。只读、不写。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "文献文件绝对路径(PDF/txt/md/docx)"},
         "max_chars": {"type": "integer", "description": "返回正文最大字符数(默认60000，超出截断)"}
     }, "required": ["path"], "additionalProperties": False}},
]
HANDLERS = {
    "sim_evidence": sim_evidence,
    "sim_repair_research": sim_repair_research,
    "sim_model_research": sim_model_research,
    "sim_design_research": sim_design_research,
    "sim_pipeline_check": sim_pipeline_check,
    "sim_status": sim_status,
    "sim_selfcheck": sim_selfcheck,
    "sim_context": sim_context,
    "sim_gmx_status": sim_gmx_status,
    "sim_review_packet": sim_review_packet,
    "sim_search_sessions": sim_search_sessions,
    "sim_exec": sim_exec,
    "sim_hpc_submit": sim_hpc_submit,
    "read_paper": read_paper,
    "sim_decision_gate": sim_decision_gate,
    "sim_surface": sim_surface,
    "sim_task_state": sim_task_state,
}

# ---------- JSON-RPC over stdio ----------
_ERROR_STATUSES = {"PIPELINE_ERROR", "RESEARCH_ERROR", "KIT_UNAVAILABLE", "NO_LOG"}


def _is_error(data):
    """业务失败也要让 MCP 客户端看到 isError,否则错误会被当成成功返回。"""
    if not isinstance(data, dict):
        return False
    if data.get("fail_closed") is True:
        return True
    if isinstance(data.get("error"), str) and data["error"].strip():
        return True
    status = data.get("status")
    return status in _ERROR_STATUSES or (isinstance(status, str) and status.endswith("_ERROR"))


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
            "serverInfo": {"name": "mdsim", "version": "0.1.0"}}})
    elif method in ("notifications/initialized", "initialized"):
        pass
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name"); args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}})
            return
        try:
            data = fn(args)
            _toolcall_log(name, args, data)
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                      "structuredContent": data}
            if _is_error(data):
                result["isError"] = True
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as ex:
            log("tool error: " + traceback.format_exc())
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "工具执行出错: %s" % ex}], "isError": True}})
    else:
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}})

def main():
    log("mdsim MCP server 启动(只读,mode=%s)" % MODE)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as ex:
            log("bad json: %s" % ex); continue
        try:
            handle(msg)
        except Exception:
            log("handle error: " + traceback.format_exc())

if __name__ == "__main__":
    main()
