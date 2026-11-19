# -*- coding: utf-8 -*-
"""
content_check —— 数值主张重算对账（证据系统 v2,确定性零 LLM）
================================================================================
定位: 回答"文书里的数字是真的吗"。不接受文书自述——从留档原始产物
(求解器日志等)重新解析计算,与主张值对账,产出不可变 factcheck 记录,
供门禁 R13 消费(验收判据里的数值主张必须 factcheck PASS)。

纪律: 纯 stdlib;解析失败=FAIL 并注明(fail-closed);记录 O_EXCL 不可变;
解析器注册表(数据可扩展): marker_log=从日志标记行取值。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PARSERS = {}


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def register(name):
    def deco(fn):
        PARSERS[name] = fn
        return fn
    return deco


@register("marker_log")
def _marker_log(source):
    """从日志的 EVLIVE_<KEY> 标记行取值: {marker: value}。"""
    fp = source.get("path")
    if not fp or not os.path.isfile(fp):
        return None, "source-missing:%s" % fp
    out = {}
    with open(fp, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if ln.startswith("EVLIVE_"):
                parts = ln.split(None, 1)
                if len(parts) == 2:
                    out[parts[0]] = parts[1].strip()
    return out, None


@register("c11_strain_log")
def _c11_strain_log(source):
    """应变能序列 → 独立重算 C11(GPa)。与驱动解析互为双实现。"""
    fp = source.get("path")
    if not fp or not os.path.isfile(fp):
        return None, "source-missing:%s" % fp
    vol0, energies = None, {}
    with open(fp, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if ln.startswith("EVLIVE_VOL0 "):
                try:
                    vol0 = float(ln.split()[1])
                except (ValueError, IndexError):
                    return None, "vol0-parse"
            elif ln.startswith("EVLIVE_STR_"):
                parts = ln.split()
                if len(parts) >= 2:
                    try:
                        energies[parts[0][len("EVLIVE_STR_"):]] = float(parts[1])
                    except ValueError:
                        pass
    if vol0 is None or "1000" not in energies or len(energies) < 3:
        return None, "strain-series-incomplete"
    e0, vals = energies["1000"], []
    for s, e in energies.items():
        try:
            eps = abs(int(s) / 1000.0 - 1.0)
        except ValueError:
            continue
        if eps > 1e-9:
            vals.append(2.0 * (e - e0) / (vol0 * eps * eps) * 160.2176621)
    if not vals:
        return None, "no-strain-points"
    return {"__value__": sum(vals) / len(vals)}, None


@register("rdf_peak_file")
def _rdf_peak_file(source):
    """ave/time RDF 文件 → 独立重找末帧 g(r) 首峰(Å)。"""
    fp = source.get("path")
    if not fp or not os.path.isfile(fp):
        return None, "source-missing:%s" % fp
    frames, cur = [], None
    with open(fp, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if ln.startswith("#"):
                continue
            parts = ln.split()
            if len(parts) == 2:
                cur = []
                frames.append(cur)
            elif cur is not None and len(parts) >= 4:
                try:
                    cur.append((float(parts[1]), float(parts[2])))
                except ValueError:
                    pass
    if not frames or not frames[-1]:
        return None, "rdf-empty"
    cand = [(r, g) for r, g in frames[-1] if 3.0 <= r <= 5.0]
    if not cand:
        return None, "rdf-window-empty"
    return {"__value__": max(cand, key=lambda x: x[1])[0]}, None


def check(case_id, field, claimed, source, tolerance=None, unit="",
          data_root=None, parser="marker_log"):
    """执行一次重算对账,落不可变 factcheck 记录,返回 (记录, 相对路径)。"""
    import stage_state
    parsed, err = (PARSERS.get(parser) or _marker_log)(source)
    if err:
        rec = {"schema_version": 1, "case_id": case_id, "field": field,
               "claimed": claimed, "measured": None, "tolerance": tolerance,
               "unit": unit, "verdict": "FAIL", "reason": err,
               "source": source, "ts": _now_iso()}
    else:
        marker = source.get("marker") or ("EVLIVE_" + field.upper())
        if "__value__" in (parsed or {}):
            measured = parsed["__value__"]
        else:
            raw = parsed.get(marker)
            try:
                measured = float(str(raw).split()[0])
            except (TypeError, ValueError, IndexError):
                measured = None
        if measured is None:
            verdict, reason = "FAIL", "marker-absent:%s" % marker
        elif tolerance is None:
            verdict, reason = ("PASS" if measured == float(claimed)
                               else "FAIL"), "no-tolerance-strict-equal"
        else:
            diff = abs(measured - float(claimed))
            verdict = "PASS" if diff <= float(tolerance) else "FAIL"
            reason = ("|%.6g-%.6g|=%.3g %s tol %.3g" % (
                measured, float(claimed), diff,
                "≤" if diff <= float(tolerance) else ">", float(tolerance)))
        rec = {"schema_version": 1, "case_id": case_id, "field": field,
               "claimed": claimed, "measured": measured,
               "tolerance": tolerance, "unit": unit, "verdict": verdict,
               "reason": reason, "source": source, "ts": _now_iso()}
    d = os.path.join(stage_state.task_dir(case_id, data_root), "factchecks")
    os.makedirs(d, exist_ok=True)
    name = "%s-%s.json" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                           field.replace(" ", "-")[:40])
    fp = os.path.join(d, name)
    try:
        with open(fp, "x", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        with open(fp[:-5] + "-%d.json" % os.getpid(), "x", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
            name = os.path.basename(f.name)
    return rec, os.path.join("factchecks", name).replace("\\", "/")


def read_factcheck(case_id, rel_ref, data_root=None):
    import stage_state
    fp = os.path.join(stage_state.task_dir(case_id, data_root), rel_ref)
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
