# -*- coding: utf-8 -*-
"""
sim_pipeline_check 的确定性流水线(流程写死在代码里,不靠模型自觉)
==================================================================
一步完成: 校验输入 → 原子写 input.json → 子进程跑 monitor_agent.py(v3.4)
→ 读 result.json → 与上一快照对比生成 deltas → 原子更新快照 → 返回结构化结果。

设计纪律:
- 任何异常都返回 {"status": "PIPELINE_ERROR", ...},绝不抛给 MCP 层(handle 另有兜底);
- 原子写一律 tmp + os.replace(与 deploy kit 的 bus 原子性约定一致);
- monitor_agent.py 本体不改(只被封装),便于演化机制整体替换;
- 每次调用终态追加 events/pipeline.jsonl(覆盖式快照会冲掉失效史,演化层靠事件流);
  snap_<case>.json 快照保留"每 case 最新"语义供 md_mcp 修复研讨读。
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid

VALID_STATUSES = {"RUNNING", "WARNING", "CRITICAL", "DONE", "UNKNOWN", "KIT_UNAVAILABLE"}
CASE_ID_RE = re.compile(r"^[\w.-]+$")  # 防 snapshot 文件名路径逃逸

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR_AGENT = os.path.join(
    os.path.dirname(HERE),  # dsh-agent/
    "..", "..", "..", "agent_deploy_kit_20260630",
    "agents", "runtime", "v3.3", "monitor_agent.py",
)
_cfg_path = os.path.join(HERE, "md_config.json")


def monitor_agent_path():
    """按调用现解析:环境变量 > md_config.local_base > 相对兜底。

    调用时解析(不是 import 时)才能让迁移机器与测试改 MDSIM_KIT_BASE 即生效;
    MONITOR_AGENT 常量保留为猴子补丁入口。
    """
    candidates = [os.environ.get("MDSIM_KIT_BASE", "")]
    try:
        if os.path.exists(_cfg_path):
            with open(_cfg_path, encoding="utf-8") as stream:
                candidates.append(json.load(stream).get("local_base", ""))
    except Exception:
        pass
    for base in candidates:
        if base:
            candidate = os.path.join(base, "agents", "runtime", "v3.3", "monitor_agent.py")
            if os.path.exists(candidate):
                return candidate
    return MONITOR_AGENT

import paths

# 常量名保留为测试猴子补丁入口(test_md_mcp 改写它重定向 workdir);
# import 时按当时 env/config 解析一次——进程内改 MDSIM_DATA_ROOT 须改本常量才生效(测试即如此做)
DEFAULT_WORKDIR = paths.pipeline_scratch_dir()
# 事件流(2026-09-05):每次调用终态追加一条到 events/pipeline.jsonl——
# 覆盖式 snap 快照会被后一次巡检冲掉,失效史不可丢(演化层复发统计的数据源);
# snap 快照保留(md_mcp 修复研讨读"每 case 最新"语义)。常量同为测试猴子补丁入口。
PIPELINE_EVENTS_LOG = paths.pipeline_events_log()
TIMEOUT_SEC = 90


def _resolve_schedule(case_id):
    """温度程序注入 in.json(2026-08-29 按项目统一):registry 里 case→project 命中用
    项目级 schedules[project],否则全局 schedule;都没有返回 None(monitor_agent 回退内置)。
    任何解析失败都不许阻断巡检——调度信息是增强,不是必需。"""
    project = None
    try:
        dash = os.path.join(os.path.dirname(HERE), "dashboard")
        if dash not in sys.path:
            sys.path.append(dash)  # 末尾追加:不遮蔽 mcp/ 同名模块(四轮对抗 N3)
        import registry as reg
        for j in reg.load()["jobs"]:
            if j.get("case_id") == case_id:
                project = j.get("project")
                break
    except Exception:
        pass
    try:
        return paths.load_schedule(project)
    except Exception:
        return None


def _atomic_write_json(path, data):
    # tmp 名带 uuid:同 case 并发调用共享 snap 路径,只有 pid 会互抢 os.replace(五轮对抗)
    tmp = "%s.tmp.%d_%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resolve_log_file(prod_dir, log_file):
    """log_file 缺省时挑 prod_dir 里最新的日志文件(*.log / log.* / *.out);返回绝对路径或 None。"""
    if log_file:
        p = log_file if os.path.isabs(log_file) else os.path.join(prod_dir, log_file)
        return os.path.abspath(p) if os.path.exists(p) else None
    if not os.path.isdir(prod_dir):
        return None
    # thermo 流优先(2026-09-17 对抗压测抓获):本流水线 thermo 在 mpirun stdout 重定向
    # (production.stdout)与 log.lammps;*.out 里的 species.out 等组分文件列布局不同,
    # 按 mtime 最新误选会解析出假温度(species.out 第二列当 Temp → 假 CRITICAL)。
    for name in ("production.stdout", "log.lammps"):
        p = os.path.join(prod_dir, name)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return os.path.abspath(p)
    candidates = [
        os.path.join(prod_dir, name)
        for name in os.listdir(prod_dir)
        if (name.endswith(".log") or name.startswith("log."))
        and os.path.isfile(os.path.join(prod_dir, name))
    ]
    if not candidates:
        return None
    return os.path.abspath(max(candidates, key=os.path.getmtime))


def _deltas(previous, current):
    """与上一快照对比,生成 step/temp/risk_score 变化;无快照返回 None。"""
    if not previous:
        return None
    out = {}
    for key in ("step", "temp", "risk_score"):
        old, new = previous.get(key), current.get(key)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            out[key] = {"previous": old, "current": new, "delta": round(new - old, 4)}
    if previous.get("status") != current.get("status"):
        out["status"] = {"previous": previous.get("status"), "current": current.get("status")}
    out["elapsed_sec"] = round(time.time() - previous.get("_snapshot_ts", time.time()), 1)
    return out


def _emit_event(case_id, result):
    """每次调用终态追加一行事件 jsonl(含 PIPELINE_ERROR/NO_LOG——它们从不写 snap)。
    永不抛:事件流是增强,绝不允许阻断巡检主链。"""
    try:
        ev = {"ts": time.time(), "case_id": str(case_id)[:80],
              "status": result.get("status", "UNKNOWN")}
        for k in ("step", "temp", "risk_score"):
            v = result.get(k)
            if isinstance(v, (int, float)):
                ev[k] = v
        if result.get("risk_factors"):
            ev["risk_factors"] = [str(x)[:60] for x in result["risk_factors"]][:8]
        if result.get("error"):
            ev["error"] = str(result["error"])[:200]
        path = PIPELINE_EVENTS_LOG
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run(prod_dir, log_file=None, case_id="unknown", workdir=None):
    """跑一遍巡检流水线,返回 monitor_agent 契约 + deltas + source 标注。永不抛异常。
    终态一律追加事件流(单出口封装:未来新增提前返回点也不会漏记账)。"""
    try:
        result = _run_impl(prod_dir, log_file, case_id, workdir)
    except Exception as ex:  # 双保险:_run_impl 已兜底,此处防未来改动破"不抛"约
        result = {"status": "PIPELINE_ERROR",
                  "error": "%s: %s" % (type(ex).__name__, ex), "case_id": case_id}
    _emit_event(case_id, result)
    return result


def _run_impl(prod_dir, log_file=None, case_id="unknown", workdir=None):
    wd = None
    # pid+uuid 后缀:同进程同毫秒的两线程不得共享 in/out 路径互抢(三轮对抗 R1)
    tag = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:8])
    try:
        target = monitor_agent_path()
        if not os.path.exists(target):
            return {"status": "KIT_UNAVAILABLE", "case_id": case_id,
                    "error": "monitor_agent.py 不存在: %s" % target,
                    "missing_kit": True,
                    "remediation": "安装 agent_deploy_kit 或设置 MDSIM_KIT_BASE"}
        if not prod_dir:
            return {"status": "PIPELINE_ERROR", "error": "缺少 prod_dir 且无法从状态兜底"}
        if not CASE_ID_RE.match(case_id):
            return {"status": "PIPELINE_ERROR",
                    "error": "case_id 含非法字符(只允许字母数字._-): %r" % case_id}
        prod_dir = os.path.abspath(prod_dir)
        resolved_log = _resolve_log_file(prod_dir, log_file)
        if resolved_log is None:
            return {"status": "NO_LOG",
                    "error": "prod_dir 下找不到 log 文件: %s" % prod_dir,
                    "case_id": case_id, "source": "pipeline_check"}

        wd = workdir or DEFAULT_WORKDIR
        os.makedirs(wd, exist_ok=True)
        ts = int(time.time() * 1000)
        in_path = os.path.join(wd, "in_%d_%s.json" % (ts, tag))
        out_path = os.path.join(wd, "out_%d_%s.json" % (ts, tag))
        in_data = {"prod_dir": prod_dir, "log_file": resolved_log, "case_id": case_id}
        sched = _resolve_schedule(case_id)
        if sched:
            in_data["schedule"] = sched  # monitor_agent 按项目温度程序判定目标温
        _atomic_write_json(in_path, in_data)

        proc = subprocess.run(
            [sys.executable, target, in_path, out_path],
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
        result = _read_json(out_path)
        if result is None:
            return {"status": "PIPELINE_ERROR",
                    "error": "monitor_agent 未产出可解析结果(rc=%d, stderr=%s)"
                             % (proc.returncode, (proc.stderr or "")[:400]),
                    "case_id": case_id}

        snap_path = os.path.join(wd, "snap_%s.json" % case_id)
        previous = _read_json(snap_path)
        result["deltas"] = _deltas(previous, result)
        result["case_id"] = case_id
        result["log_file"] = resolved_log
        result["source"] = "pipeline_check"
        # 只把状态完好的结果写成下次基线;异常/残缺结果不污染快照
        if result.get("status") in VALID_STATUSES and "step" in result:
            snap = dict(result)
            snap["_snapshot_ts"] = time.time()
            _atomic_write_json(snap_path, snap)

        for stale in (in_path, out_path):
            try:
                os.remove(stale)
            except OSError:
                pass
        return result
    except subprocess.TimeoutExpired:
        _cleanup(wd, tag)  # wd 在 subprocess 之前已赋值(未走到则为 None,_cleanup 直接返回)
        return {"status": "PIPELINE_ERROR",
                "error": "monitor_agent 超时(%ds)" % TIMEOUT_SEC, "case_id": case_id}
    except Exception as ex:  # 兜底: 流水线不能让 MCP 层崩
        _cleanup(wd, tag)  # 非超时异常同样不得泄漏 in_/out_ 临时文件(二轮对抗)
        return {"status": "PIPELINE_ERROR", "error": "%s: %s" % (type(ex).__name__, ex),
                "case_id": case_id}


def _cleanup(workdir, tag=None):
    """异常路径下清理本次调用遗留的 in_*/out_* 临时文件。
    只按本次调用的 pid_uuid tag 精确匹配(不连坐并发线程/进程),兼收无后缀的远古残留。"""
    if not workdir or not os.path.isdir(workdir):
        return
    for name in os.listdir(workdir):
        if not name.startswith(("in_", "out_")):
            continue
        own = tag is not None and ("_%s.json" % tag) in name  # 含 .tmp.<pid> 残件(四轮对抗 N4)
        if own or re.match(r"^(in|out)_\d+\.json$", name):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
