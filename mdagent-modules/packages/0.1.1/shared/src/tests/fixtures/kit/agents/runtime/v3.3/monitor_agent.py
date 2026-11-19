#!/usr/bin/env python3
"""Minimal monitor_agent contract fixture for tests (never used in production).

Usage: python monitor_agent.py <input.json> <output.json>
Input: {"prod_dir", "log_file", "case_id", "schedule"?}
Output: {"status", "step", "temp", "temp_k", "anomalies", "risk_score", "risk_factors"}
"""
import json
import os
import sys


def _read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _atomic_json(path, value):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    os.replace(tmp, path)


def _rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                parts = line.split()
                if len(parts) < 8:
                    continue
                try:
                    rows.append((int(float(parts[0])), float(parts[1])))
                except ValueError:
                    continue
    except OSError:
        return rows
    return rows


def _target_temp(schedule, step):
    for start, end, t_start, t_end in schedule:
        if start <= step <= end:
            if end == start:
                return float(t_end)
            ratio = (step - start) / float(end - start)
            return float(t_start) + (float(t_end) - float(t_start)) * ratio
    return None


def main():
    if len(sys.argv) < 3:
        return 2
    data = _read_json(sys.argv[1])
    log_file = data.get("log_file") or ""
    if log_file and not os.path.isabs(log_file):
        log_file = os.path.join(data.get("prod_dir") or "", log_file)
    rows = _rows(log_file)
    result = {"case_id": data.get("case_id"), "anomalies": [], "risk_factors": [],
              "risk_score": 0, "status": "UNKNOWN"}
    if rows:
        step, temp = rows[-1]
        result.update(status="RUNNING", step=step, temp=temp, temp_k=temp)
        target = _target_temp(data.get("schedule") or [], step)
        if target is not None and abs(temp - target) > 50.0:
            result["status"] = "CRITICAL"
            result["risk_score"] = 80
            result["anomalies"].append({"type": "TEMPERATURE_SUSTAINED",
                                        "detail": "temp=%.1f target=%.1f" % (temp, target)})
            result["risk_factors"].append("temperature_sustained")
    else:
        result["error"] = "no parseable thermo rows"
    _atomic_json(sys.argv[2], result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
