# -*- coding: utf-8 -*-
"""测试夹具：为某阶段填充完整核查面（E6 之外的规则测试用）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "evolution"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mcp"))

import surface  # noqa: E402


def fill_phase(data_root, case_id, phase, paper="paper:10.1234/fixture"):
    """把指定阶段的核查面全部填成有据/N/A（测试默认值）。"""
    items = [i for i in surface.load_items(data_root).values()
             if str(i.get("phase")) == str(phase)]
    for it in items:
        if it.get("na_allowed"):
            reason = ("静态最小化：无时间步长/无系综/无控温/无预平衡/无初速"
                      if it.get("na_reason_pattern") else "测试默认：该阶段不适用")
            surface.record_coverage(case_id, it["id"], "na",
                                    reason=reason, data_root=data_root)
        else:
            surface.record_coverage(case_id, it["id"], "evidenced",
                                    evidence=[paper], answer="测试确认值 1（满足该条目）",
                                    data_root=data_root)
    return len(items)


def fill_all(data_root, case_id, paper="paper:10.1234/fixture"):
    for phase in ("design", "build", "exec", "analyze"):
        fill_phase(data_root, case_id, phase, paper=paper)
