#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""compute_run —— E2E 用真实计算体(域无关):蒙特卡洛估算圆周率。
EXEC 段由 run_e2e.py 经子进程真实执行本脚本,输出结果 JSON(确定性种子)。
用法: python compute_run.py <N> <seed> <out.json>"""
import json
import random
import sys


def main():
    n, seed, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    rng = random.Random(seed)
    inside = sum(1 for _ in range(n)
                 if (rng.random() ** 2 + rng.random() ** 2) <= 1.0)
    pi_hat = 4.0 * inside / n
    doc = {"n": n, "seed": seed, "pi_hat": pi_hat,
           "abs_error": abs(pi_hat - 3.141592653589793)}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(json.dumps(doc))


if __name__ == "__main__":
    main()
