# 设计：一致性校核与 UKB 知识库耦合 v0.1

> 日期：2026-11-01。状态：**已实施并验证**（本机 UKB，`MDagent-evidence-v3/data/ukb`）。
> 上游：`方案_UKB嵌入MDagent替换知识库_v1.0.md`（v1.1 已实施）；本层不修改 UKB 冻结包，只通过其 API 读写。

## 1. 目标

让一致性校核（design-review-layer）的知识、经验、证据只存 UKB、只从 UKB 取；本地 JSON 仅作种子/回退。

## 2. 存储映射

| 类别 | 内容 | UKB 类型 | 标题前缀 | 当前条数 |
|---|---|---|---|---|
| 知识类 | 9 维审核基线 | `rule` | `基线维度 ` | 9 |
| 经验类 | 常见问题/已知局限（12 条） | `lesson` | `常见问题 ` | 12 |
| 证据类 | 五案例开放审查点（AI 发现） | `observation` | `审查点 ` | 25 |

- 每条均带来源：`knowledge_entry`（基线/常见问题）或 `run_artifact`（审查点，指向 discovery JSON）。
- 标签：`topic/general` + `stage/planning|validation`。
- 经验条目保留处置（explained/record_required）、匹配词、解释与声明。

## 3. 读写路径

```
common_issues.load() ──┐
discovery.BASELINE ────┼── ukb_store.load_baseline()/load_common_issues()
                       │        └─ UKB catalog/read（profile=agent）
写：ukb_store.propose_approve()
        └─ UKB propose → 机械预审（结构检查）→ 管理员批准（token 本地 0600）
```

- 开关：`REVIEW_KB=legacy` 或 UKB 不可用时自动回退本地 JSON；默认 `REVIEW_KB=ukb`。
- 数据根：`REVIEW_UKB_ROOT`，默认 `MDagent-evidence-v3/data/ukb`。
- 管理员 token：`design-review-layer/ukb_admin.json`（600，仅本地）。
- CLI：`python -m review_layer.ukb_cli init|seed-all|stats|baseline|common`。

## 4. 验证

- UKB 初始化 + 管理员 `redmaple`；`stats`：rule 9、lesson 12、observation 25。
- `load_baseline()` 返回 9 维；`load_common_issues()` 返回 12 条（含 CI-002/CI-003）。
- 从 UKB 读取后重跑 annotate：RP-008→CI-003（record_required）、RP-011→CI-002（explained），与本地种子一致。
- 机械预审 + 管理员批准链路走通；测试条目已撤销，UKB 内无残留测试规则。
- discovery 默认从 UKB 取基线（9 维）与常见问题（12 条），本地 JSON 仅回退。

## 5. 边界

- 不改 `MDagent-src`；UKB 包为冻结 1.0.0（未改动文件）。
- 证据类目前存"开放审查点"；后续 review verdict / 报告摘要也以 `observation` 入库（同一路径）。
- 经验类新增走 `lesson`；知识类新增走 `rule`/`checklist`，均需审核链。
- 本地 registry JSON 保留为种子与离线回退，不再作为主读取源。
