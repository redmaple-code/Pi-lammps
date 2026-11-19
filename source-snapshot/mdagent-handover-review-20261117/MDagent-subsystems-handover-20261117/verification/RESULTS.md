# 验证结果

> 由 `tools/run_verification.py` 生成；原始输出在 `verification/logs/`。

| 项 | 值 |
|---|---|
| 包 | `MDagent-subsystems-handover-20261117` |
| 时间 | 2026-11-17T12:57:08+0800 |
| Python / 平台 | 3.11.4 / win32 |
| 总结 | 全部通过（失败 0） |

## 套件明细

| 套件 | 结果 | 明细 | 耗时 |
|---|---|---|---|
| `ukb-package` | OK | 103 tests, 0 skipped | 59.4s |
| `kb-compat` | OK | 44 tests, 6 skipped | 52.3s |
| `evidence-v3` | OK | 218 tests, 1 skipped | 89.5s |
| `gate-core:test_stage_gate` | OK | 30 tests, 0 skipped | 6.8s |
| `gate-core:test_stage_state` | OK | 16 tests, 0 skipped | 10.6s |
| `gate-core:test_assessment_v2` | OK | 9 tests, 0 skipped | 3.4s |
| `gate-core:test_config_check` | OK | 9 tests, 0 skipped | 2.4s |
| `gate-core:test_deep_review` | OK | 9 tests, 0 skipped | 0.8s |
| `gate-core:test_evidence_grade` | OK | 21 tests, 0 skipped | 0.4s |
| `gate-core:test_human_authority` | OK | 8 tests, 0 skipped | 1.5s |
| `gate-core:test_md_mcp_contract` | OK | 12 tests, 0 skipped | 1.0s |
| `dual-gate:test_design_review` | OK | 16 tests, 0 skipped | 1.0s |
| `dual-gate:test_physics_gate` | OK | 19 tests, 0 skipped | 0.4s |
| `dual-gate:test_md_proj_mcp` | OK | 36 tests, 1 skipped | 3.5s |
| `dual-gate:test_model_research` | OK | 18 tests, 0 skipped | 0.7s |
| `dual-gate:e2e` | OK | None tests, 0 skipped | 1.6s |
| `design-review` | OK | 29 tests, 0 skipped | 4.1s |
| `design-review:template` | OK | None tests, 0 skipped | 0.2s |
| `ukb-freeze` | OK | sha256 unchanged | 0.3s |
| `flow-gate:node` | OK | 27/27 passed | 0.3s |
| `usage-patch:node` | OK | 2/2 passed | 0.6s |

## 跳过说明

- `kb-compat` 6 项：5 项需要生产 UKB 库（本包不分发运行库），1 项需要目录符号链接权限。
- `evidence-v3` 1 项、`dual-gate:test_md_proj_mcp` 1 项：Windows 符号链接权限。
- `design-review` 在验证时临时播种了 UKB 存储（基线 9 + 常见问题 12 + 审查点 24），跑完已删除。

## 冒烟（不在自动脚本内）

| 项 | 结果 |
|---|---|
| `md_mcp` stdio initialize + tools/list | 通过，17 个工具 |
| `ukb.mcp_server` stdio initialize + tools/list | 通过，16 个工具 |
| 方案校核 8900 `/`、`/api/cases`、`/api/infile_check` | HTTP 200，返回 case1–5 |
| 证据 Web UI 首页 | HTTP 200 |
