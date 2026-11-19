"""Summary step — deliberately contains NO policy rules.

Owner instruction (2026-10-29): no rules may be injected into this layer without
explicit approval. This module only aggregates the consistency items produced by
consistency.py into a verdict; it does not add findings, severities or policy
rules of its own.

Adding any rule later requires:
  1) owner approval,
  2) a data file under review_layer/rules/ (explicitly loaded),
  3) tests covering it.
"""
from __future__ import annotations

from .models import ReviewReport


def summarize(report: ReviewReport) -> ReviewReport:
    """Deterministic summary of consistency verdicts; no policy findings."""
    report.findings = []
    items = report.consistency
    if any(i.verdict == "invalid" for i in items):
        report.verdict = "FAIL"
    elif any(i.verdict == "plan_conflict" for i in items):
        report.verdict = "NEEDS_REVISION"
    elif not items or any(i.verdict not in ("ok", "questionable", "unproven", "invalid", "plan_conflict") for i in items) or any(i.verdict in ("questionable", "unproven") for i in items):
        report.verdict = "REVIEW"
    else:
        report.verdict = "PASS"
    report.notes.append(
        "本层未配置任何审核规则（按所有者指示）；verdict 仅为一致性条目判定的确定性汇总。"
        "任何规则须经所有者批准后以数据文件注入。")
    return report
