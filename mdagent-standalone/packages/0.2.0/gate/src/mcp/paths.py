# -*- coding: utf-8 -*-
"""
集中路径解析 —— 所有运行时数据文件路径的唯一出处（2026-08-29 数据根外置）
=====================================================================
解析优先级：MDSIM_DATA_ROOT 环境变量 > md_config.json["data_root"] > 默认 <代码根父目录>/mdsim-data
注意：每次调用动态读 env，不做模块级缓存 —— 测试用 mock.patch.dict(os.environ) 重定向。
JS 侧（flow-gate 插件）无代码父目录概念，靠 run-dsh.sh export MDSIM_DATA_ROOT 保证 env 恒存在。
"""
import os, json, math, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_ROOT = os.path.dirname(HERE)  # dsh-agent/


def _cfg():
    try:
        with open(os.path.join(HERE, "md_config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _pick(value):
    """空白串视为未设置;~ 展开为用户家目录(2026-08-29 对抗审计 F1/F3)。
    非字符串配置值(如 data_root: 123)按未设置处理并告警,不静默 str() 化(三轮对抗)。"""
    if value is None:
        return None
    if not isinstance(value, str):
        print("[paths] data_root 值类型非法(%s),按未设置处理" % type(value).__name__,
              file=sys.stderr)
        return None
    v = os.path.expanduser(value.strip())
    return v or None


def data_root():
    raw = (_pick(os.environ.get("MDSIM_DATA_ROOT"))
           or _pick(_cfg().get("data_root"))
           or os.path.join(os.path.dirname(CODE_ROOT), "mdsim-data"))
    if not os.path.isabs(raw):
        # 相对值锚定代码根父目录(与默认数据根同层),不随 cwd 漂移(对抗审计 F2)
        raw = os.path.join(os.path.dirname(CODE_ROOT), raw)
    return os.path.abspath(raw)


def _sub(name, make=True):
    p = os.path.join(data_root(), name)
    if make:
        os.makedirs(p, exist_ok=True)
    return p


def events_dir(make=True):
    return _sub("events", make)


def state_dir(make=True):
    return _sub("state", make)


def memory_dir(make=True):
    return _sub("memory", make)


def scratch_dir(make=True):
    return _sub("scratch", make)


def pipeline_scratch_dir(make=True):
    p = os.path.join(scratch_dir(False), "pipeline")
    if make:
        os.makedirs(p, exist_ok=True)
    return p


def dsh_home(make=True):
    return _sub("dsh-home", make)


def hpc_cache_dir(make=True):
    p = os.path.join(state_dir(False), "hpc_cache")
    if make:
        os.makedirs(p, exist_ok=True)
    return p


def registry_file():
    return os.path.join(state_dir(), "registry.json")


# 事件流文件
def flowgate_log():
    return os.path.join(events_dir(), "flowgate.jsonl")


def pipeline_events_log():
    return os.path.join(events_dir(), "pipeline.jsonl")


def toolcalls_log():
    return os.path.join(events_dir(), "toolcalls.jsonl")


def friction_log():
    return os.path.join(events_dir(), "friction.jsonl")


def _coerce_schedule(raw):
    """[[s, e, t_start, t_end], ...] 形式校验 + 归一化为 float 元组;不合法返回 None。
    段必须是 list/tuple(字符串段会被逐字符 float 化成合法假象,四轮对抗);NaN/inf 拒收。"""
    if not isinstance(raw, list) or not raw:
        return None
    try:
        segs = [tuple(float(x) for x in seg) for seg in raw
                if isinstance(seg, (list, tuple))]
    except (TypeError, ValueError):
        return None
    if len(segs) != len(raw) or not all(len(s) == 4 for s in segs):
        return None
    if not all(math.isfinite(x) for s in segs for x in s):
        return None
    return segs


def load_schedule(project=None):
    """温度程序唯一出处(md_config.json):schedules[project] > schedule;都没有返回 None,
    调用方回退各自内置默认(web_monitor 斜坡 / monitor_agent 平台)。
    配置写坏(非四元段/schedules 非 dict)按未设置处理,绝不让坏配置打崩巡检
    (2026-08-29 按项目统一;schedules 类型守卫为四轮对抗)。"""
    cfg = _cfg()
    sched = None
    schedules = cfg.get("schedules")
    if project and isinstance(schedules, dict):
        sched = _coerce_schedule(schedules.get(project))
    return sched or _coerce_schedule(cfg.get("schedule"))
