#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""atoms.jsonl 三轴标签一次性回填(v1.6.0)。

逐条加 axes 字段(已有合法 axes 且不加 --force 时跳过),其余字段原样保留。

对抗加固(2026-09-18 红队μ/ξ 后):
  - 全程持 ATOMS.lock 文件锁,单次读取同时供备份与重写——
    读写窗口内 record_atom 的追加不再被旧快照回滚(原双读无锁丢原子);
  - 坏行/无 kind 行原样回写(原静默丢弃=数据丢失);
  - json.loads 严格模式拒 NaN/Infinity(原 loads 收 dumps 吐,文件对 jq 永久损坏);
  - 写入前 lstat 拒非正则文件(软链跟随会把任意可读文件拷进备份);
  - 备份 os.open O_EXCL 0o640(不依赖 umask/ACL 环境),保留最近 10 份。

用法:
  python3.11 evolution/backfill_tags.py            # dry-run 报告
  python3.11 evolution/backfill_tags.py --write    # 实写(带备份)
  python3.11 evolution/backfill_tags.py --write --force   # 强制重打已有 axes
"""
import argparse
import collections
import glob
import json
import os
import stat
import sys
import time
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import memory  # noqa: E402
import tagging  # noqa: E402

BACKUP_KEEP = 10


def _reject_constant(s):
    raise ValueError("非法 JSON 常量: %s" % s)


def _parse_rows(src):
    """逐行解析。返回 (rows, kept_lines):rows 是合法原子(dict),
    kept_lines 与源行一一对应——原子行占位下标,坏行保留原文。"""
    rows, kept, bad = [], [], 0
    for ln in src.splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            a = json.loads(s, parse_constant=_reject_constant)
        except ValueError:
            bad += 1
            kept.append(ln if ln.endswith("\n") else ln + "\n")  # 原样保留
            continue
        if isinstance(a, dict) and a.get("kind"):
            rows.append(a)
            kept.append(None)  # 占位,写盘时用打标后的 JSON
        else:
            bad += 1
            kept.append(ln if ln.endswith("\n") else ln + "\n")
    return rows, kept, bad


def _tag_rows(rows, force):
    changed = 0
    stats = {"topic": collections.Counter(), "topic_candidates": collections.Counter(),
             "method": collections.Counter(), "ops": collections.Counter(),
             "exam_ready": 0, "untagged": 0, "method_flood": 0}
    for a in rows:
        axes = a.get("axes")
        if isinstance(axes, dict) and axes.get("method") is not None and not force:
            pass  # 已有合法 axes,沿用
        else:
            axes = tagging.tag_atom(a.get("kind"), a.get("fact", ""),
                                    a.get("refs"))
            if axes != a.get("axes"):
                changed += 1
            a["axes"] = axes
        for ax in ("topic", "topic_candidates", "method", "ops"):
            for t in axes.get(ax) or []:
                stats[ax][t] += 1
        if axes.get("exam_ready"):
            stats["exam_ready"] += 1
        if len(axes.get("method") or []) >= 4:
            stats["method_flood"] += 1  # 红队ν:单原子挂满标签=灌水信号,告警
        if not any(axes.get(ax) for ax in ("topic", "topic_candidates",
                                           "method", "ops")):
            stats["untagged"] += 1
    return changed, stats


def _report(rows, bad, changed, stats):
    print("原子总数: %d(坏行保留 %d), 本次重打标: %d" % (len(rows), bad, changed))
    print("可考题化(exam_ready): %d, 三轴全空: %d, 标签灌水(单原子≥4 method): %d"
          % (stats["exam_ready"], stats["untagged"], stats["method_flood"]))
    for ax in ("topic", "topic_candidates", "method", "ops"):
        print("\n[%s] 命中分布:" % ax)
        for tag, n in stats[ax].most_common(20):
            print("  %4d  %s" % (n, tag))


def _write_locked(src, rows, kept):
    """锁内:备份(单次读取的 src)+ 全量重写(原子行重打标,坏行原文保留)。"""
    st = os.lstat(memory.ATOMS)
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit("拒绝写入: %s 不是正则文件(可能软链攻击)" % memory.ATOMS)
    bak = "%s.bak_tags_%s_%s" % (memory.ATOMS, time.strftime("%Y%m%d_%H%M%S"),
                                 uuid.uuid4().hex[:6])
    fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(src)
    ri = 0
    parts = []
    for keep in kept:
        if keep is not None:
            parts.append(keep)
        else:
            parts.append(json.dumps(rows[ri], ensure_ascii=False,
                                    allow_nan=False) + "\n")
            ri += 1
    memory._atomic_write(memory.ATOMS, "".join(parts))
    # 备份 retention:只留最近 BACKUP_KEEP 份
    baks = sorted(glob.glob(memory.ATOMS + ".bak_tags_*"),
                  key=os.path.getmtime)
    for old in baks[:-BACKUP_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return bak


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="实写(默认 dry-run)")
    ap.add_argument("--force", action="store_true", help="已有 axes 也重打")
    args = ap.parse_args(argv)

    if not os.path.exists(memory.ATOMS):
        print("atoms.jsonl 不存在: %s" % memory.ATOMS)
        return 1
    with open(memory.ATOMS, encoding="utf-8", errors="replace") as f:
        src = f.read()
    rows, kept, bad = _parse_rows(src)
    changed, stats = _tag_rows(rows, args.force)
    _report(rows, bad, changed, stats)

    if not args.write:
        print("\ndry-run,未写盘。加 --write 实写(自动备份)。")
        return 0
    if fcntl is not None:
        # 与 record_atom 同一把锁:锁内备份+重写,并发入账被挡在锁外
        with open(memory.ATOMS + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # 锁内重读:等锁期间可能有新原子入账,以最新内容为准重算
            with open(memory.ATOMS, encoding="utf-8", errors="replace") as f:
                src = f.read()
            rows, kept, bad = _parse_rows(src)
            changed, stats = _tag_rows(rows, args.force)
            _report(rows, bad, changed, stats)
            bak = _write_locked(src, rows, kept)
    else:
        bak = _write_locked(src, rows, kept)
    print("\n已写盘 %d 条,备份在 %s" % (len(rows), bak))
    return 0


if __name__ == "__main__":
    sys.exit(main())
