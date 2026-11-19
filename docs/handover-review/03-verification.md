# 本轮验证事实与后续交接操作

HR-1；2026-11-17，Asia/Shanghai。本文件区分原包历史记录、本轮实际复测、缺陷探针及尚未执行的开发验收。

## 1. 实测结果

| 检查 | 本轮结果 | 证据 |
|---|---|---|
| 原包清单严格校验 | 376 文件摘要一致，无额外文件 | [manifest-before.log](evidence/manifest-before.log)；只读参考副本另作复查 |
| 原脚本完整运行 | 21 个 suite/check 中 20 成功、1 错误；all_ok=false | [RESULTS.json](evidence/rerun/RESULTS.json)、[汇总日志](evidence/original-runner.log) |
| Python unittest | 597 登记，588 成功、1 error、8 skipped | 同上及 [MCP 错误日志](evidence/rerun/logs/gate-core_test_md_mcp_contract.log) |
| Node | 29 成功：flow-gate 27、usage-patch 2 | [flow](evidence/rerun/logs/flow-gate_node.log)、[usage](evidence/rerun/logs/usage-patch_node.log) |
| 双门 E2E | 原脚本的 5 场景成功，非真实求解器执行 | [E2E 日志](evidence/rerun/logs/dual-gate_e2e.log) |
| 模板与 UKB 冻结 | 校验成功 | [模板](evidence/rerun/logs/design-review_template.log)、[冻结](evidence/rerun/logs/ukb-freeze.log) |
| 验证脚本清理 | 发现遗留凭据路径 `src/data/admin.json` | [recheck.json](evidence/recheck.json)；不含令牌内容 |
| 8 项行为探针 | 均复现对应边界问题 | [probes.json](evidence/probes.json) |
| MCP 编码定位 | native locale 失败、仅 stdio UTF-8 失败、完整 UTF-8 12/12 成功 | [编码对比](evidence/encoding/results.json) |

完整复测合计 626 个登记测试，617 成功、1 error、8 skipped。针对性编码复测成功只说明受影响的 12 个测试在完整 UTF-8 模式下成功；本轮**没有在该模式下再跑整个 626 项**，不把两次结果拼成一次“全绿”。

原包历史结果为 618 成功、8 跳过（626 登记）。原报告所称 4 项 MCP/Web 手工冒烟是历史证据，本轮未重复执行。完整自动复测开始 13:49:12，结束 13:55:20。环境：Windows、Python 3.11.4、Node 22.19.0、numpy 1.25.0。

## 2. 失败原因与修复方向

`md_mcp.py:21` 强制 stdout/stderr UTF-8；`test_md_mcp_contract.py:148` 使用 `subprocess.run(text=True)` 却没有指定解码。Windows cp936 下读取线程抛 UnicodeDecodeError，随后 `proc.stderr[-400:]` 触发 TypeError。即使不设置 PYTHONIOENCODING，本机原生 locale 模式仍复现；开启 `PYTHONUTF8=1` 后该套件通过。

这说明测试 harness 存在可移植性缺陷，不能据此判定整个 MCP 业务失效。正式修复应显式指定协议 UTF-8 编解码，并妥善处理读取失败；设置环境只是临时运行方法，原源码未修复。完整 UTF-8 模式日志仍有测试文件句柄 ResourceWarning，建议随测试清理修正。

原验证脚本不重生成 RESULTS.md。为避免混淆，本目录只导出本轮 JSON 和日志，原 Markdown 保留在只读参考副本，不能解释为本轮结果。

## 3. 复测命令

以下在 `F:/BaiduSyncdisk/Codex/Pi-lammps` 执行。输出目录每次用新 run 名，避免覆盖本轮事实。提供的脚本不修改原 ZIP；原验证器只在临时副本执行。脚本移除服务数据根环境变量，并将任务/项目数据定向到临时根；凭据只在临时副本存在，清理后不保留。

```powershell
python docs/handover-review/tools/recheck_package.py `
  F:/BaiduSyncdisk/Codex/MDagent/deliverables/MDagent-subsystems-handover-20261117.zip `
  --output docs/handover-review/evidence/run-next

python docs/handover-review/tools/probe_findings.py `
  source-snapshot/mdagent-handover-review-20261117/MDagent-subsystems-handover-20261117 `
  --output docs/handover-review/evidence/probes-next.json

python docs/handover-review/tools/check_stdio_encoding.py `
  F:/BaiduSyncdisk/Codex/MDagent/deliverables/MDagent-subsystems-handover-20261117.zip `
  --output docs/handover-review/evidence/encoding-next
```

`recheck_package.py` 设置 stdio UTF-8、保留父环境的 PYTHONUTF8，因此在本轮未设 PYTHONUTF8 的 cp936 环境中会重现已记录错误。编码对比脚本显式控制三个模式。要用完整 UTF-8 模式重跑，可在新 PowerShell 进程中设置 `$env:PYTHONUTF8='1'` 后执行第一条，并将输出另存；不得覆盖原失败证据。

探针是在临时目录故意调用边界输入，不是修复后回归测试断言；退出 0 表示探针成功运行，不能表示产品没有缺陷。实际结果查看 `defect_reproduced`。

## 4. 当前部署操作边界与修订要求

运行库选择本机非同步目录；代码和文档可以位于当前同步工作区。给四组件同时设置一致的 `MDSIM_DATA_ROOT`、`MDAGENT_UKB_ROOT=<data>/ukb`、`REVIEW_UKB_ROOT=<data>/ukb`，凭据通过独立受控文件提供。当前源码不会代替操作者验证它们一致，正式配置冲突拒绝属于 HR-W06。

新库初始化只能选择一条路线。`bootstrap.py` 创建 MDagent 使用的种子和管理员；`ukb_cli init` 创建校核用的新库并保存 token。它们不是可以连续执行的步骤，也不具有完全相同的种子/凭据行为。已有库不得再次 init，应先确认版本、根与管理员凭据，再使用维护入口。完整四系统初始化、播种兼容与权限操作需要 HR-W01 的验收证明，不在此宣称现有 README 已满足。

端口统一约定为 UKB 8765、证据 UI 8766、一致性页 8900。真实代码入口分别是：

| cwd（相对可写开发副本根） | 已存在的入口 | 备注 |
|---|---|---|
| `src/ukb` | `python -m ukb --root <绝对UKB根> serve` | 管理员审核台；本轮未启动 |
| `src` | `python -m webui.server --data-root <绝对数据根> --port 8766` | 不附 --enable-execution 时不开放执行 |
| `src/design-review-layer/web` | `python design_plan_server.py 8900` | fixture 页；rt_* 外部案例未随包提供 |
| 包根 | `python src/mcp/md_mcp.py` / `python src/mcp/md_proj_mcp.py` | stdio 服务；配置根须在同一进程环境明确提供 |

这些是入口核对，不是本轮服务启动成功回执。生产启动前应生成缺能力清单，配置 kit 路径、执行注册表、模型身份与凭据；先在 validation 范围做真实演练，再按独立授权启用 production。

## 5. 补充验收环境

8 个历史/本轮跳过项：KB 6（5 项需要未随包分发的生产 UKB、1 项符号链接权限），v3 1 与双门 1 为符号链接权限。需要带来源的脱敏数据集和具权限的 Windows/Linux runner 补跑；不得删 skip 或降低断言来凑全绿。

后续必须补：中文/空格/长路径，Linux 文件锁与进程树取消，跨进程同时写，断电/写盘失败恢复，Blob 缺失恢复，来源撤回/知识重验，真实 LLM 输入覆盖，真实 LAMMPS validation，以及宏观/事件领域隔离。LAMMPS 包测试不能替代 MOD21 的宏观/离散事件真实基准。

## 6. 本次交付状态

已完成：审阅、逐问题开发设计、8 工作包/21 验收项、来源追踪、完整自动复测、定向复现、编码原因定位和文档检查。未完成且未声称完成：12 项产品问题修复、Pi 集成、生产部署、真实模型/科学验收。全部 HR 实现任务保持 planned。
