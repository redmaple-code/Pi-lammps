# -*- coding: utf-8 -*-
"""
sim_hpc_submit 的确定性作业脚本渲染器(预留接口:契约先行,执行受控)
==================================================================
定位:超算(slurm/pbs)提交是【写操作】,本模块只做确定性渲染与校验,
绝不直接提交——返回脚本文本与提交命令,真正提交由会话模型经唯一写门
sim_exec 执行(sbatch/qsub 须在 claude_interface.py 白名单内,
把 sbatch 加进白名单属于人工决策,不是本模块的事)。

设计纪律(与 pipeline_check/model_research 一致):
- build_script / build_submit_cmd 是纯函数,同样输入同样输出;
- run() 任何异常都返回 {"status": "HPC_ERROR", ...},绝不抛给 MCP 层;
- dry_run 默认 true:只渲染不写盘不提交;
- dry_run=false 且 md_config.json 未显式 hpc.enabled=true → DISABLED。

迁移注意:调度器自动探测(command -v sbatch/qsub),分区/账号等
集群专属默认值放 md_config.json 的 hpc.*,换集群只改配置。
"""
import os
import re
import shutil

JOB_NAME_RE = re.compile(r"^[\w.-]{1,64}$")
# d-hh:mm:ss(d- 前缀时 hh<24)或 hh:mm:ss(无天数时 hh 可超 24,slurm 允许)
WALLTIME_RE = re.compile(r"^(\d{1,3}-([01]\d|2[0-3])|\d{1,4}):[0-5]\d:[0-5]\d$")
SAFE_TOKEN_RE = re.compile(r"^[\w.-]{1,64}$")     # partition/account/job_name
SAFE_PATH_RE = re.compile(r"^[\w./~+-]{1,256}$")  # workdir(禁空白/换行/分号,防注入伪指令行)
SCHEDULERS = ("slurm", "pbs")


def detect_scheduler():
    """按 PATH 里的提交命令探测调度器;都没有返回 None。"""
    if shutil.which("sbatch"):
        return "slurm"
    if shutil.which("qsub"):
        return "pbs"
    return None


def _validate(workdir, command, cores, nodes, walltime, scheduler):
    if not workdir or not str(workdir).strip():
        return "缺少 workdir(作业工作目录)"
    if not SAFE_PATH_RE.match(str(workdir)):
        return "workdir 含非法字符(禁空白/换行/分号): %r" % workdir
    if not command or not str(command).strip():
        return "缺少 command(作业内要执行的命令,如 mpirun -np 32 lmp_mpi -in production.in)"
    for name, val in (("cores", cores), ("nodes", nodes)):
        try:
            if int(val) < 1:
                return "%s 必须 ≥1" % name
        except (TypeError, ValueError):
            return "%s 必须是正整数: %r" % (name, val)
    if int(nodes) > int(cores):
        return "nodes(%s) 不能大于 cores(%s),否则 ppn=0 非法" % (nodes, cores)
    if int(cores) % int(nodes) != 0:
        return "cores(%s) 须能被 nodes(%s) 整除,否则 ppn 截断会少申请核" % (cores, nodes)
    if walltime and not WALLTIME_RE.match(str(walltime)):
        return "walltime 格式须为 hh:mm:ss 或 d-hh:mm:ss(hh<24): %r" % walltime
    if scheduler and scheduler not in SCHEDULERS:
        return "scheduler 只支持 slurm/pbs: %r" % scheduler
    return None


def build_script(scheduler, workdir, command, cores=32, nodes=1,
                 walltime="24:00:00", partition="", account="", job_name="mdsim"):
    """渲染作业脚本(slurm sbatch / pbs qsub)。纯函数,不写盘。"""
    if scheduler == "slurm":
        lines = ["#!/bin/bash",
                 "#SBATCH --job-name=%s" % job_name,
                 "#SBATCH --nodes=%d" % int(nodes),
                 "#SBATCH --ntasks=%d" % int(cores),
                 "#SBATCH --time=%s" % walltime,
                 "#SBATCH --output=job_%j.out",
                 "#SBATCH --error=job_%j.err"]
        if partition:
            lines.append("#SBATCH --partition=%s" % partition)
        if account:
            lines.append("#SBATCH --account=%s" % account)
        lines += ["", "cd %s" % workdir, "", str(command), ""]
        return "\n".join(lines)
    # pbs
    lines = ["#!/bin/bash",
             "#PBS -N %s" % job_name,
             "#PBS -l nodes=%d:ppn=%d" % (int(nodes), int(cores) // int(nodes)),
             "#PBS -l walltime=%s" % walltime.replace("-", ":"),
             "#PBS -o job_$PBS_JOBID.out",
             "#PBS -e job_$PBS_JOBID.err"]
    if partition:
        lines.append("#PBS -q %s" % partition)
    if account:
        lines.append("#PBS -A %s" % account)
    lines += ["", "cd %s" % workdir, "", str(command), ""]
    return "\n".join(lines)


def build_submit_cmd(scheduler, script_path):
    """提交命令(渲染结果,不执行)。真正提交走 sim_exec 写门。"""
    return ["sbatch" if scheduler == "slurm" else "qsub", script_path]


def run(args, cfg_hpc=None):
    """MCP 入口。cfg_hpc 来自 md_config.json 的 hpc 节;永不抛。"""
    try:
        cfg_hpc = cfg_hpc or {}
        workdir = str(args.get("workdir", "")).strip()
        command = str(args.get("command", "")).strip()
        cores = args.get("cores", 32)
        nodes = args.get("nodes", 1)
        walltime = str(args.get("walltime", "") or cfg_hpc.get("default_walltime", "24:00:00"))
        partition = str(args.get("partition", "") or cfg_hpc.get("default_partition", ""))
        account = str(args.get("account", "") or cfg_hpc.get("default_account", ""))
        case_id = str(args.get("case_id", "")).strip()
        job_name = str(args.get("job_name", "")).strip() or (
            "mdsim_%s" % case_id if case_id and JOB_NAME_RE.match(case_id) else "mdsim")
        dry_run = bool(args.get("dry_run", True))
        scheduler = str(args.get("scheduler", "") or cfg_hpc.get("scheduler", "auto")).strip()
        if scheduler == "auto":
            scheduler = detect_scheduler() or "slurm"  # 无调度器环境按 slurm 渲染(dry_run 预览用)

        err = _validate(workdir, command, cores, nodes, walltime, scheduler)
        if err:
            return {"status": "HPC_ERROR", "error": err}
        if not JOB_NAME_RE.match(job_name):
            return {"status": "HPC_ERROR",
                    "error": "job_name 含非法字符(只允许字母数字._-,≤64): %r" % job_name}
        for label, tok in (("partition", partition), ("account", account)):
            if tok and not SAFE_TOKEN_RE.match(tok):
                return {"status": "HPC_ERROR",
                        "error": "%s 含非法字符(只允许字母数字._-): %r" % (label, tok)}

        script_name = "job_%s.sh" % (case_id if case_id and JOB_NAME_RE.match(case_id) else "mdsim")
        script_path = os.path.join(workdir, script_name)
        script = build_script(scheduler, workdir, command, cores, nodes,
                              walltime, partition, account, job_name)
        submit_cmd = build_submit_cmd(scheduler, script_path)
        base = {"scheduler": scheduler, "job_name": job_name,
                "script_name": script_name, "script": script,
                "submit_cmd": " ".join(submit_cmd),
                "detected_scheduler": detect_scheduler(),
                "source": "hpc_submit"}
        if dry_run:
            base["status"] = "DRY_RUN"
            base["note"] = ("预览模式:脚本未写盘未提交。落地步骤:① 用户确认方案;② 用 write 工具把 script "
                            "写入 %s;③ 经 sim_exec 执行 '%s'(sbatch/qsub 须在审计白名单内)。"
                            % (script_path, base["submit_cmd"]))
            return base
        if not cfg_hpc.get("enabled"):
            base["status"] = "DISABLED"
            base["error"] = ("超算提交未启用:md_config.json 的 hpc.enabled!=true。"
                             "启用属人工决策(且 sbatch/qsub 需加入 claude_interface.py 白名单)。")
            return base
        # enabled 且非 dry_run:仍然不直接提交——只确认可提交,执行交给 sim_exec
        base["status"] = "READY"
        base["note"] = ("已启用。本工具仍不直接提交:请用 write 工具落盘脚本后,经 sim_exec 执行提交命令, "
                        "提交后用 sim_status/巡检盯作业。")
        return base
    except Exception as ex:  # noqa: BLE001 —— 接口不能让 MCP 层崩
        return {"status": "HPC_ERROR", "error": "%s: %s" % (type(ex).__name__, ex)}
