"""Data models for the design-review layer."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Artifact:
    kind: str                      # intent | infile | gate
    path: str
    sha256: str
    source: str = "mdagent"        # mdagent | fixture | unknown


@dataclass
class ConsistencyItem:
    id: str
    element: str                   # 系综 / 时间步 / 步数 / 基本设置 / 命令用途 / 观测量 ...
    claim: str                     # 目标要求或方案声明（被核对的对象）
    detail: str
    anchors: list[int] = field(default_factory=list)
    verdict: str = "ok"            # ok | questionable | unproven | invalid | plan_conflict
    evidence_refs: list[str] = field(default_factory=list)   # 经验/证据条目 id（后续接入）
    source: str = "rule"           # rule | knowledge | experience


@dataclass
class ReviewFinding:
    id: str
    rule: str                      # R-PROV / R-VERDICT / R-CMD / R-EVID / R-COMPLETE
    severity: str                  # blocker | major | minor | info
    title: str
    detail: str
    refs: list[str] = field(default_factory=list)


@dataclass
class ReviewReport:
    case_id: str
    generated_at: str
    artifacts: list[Artifact]
    consistency: list[ConsistencyItem]
    findings: list[ReviewFinding]
    gate_status: str               # 来自 validate 结果（只读）
    verdict: str                   # PASS | REVIEW | NEEDS_REVISION | FAIL
    experiment_plan: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["summary"] = {
            "items": len(self.consistency),
            "ok": sum(1 for i in self.consistency if i.verdict == "ok"),
            "questionable": sum(1 for i in self.consistency if i.verdict == "questionable"),
            "unproven": sum(1 for i in self.consistency if i.verdict == "unproven"),
            "invalid": sum(1 for i in self.consistency if i.verdict == "invalid"),
            "plan_conflict": sum(1 for i in self.consistency if i.verdict == "plan_conflict"),
            "findings": len(self.findings),
            "blockers": sum(1 for f in self.findings if f.severity == "blocker"),
            "gate": self.gate_status,
        }
        return d
