#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""persona 两入口逐字一致校验(证据系统 P0)。

抽取 agent.cordis.yml 与 headless.patch.yml 中的【阶段纪律(证据门)】块
(至【回答风格】前),空白归一后逐字比对。退出码 0=一致,1=不一致,2=块缺失。
用法: python scripts/check_persona_sync.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FILES = [os.path.join(ROOT, "presets", "md-sim-assistant", "agent.cordis.yml"),
         os.path.join(ROOT, "md-sim-assistant.headless.patch.yml")]
BEGIN, END = "【阶段纪律(证据门)】", "【回答风格】"


def extract(fp):
    with open(fp, "r", encoding="utf-8") as f:
        text = f.read()
    i = text.find(BEGIN)
    if i < 0:
        return None
    j = text.find(END, i)
    if j < 0:
        return None
    block = text[i:j]
    # 归一:去行首缩进/YAML 折叠引入的差异,压空白
    return re.sub(r"\s+", " ", block).strip()


def main():
    blocks = [extract(fp) for fp in FILES]
    for fp, b in zip(FILES, blocks):
        if b is None:
            print("[persona-sync] 块缺失: %s" % fp)
            return 2
    if blocks[0] != blocks[1]:
        print("[persona-sync] 两入口阶段纪律块不一致!")
        print("--- cordis  ---\n%s" % blocks[0][:600])
        print("--- headless ---\n%s" % blocks[1][:600])
        return 1
    print("[persona-sync] OK: 两入口阶段纪律(证据门)块逐字一致(%d 字符)"
          % len(blocks[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
